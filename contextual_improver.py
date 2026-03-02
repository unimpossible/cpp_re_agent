import knowledge_graph
from llm_factory import get_llm
from scanner import scan_code
import os

def get_callee_context(target_code: str) -> str:
    """
    Extracts function calls from target_code and retrieves context from KG.
    """
    # Parse target code to find calls
    # scan_code returns a list of CppType objects
    scanned_items = scan_code(target_code)
    
    # We assume target_code contains at least one function definition
    target_func = next((i for i in scanned_items if i.kind == "function"), None)
    
    callee_context = ""
    if target_func and target_func.dependencies:
        unique_dependencies = list(set(target_func.dependencies))
        for dep_name in unique_dependencies:
            # Skip self-recursion or generic names if we want filtering
            # Lookup dependency in KG
            func_data = knowledge_graph.get_function_by_name(dep_name)
            if func_data:
                # We include the signature or a truncated body
                # For context, seeing the signature is most crucial
                body_lines = func_data["body"].splitlines()
                # Heuristic: First 5 lines usually contain the signature and opening brace
                signature = "\n".join(body_lines[:10]) 
                callee_context += f"// Function: {dep_name}\n// From: {func_data['source']}\n{signature}\n// ...\n\n"

    return callee_context

def run_contextual_improvement(target_code: str, project_header: str, provider="gemini", model_name="openai/gpt-oss-20b") -> str:
    """
    Executes the improvement loop.
    """
    callee_context = get_callee_context(target_code)
    
    prompt = (
        "You are a Senior C++ Refactoring Engineer. \n"
        "Your goal is to refine the following C++ function to be more readable, idiomatic, and consistent "
        "with the project's types and other functions.\n\n"
        "### Project Types (project.h)\n"
        f"```cpp\n{project_header}\n```\n\n"
        "### Related Function Context (Callees)\n"
        f"```cpp\n{callee_context}\n```\n\n"
        "### Target Function to Improve\n"
        f"```cpp\n{target_code}\n```\n\n"
        "### Instructions\n"
        "1.  **Use Project Types**: Replace generic types (char*, int) with specific structs/classes from `project.h` where they match.\n"
        "2.  **Propagate Names**: If the target function calls functions listed in 'Related Function Context', ensure the arguments passed match the expected types/intent of those callees.\n"
        "3.  **Refine Logic**: Clean up any remaining decompilation artifacts (goto, casts) if possible.\n"
        "4.  **Comments**: Add brief comments explaining *why* you made changes based on the context.\n"
        "5.  **Output**: Return ONLY the improved function code. Do not wrap in markdown.\n"
    )
    
    # Use standard LLM factory
    llm = get_llm(provider, model_name)
    try:
        response = llm.invoke(prompt)
        return response.content.strip().replace("```cpp", "").replace("```", "")
    except Exception as e:
        return f"// Error during contextual improvement: {e}\n\n{target_code}"
