import os
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv

load_dotenv()

def get_llm(provider="local", model_name="openai/gpt-oss-20b"):
    """
    Returns a LangChain LLM instance for the specified provider.
    """
    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key: return None
        return ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, temperature=0)
    
    else: # Default to local / openai compatible
        # Local via OpenAI compatible endpoint
        base_url = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1")
        # For local servers, key often doesn't matter, but we pass something
        return ChatOpenAI(
            base_url=base_url, 
            api_key=os.getenv("LOCAL_LLM_KEY", "lm-studio"), 
            model=model_name,
            temperature=0.2
        )
