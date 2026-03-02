import os
import scanner
from pathlib import Path
from llm_factory import get_llm

def should_improve(code: str) -> bool:
    """
    Heuristic to determine if a function is worth improving.
    """
    if not code or len(code.strip()) == 0:
        return False
    
    # Check for simple thunks or extremely short functions
    lines = [l for l in code.splitlines() if l.strip()]
    if len(lines) < 5:
        return False
        
    return True

def improve_function(code: str, provider="gemini", model_name="openai/gpt-oss-20b", binary_path=None, recursive=False, status_callback=None) -> str:
    """
    Sends the code to the selected LLM provider for improvement.
    Uses LangChain via llm_factory for consistency.
    
    Args:
        code: The C++ code to improve.
        provider: LLM provider name.
        model_name: Model identifier.
        binary_path: Path to the binary (used to locate workspace/context).
        recursive: If True, attempts to improve direct callees (1 layer deep) before improving this function.
        status_callback: Optional function(str) to report progress.
    """
    if not should_improve(code):
        return code # Return original if we skip

    # --- Build Context & Handle Recursion ---
    additional_context = ""
    
    if binary_path:
        # Deduce paths
        bin_name = Path(binary_path).name
        workspace_dir = Path(os.getcwd()) / "workspace" / bin_name
        improved_dir = workspace_dir / "improved"
        raw_dir = workspace_dir / "raw"
        project_h = workspace_dir / "project.h"
        
        # Ensure dirs exist (though they should)
        os.makedirs(improved_dir, exist_ok=True)
        
        # 0. Recursive Improvement (One Layer Deep)
        # Only scan if recursive=True AND we can find files
        if recursive:
            try:
                scanned_items = scanner.scan_code(code)
                print("scanned_items", scanned_items)
                target_func = next((i for i in scanned_items if i.kind == "function"), None)
                if target_func and target_func.dependencies:
                    unique_deps = sorted(list(set(target_func.dependencies)))
                    
                    for dep in unique_deps:
                        dep_improved_path = improved_dir / f"{dep}.cpp"
                        dep_improved_path_glob = improved_dir / f"{dep}*.cpp"
                        matches = list(dep_improved_path_glob.glob("*"))
                        if not dep_improved_path.exists():
                            # Find raw file
                            matches = list(raw_dir.rglob(f"{dep}*.c"))
                            if matches:
                                try:
                                    if status_callback:
                                        status_callback(f"Recursively improving dependency: {dep}")
                                        
                                    with open(matches[0], "r", encoding="utf-8") as rf:
                                        raw_dep_code = rf.read()
                                    
                                    # RECURSIVE CALL (Depth 1: recursive=False)
                                    new_dep_code = improve_function(
                                        raw_dep_code, 
                                        provider, 
                                        model_name, 
                                        binary_path, 
                                        recursive=False,
                                        status_callback=status_callback
                                    )
                                    
                                    # Save immediately so we can use it for context below
                                    with open(dep_improved_path, "w", encoding="utf-8") as wf:
                                        wf.write(new_dep_code)
                                except Exception as e:
                                    print(f"Recursive improve failed for {dep}: {e}")

            except Exception as e:
                print(f"Recursion error: {e}")

        # 1. Load Project Types
        if project_h.exists():
            try:
                with open(project_h, "r", encoding="utf-8") as f:
                    additional_context += f"### Project Types (project.h)\n```cpp\n{f.read()}\n```\n\n"
            except: 
                pass
        
        # 2. Load Neighbor Context (Neighbors)
        try:
             # Re-scan valid context from disk (now potentially populated by recursion)
             scanned_items = scanner.scan_code(code)
             target_func = next((i for i in scanned_items if i.kind == "function"), None)
             if target_func and target_func.dependencies:
                 found_neighbors = False
                 neighbor_text = "### Related Function Signatures (Context)\n```cpp\n"
                 
                 unique_deps = sorted(list(set(target_func.dependencies)))
                 for dep in unique_deps:
                     # Check if improved file exists
                     dep_file = improved_dir / f"{dep}.cpp"
                     if dep_file.exists():
                         with open(dep_file, "r", encoding="utf-8") as f:
                             # Read first 10 lines for signature
                             head = [next(f) for _ in range(10)]
                             neighbor_text += "".join(head) + "\n// ...\n\n"
                             found_neighbors = True
                 
                 neighbor_text += "```\n\n"
                 if found_neighbors:
                     additional_context += neighbor_text
        except Exception as e:
            print(f"Context scan warning: {e}")

    # --- Construct Prompt ---
    system_prompt = (
        "You are an expert C++ Reverse Engineer. "
        "Your task is to improve the readability of the following decompiled C++ code. "
        "1. Rename variables to be meaningful (e.g., iVar1 -> distinct_count). "
        "2. Fix data types where obvious. "
        "3. Add comments explaining the logic. "
        "4. Do NOT change the functional behavior. "
        "5. Return ONLY the C++ code, no markdown fencing."
        "6. Create structs or classes where appropriate."
        "7. If you aren't sure what something does, do not rename it."
    )
    
    if additional_context:
        system_prompt += (
            "\n\n### CONTEXT PROVIDED\n"
            "You have been provided with 'Project Types' and/or 'Related Function Signatures'. "
            "USE THEM. If the code calls a function found in the context, ensure your argument types match the signature. "
            "Use the defined structs from Project Types instead of raw pointers/bytes where applicable."
        )

    try:
        # Get standardized LangChain LLM
        llm = get_llm(provider, model_name)
        if not llm:
            return "// Error: LLM client could not be initialized (check API keys/env)."

        # LangChain invoke
        messages = [
            ("system", system_prompt),
            ("user", f"{additional_context}Code to Improve:\n{code}")
        ]
        
        response = llm.invoke(messages)
        return response.content.strip().replace("```cpp", "").replace("```", "")
            
    except Exception as e:
        return f"// Error during AI improvement: {str(e)}"

    return code
