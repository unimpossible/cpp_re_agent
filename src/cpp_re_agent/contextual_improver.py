from . import knowledge_graph
from .llm_factory import ImprovementError, require_llm, stream_text
from .scanner import scan_code
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

# Base instruction for the contextual ("function merging") pass: refine one
# already-improved function so it is consistent with the project's types and the
# functions it calls. Exposed as a constant (like ai_improver.DEFAULT_SYSTEM_PROMPT
# and knowledge_graph.DEFAULT_CONSOLIDATE_PROMPT) so the experiment framework can
# inject evolved variants. The context/data sections are appended at call time.
DEFAULT_CONTEXTUAL_PROMPT = (
    "You are a Senior C++ Refactoring Engineer. Refine the target C++ function so "
    "it is more readable, idiomatic, and consistent with the project's types and "
    "the other functions it uses.\n"
    "Instructions:\n"
    "1. Use Project Types: replace generic types (char*, int, void*) with the "
    "specific structs/classes from project.h where they match.\n"
    "2. Propagate Names/Types: if the target calls functions in the Related "
    "Function Context, make the arguments and return handling match those callees' "
    "expected types and intent.\n"
    "3. Refine Logic: clean up remaining decompilation artifacts (goto, raw casts) "
    "where it does not change behavior.\n"
    "4. Preserve behavior exactly; do not invent fields or change the function's "
    "signature beyond adopting project types.\n"
    "5. Output: return ONLY the refined function code, with no markdown fences.\n"
    "You are given Project Types, Related Function Context (callees), and the "
    "Target Function below."
)


def refine_function(target_code: str, project_header: str, callee_context: str,
                    provider="local", model_name="openai/gpt-oss-20b",
                    base_prompt=None) -> str:
    """
    Run the contextual refinement on `target_code` given project types and callee
    context. `base_prompt` overrides the instruction (the experiment's knob);
    context is supplied directly (no knowledge-graph/chromadb dependency).
    """
    instruction = base_prompt if base_prompt is not None else DEFAULT_CONTEXTUAL_PROMPT
    prompt = (
        f"{instruction}\n\n"
        f"### Project Types (project.h)\n```cpp\n{project_header}\n```\n\n"
        f"### Related Function Context (Callees)\n```cpp\n{callee_context}\n```\n\n"
        f"### Target Function to Improve\n```cpp\n{target_code}\n```\n"
    )
    llm = require_llm(provider, model_name)
    try:
        content = stream_text(llm, prompt)
    except Exception as e:
        raise ImprovementError(f"Contextual refinement failed: {e}") from e
    return content.strip().replace("```cpp", "").replace("```", "")


def run_contextual_improvement(target_code: str, project_header: str,
                               provider="gemini", model_name="openai/gpt-oss-20b",
                               base_prompt=None) -> str:
    """Build callee context from the knowledge graph, then refine."""
    callee_context = get_callee_context(target_code)
    return refine_function(target_code, project_header, callee_context,
                           provider, model_name, base_prompt)
