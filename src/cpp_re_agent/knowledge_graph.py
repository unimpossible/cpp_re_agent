import os
import uuid
from typing import List, Dict
from .scanner import CppType
from .llm_factory import ImprovementError, require_llm, stream_text

# Persistent local storage for the ChromaDB vector store.
DB_DIR = os.path.join(os.getcwd(), "chroma_db")

# ChromaDB is imported lazily so this module (and the consolidation prompt /
# consolidate_definitions, which need no DB) can be imported in environments
# where chromadb's native deps are unavailable (e.g. a blocked grpc DLL).
_client = None
_collection = None
_functions_collection = None


def _collections():
    """Lazily create and return (types_collection, functions_collection)."""
    global _client, _collection, _functions_collection
    if _client is None:
        import chromadb
        _client = chromadb.PersistentClient(path=DB_DIR)
        _collection = _client.get_or_create_collection(name="cpp_types")
        _functions_collection = _client.get_or_create_collection(name="cpp_functions")
    return _collection, _functions_collection


def clear_db():
    global _collection, _functions_collection
    _collections()  # ensure _client is initialized
    _client.delete_collection("cpp_types")
    _client.delete_collection("cpp_functions")
    _collection = _client.get_or_create_collection(name="cpp_types")
    _functions_collection = _client.get_or_create_collection(name="cpp_functions")

def add_scan_results(items: List[CppType], source_file: str):
    """
    Adds scanned items to their respective vector DB collections.
    """
    if not items:
        return

    collection, functions_collection = _collections()

    # Split types and functions
    types = [t for t in items if t.kind in ["struct", "class", "declaration"]]
    funcs = [t for t in items if t.kind == "function"]

    # Add Types
    if types:
        ids = [str(uuid.uuid4()) for _ in types]
        documents = [t.body for t in types]
        metadatas = [{"name": t.name, "kind": t.kind, "source": source_file} for t in types]
        collection.add(documents=documents, metadatas=metadatas, ids=ids)

    # Add Functions
    if funcs:
        f_ids = [str(uuid.uuid4()) for _ in funcs]
        f_docs = [t.body for t in funcs]
        # Flatten dependencies list to string for metadata storage constraint
        f_metas = [{
            "name": t.name, 
            "kind": "function", 
            "source": source_file,
            "dependencies": ",".join(t.dependencies)
        } for t in funcs]
        functions_collection.add(documents=f_docs, metadatas=f_metas, ids=f_ids)

# Backwards compatibility alias
add_types = add_scan_results

def get_function_by_name(name: str):
    """
    Retrieves a function by its exact name.
    """
    # Filter by metadata field "name"
    _, functions_collection = _collections()
    results = functions_collection.get(where={"name": name})
    if results and results['documents']:
        # Return the first match (assuming uniqueness per file or globally desired)
        return {
            "body": results['documents'][0],
            "source": results['metadatas'][0]['source'],
            "dependencies": results['metadatas'][0]['dependencies'].split(',') if results['metadatas'][0]['dependencies'] else []
        }
    return None

def get_all_types() -> List[Dict]:
    """
    Retrieves all types from the DB.
    """
    # Chroma get without args returns everything (up to limit)
    collection, _ = _collections()
    results = collection.get()
    
    all_types = []
    if results and results['ids']:
        for i in range(len(results['ids'])):
            all_types.append({
                "id": results['ids'][i],
                "body": results['documents'][i],
                "name": results['metadatas'][i]['name'],
                "source": results['metadatas'][i]['source']
            })
    return all_types



# Base instruction for header synthesis. Exposed as a constant (like
# ai_improver.DEFAULT_SYSTEM_PROMPT) so the experiment framework can use it as
# the control/seed prompt and inject evolved variants. The definitions block is
# appended at call time.
DEFAULT_CONSOLIDATE_PROMPT = (
    "You are a C++ Architect. I have extracted the following struct/class definitions "
    "from various decompiled files. Some are duplicates with different names, or "
    "partial definitions. \n\n"
    "Your task:\n"
    "1. Identify unique types.\n"
    "2. Merge partial definitions into the most complete version.\n"
    "3. Resolve naming conflicts (prefer more descriptive names).\n"
    "4. Identify any matching function prototypes or forward declarations and include them.\n"
    "5. Generate a single valid `project.h` file containing all these merged definitions.\n"
    "6. Return ONLY the C++ code."
)


def consolidate_definitions(all_definitions: str, provider="local",
                            model_name="openai/gpt-oss-20b", base_prompt=None) -> str:
    """
    Merge a block of (possibly duplicate/partial) type definitions into a single
    project.h via the LLM. `base_prompt` overrides the default instruction (this
    is the knob the header-synthesis experiment optimizes).

    Streams (via `llm_factory.stream_text`) rather than blocking on a single
    `invoke`: this is the largest prompt the pipeline sends, and a non-streaming
    call sits idle — no bytes on the wire — for the whole generation, which
    trips idle-read timeouts on proxies and gateways in front of remote
    endpoints. Streaming keeps bytes flowing so those timeouts don't fire.
    """
    llm = require_llm(provider, model_name)
    instruction = base_prompt if base_prompt is not None else DEFAULT_CONSOLIDATE_PROMPT
    prompt = f"{instruction}\n\nDefinitions:\n{all_definitions}"
    try:
        content = stream_text(llm, prompt)
    except Exception as e:
        raise ImprovementError(f"Header synthesis failed: {e}") from e
    return content.strip().replace("```cpp", "").replace("```", "")


def consolidate_types(provider="local", model_name="openai/gpt-oss-20b",
                      base_prompt=None) -> str:
    """
    Naive consolidation:
    1. Get all types.
    2. Ask LLM to merge them into a single header.
    (Future: Use clustering here for large datasets)
    """
    types = get_all_types()
    if not types:
        return "// No types found."

    all_definitions = "\n\n".join([f"// From {t['source']}\n{t['body']}" for t in types])
    return consolidate_definitions(all_definitions, provider, model_name, base_prompt)
