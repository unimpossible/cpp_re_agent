"""
LLM-as-judge: rate how well a reconstruction recovers the ORIGINAL source.

Returns a metrics.MetricResult whose score is the overall rating, whose
components are the per-axis scores, and whose feedback_text is the critique —
the natural-language signal GEPA mutates the prompt against. The judge model is
resolved through llm_factory.get_llm, so its credentials come from .env.
"""
import json
import re
from typing import Callable

from cpp_re_agent.llm_factory import get_llm

from .metrics import MetricResult

_RUBRIC = """You are grading a C++ reverse-engineering result. A program was \
compiled to a binary, decompiled, and an LLM tried to rewrite the decompiled \
code back into something close to the ORIGINAL source. Judge how well the \
RECONSTRUCTION recovers the ORIGINAL.

Score each axis from 0.0 to 1.0:
- correctness: does the reconstruction preserve the original's behavior/logic?
- readability: is it as clear and idiomatic as the original?
- naming: are variable/function names as meaningful as the original's intent?
- types: are data types/structs as accurate as the original's?

Return ONLY a JSON object:
{"correctness": x, "readability": x, "naming": x, "types": x, "overall": x, \
"critique": "one or two sentences on what is still missing vs the original"}

### ORIGINAL
```cpp
{original}
```

### RECONSTRUCTION
```cpp
{improved}
```
"""

_AXES = ("correctness", "readability", "naming", "types")


def _parse_json(text: str) -> dict:
    """Extract the first JSON object from a model response, tolerating fences/prose."""
    text = text.replace("```json", "").replace("```", "")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except Exception:
        return {}


def make_judge(provider: str = "local", model_name: str = "openai/gpt-oss-20b"
               ) -> Callable[[str, str], MetricResult]:
    """
    Build a judge_fn(original, improved) -> MetricResult bound to one model.
    Pass the result to metrics.composite(..., judge_fn=...).
    """
    llm = get_llm(provider, model_name)

    def judge(original: str, improved: str) -> MetricResult:
        if llm is None:
            return MetricResult(0.0, feedback_text="Judge LLM unavailable (check .env).")
        prompt = _RUBRIC.replace("{original}", original).replace("{improved}", improved)
        try:
            data = _parse_json(llm.invoke(prompt).content)
        except Exception as e:
            return MetricResult(0.0, feedback_text=f"Judge call failed: {e}")

        if not data:
            return MetricResult(0.0, feedback_text="Judge returned unparseable output.")

        axes = {a: float(data.get(a, 0.0)) for a in _AXES}
        overall = float(data.get("overall", sum(axes.values()) / len(axes)))
        critique = str(data.get("critique", "")).strip()
        return MetricResult(score=overall, components=axes, feedback_text=critique)

    return judge
