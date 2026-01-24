import os
import google.generativeai as genai
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

def get_client(provider="gemini"):
    """
    Factory function to get the appropriate AI client.
    """
    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return None # Handle gracefully in UI
        genai.configure(api_key=api_key)
        return "gemini_client" # Placeholder, actual call differs slightly
    elif provider == "local":
        # Assumes local server running at standard OpenAI compatible endpoint
        base_url = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1")
        api_key = os.getenv("LOCAL_LLM_KEY", "lm-studio") # Key often doesn't matter for local
        return OpenAI(base_url=base_url, api_key=api_key)
    return None

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
    )

    try:
        if provider == "gemini":
            client = get_client("gemini")
            if not client:
                return "// Error: GEMINI_API_KEY not set."
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(f"{system_prompt}\n\nCode:\n{code}")
            return response.text.strip().replace("```cpp", "").replace("```", "")

        elif provider == "local":
            client = get_client("local")
            response = client.chat.completions.create(
                model=model_name, # e.g. "qwen2.5-coder-7b"
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": code}
                ],
                temperature=0.2
            )
            print(f"DEBUG: LLM Response: {response}") # Debugging
            
            if not response or not hasattr(response, 'choices') or not response.choices:
                 return f"// Error: Invalid response from Local LLM. raw response: {response}"
                 
            return response.choices[0].message.content.strip().replace("```cpp", "").replace("```", "")
            
    except Exception as e:
        return f"// Error during AI improvement: {str(e)}"

    return code
