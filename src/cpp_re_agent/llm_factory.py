import os
import sys

from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv


def _patch_pydantic_v1_for_py314() -> None:
    """
    Teach pydantic v1 about PEP 649 deferred annotations.

    On Python 3.14 a class body carries `__annotate_func__` instead of a
    populated `__annotations__`, which pydantic v1 (vendored inside langfuse)
    does not know about. Must run before any pydantic v1 model is imported.

    Guarded by version: `annotationlib` is 3.14+, so importing it unconditionally
    breaks every supported older interpreter — including the 3.11 CI runs.
    """
    if sys.version_info < (3, 14):
        return
    import annotationlib
    import pydantic.v1.main as pydantic_main

    original = pydantic_main.ModelMetaclass.__new__

    def patched(mcs, name, bases, ns, **kw):
        if not ns.get("__annotations__") and "__annotate_func__" in ns:
            try:
                ns["__annotations__"] = ns["__annotate_func__"](annotationlib.Format.VALUE)
            except Exception:
                pass
        return original(mcs, name, bases, ns, **kw)

    pydantic_main.ModelMetaclass.__new__ = staticmethod(patched)


_patch_pydantic_v1_for_py314()

from langfuse.langchain import CallbackHandler  # noqa: E402  (must follow the patch)

load_dotenv()


class ImprovementError(Exception):
    """
    An LLM improvement/refinement call failed.

    Raised instead of returning error text as if it were code: a returned
    `"// Error: ..."` string is indistinguishable from a result to callers that
    write it to disk or cache it, which turns one transient failure into a
    permanently poisoned workspace.
    """


def get_llm(provider="local", model_name="openai/gpt-oss-20b"):
    """
    Returns a LangChain LLM instance for the specified provider.
    """
    ai = None
    # Bound every request so a hung/queued call fails fast instead of stalling a
    # whole batch run indefinitely. Generous (slow reasoning models can take a
    # couple of minutes) but finite, with a couple of retries for transient drops.
    request_timeout = float(os.getenv("LLM_TIMEOUT", "300"))

    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key: return None
        ai = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key,
                                    temperature=0, timeout=request_timeout, max_retries=2)

    else: # Default to local / openai compatible
        # Local via OpenAI compatible endpoint
        base_url = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1")
        # For local servers, key often doesn't matter, but we pass something
        ai = ChatOpenAI(
            base_url=base_url,
            api_key=os.getenv("LOCAL_LLM_KEY", "lm-studio"),
            model=model_name,
            temperature=0.2,
            timeout=request_timeout,
            max_retries=2,
        )

    # Attach the Langfuse callback unless disabled. Set DISABLE_LANGFUSE=1 for
    # long batch/experiment runs: its trace exporter can time out against an
    # unreachable server and stall or crash the process.
    if os.getenv("DISABLE_LANGFUSE", "").lower() in ("1", "true", "yes"):
        return ai
    return ai.with_config(callbacks=[CallbackHandler()])
