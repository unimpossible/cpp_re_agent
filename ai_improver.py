from llm_factory import get_llm

def should_improve(code: str) -> bool:
    """
    Heuristic to determine if a function is worth improving.
    """
    if not code or len(code.strip()) == 0:
        return False
    
    # Check for simple thunks or extremely short functions
    lines = [l for l in code.splitlines() if l.strip()]
    if len(lines) < 10:
        return False
        
    return True

def improve_function(code: str, provider="gemini", model_name="openai/gpt-oss-20b") -> str:
    """
    Sends the code to the selected LLM provider for improvement.
    Uses LangChain via llm_factory for consistency.
    """
    if not should_improve(code):
        return code # Return original if we skip

    system_prompt = (
        "You are an expert C++ Reverse Engineer. "
        "Your task is to improve the readability of the following decompiled C++ code. "
        "1. Rename variables to be meaningful (e.g., iVar1 -> distinct_count). "
        "2. Fix data types where obvious. "
        "3. Add comments explaining the logic. "
        "4. Do NOT change the functional behavior. "
        "5. Return ONLY the C++ code, no markdown fencing."
        "6. Create structs or classes where appropriate."
        "7. If you arent sure what something does, do not rename it."
    )

    try:
        # Get standardized LangChain LLM
        llm = get_llm(provider, model_name)
        if not llm:
            return "// Error: LLM client could not be initialized (check API keys/env)."

        # LangChain invoke
        messages = [
            ("system", system_prompt),
            ("user", f"Code:\n{code}")
        ]
        
        response = llm.invoke(messages)
        return response.content.strip().replace("```cpp", "").replace("```", "")
            
    except Exception as e:
        return f"// Error during AI improvement: {str(e)}"

    return code
