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

def clear_db():
    global collection
    client.delete_collection("cpp_types")
    collection = client.get_or_create_collection(name="cpp_types")

def add_types(types: List[CppType], source_file: str):
    """
    Adds extracted types to the vector DB.
    """
    if not types:
        return

    ids = [str(uuid.uuid4()) for _ in types]
    documents = [t.body for t in types] # Embed the full body for similarity
    metadatas = [{"name": t.name, "kind": t.kind, "source": source_file} for t in types]
    
    collection.add(
        documents=documents,
        metadatas=metadatas,
        ids=ids
    )

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
        "4. Generate a single valid `project.h` file containing all these merged definitions.\n"
        "5. Return ONLY the C++ code.\n\n"
        f"Definitions:\n{all_definitions}"
    )
    
    response = llm.invoke(prompt)
    return response.content.strip().replace("```cpp", "").replace("```", "")
