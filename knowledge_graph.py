import os
import chromadb
import uuid
from typing import List, Dict
from scanner import CppType
from llm_factory import get_llm

# Setup ChromaDB
# Persistent local storage
DB_DIR = os.path.join(os.getcwd(), "chroma_db")
client = chromadb.PersistentClient(path=DB_DIR)

# Collection for types
# We use a simple default embedding function (all-MiniLM-L6-v2) typically built-in or provided
collection = client.get_or_create_collection(name="cpp_types")
functions_collection = client.get_or_create_collection(name="cpp_functions")

def clear_db():
    global collection, functions_collection
    client.delete_collection("cpp_types")
    client.delete_collection("cpp_functions")
    collection = client.get_or_create_collection(name="cpp_types")
    functions_collection = client.get_or_create_collection(name="cpp_functions")

def add_scan_results(items: List[CppType], source_file: str):
    """
    Adds scanned items to their respective vector DB collections.
    """
    if not items:
        return

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



def consolidate_types(provider="local") -> str:
    """
    Naive consolidation: 
    1. Get all types.
    2. Ask LLM to merge them into a single header.
    (Future: Use clustering here for large datasets)
    """
    types = get_all_types()
    if not types:
        return "// No types found."

    # Concatenate all type bodies
    all_definitions = "\n\n".join([f"// From {t['source']}\n{t['body']}" for t in types])
    
    llm = get_llm(provider)
    
    prompt = (
        "You are a C++ Architect. I have extracted the following struct/class definitions "
        "from various decompiled files. Some are duplicates with different names, or "
        "partial definitions. \n\n"
        "Your task:\n"
        "1. Identify unique types.\n"
        "2. Merge partial definitions into the most complete version.\n"
        "3. Resolve naming conflicts (prefer more descriptive names).\n"
        "4. Identify any matching function prototypes or forward declarations and include them.\n"
        "5. Generate a single valid `project.h` file containing all these merged definitions.\n"
        "6. Return ONLY the C++ code.\n\n"
        f"Definitions:\n{all_definitions}"
    )
    
    response = llm.invoke(prompt)
    return response.content.strip().replace("```cpp", "").replace("```", "")
