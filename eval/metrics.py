"""
Scoring for a round-trip result. Each metric is a small pure function; the
`composite` aggregator gates on parseability and blends the rest into a single
fitness plus a natural-language `feedback_text` (the channel GEPA reflects on).

P1 metrics: parse gate, identifier recovery, LLM judge. Semantic proxy
(re-decompile diff) and CodeBLEU are added in P2.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from cpp_re_agent import scanner

# Identifiers that carry no "recovery" signal: language keywords, primitive
# types, and ubiquitous std names. Compared case-insensitively.
_STOP_IDENTIFIERS: Set[str] = {
    "int", "char", "bool", "double", "float", "long", "short", "unsigned",
    "signed", "void", "const", "static", "struct", "class", "return", "if",
    "else", "for", "while", "do", "switch", "case", "break", "continue",
    "true", "false", "nullptr", "null", "sizeof", "new", "delete", "this",
    "std", "cout", "cin", "cerr", "endl", "string", "vector", "size_t",
    "uint", "ulong", "auto", "namespace", "using", "include", "main",
}

# Default composite weights (sum need not be 1; composite normalizes by total).
DEFAULT_WEIGHTS = {"identifier": 0.4, "judge": 0.6}


@dataclass
class MetricResult:
    score: float
    components: Dict[str, float] = field(default_factory=dict)
    feedback_text: str = ""
    details: Dict[str, object] = field(default_factory=dict)


def extract_identifiers(code: str) -> Set[str]:
    """Meaningful identifiers (variables/fields/functions) from C++ via tree-sitter."""
    if not code:
        return set()
    parser = scanner.get_parser()
    root = parser.parse(code.encode("utf-8")).root_node
    found: Set[str] = set()

    def walk(node):
        if node.type in ("identifier", "field_identifier"):
            # node.text is byte-offset-correct; slicing the str with byte offsets
            # silently corrupts once the source contains any non-ASCII char.
            name = node.text.decode("utf-8")
            if len(name) >= 2 and name.lower() not in _STOP_IDENTIFIERS:
                found.add(name)
        for child in node.children:
            walk(child)

    walk(root)
    return found


def parses(code: str) -> bool:
    """Hard gate: does the reconstruction parse as C++ (tree-sitter, no errors)?"""
    return scanner.is_valid_cpp(code)


def identifier_recovery(original: str, improved: str, raw: str) -> MetricResult:
    """
    Recall of original identifiers that were *lost* in decompilation (i.e. not
    present in the raw decompiled code) and recovered in the improved output.
    Measures genuine recovery rather than names that simply survived stripping.
    """
    orig_ids = extract_identifiers(original)
    raw_ids = extract_identifiers(raw)
    improved_ids = extract_identifiers(improved)

    recoverable = orig_ids - raw_ids
    if not recoverable:
        # Nothing was lost (or original is trivial) — neutral, not penalized.
        return MetricResult(
            score=1.0,
            components={"recovered": 0, "recoverable": 0},
            feedback_text="No identifiers were lost in decompilation to recover.",
        )

    recovered = recoverable & improved_ids
    missed = sorted(recoverable - improved_ids)
    score = len(recovered) / len(recoverable)
    fb = f"Recovered {len(recovered)}/{len(recoverable)} lost identifiers."
    if missed:
        fb += f" Still generic / not recovered: {', '.join(missed[:12])}."
    return MetricResult(
        score=score,
        components={"recovered": len(recovered), "recoverable": len(recoverable)},
        feedback_text=fb,
        details={"missed": missed, "recovered": sorted(recovered)},
    )


def score_pair(original: str, improved: str, raw: str,
               judge_fn: Optional[Callable[[str, str], MetricResult]] = None,
               weights: Optional[Dict[str, float]] = None) -> MetricResult:
    """
    Score an (original, improved, raw) triple — the shared scorer used both per
    function (optimizer) and whole-program (composite).

    - Hard gate: `improved` must parse, else score 0 with the reason surfaced.
    - judge_fn(original, improved) -> MetricResult is injected to keep this module
      free of LLM deps; omit it to score on identifier recovery alone.
    """
    weights = weights or DEFAULT_WEIGHTS

    if not parses(improved):
        return MetricResult(
            0.0,
            components={"parses": 0.0},
            feedback_text="Reconstruction does not parse as valid C++ — fix "
                          "syntax (balanced braces, complete statements) first.",
        )

    parts: Dict[str, float] = {"parses": 1.0}
    feedback: List[str] = []

    idr = identifier_recovery(original, improved, raw)
    parts["identifier"] = idr.score
    feedback.append(idr.feedback_text)

    active = {"identifier": weights.get("identifier", 0.0)}
    if judge_fn is not None:
        jr = judge_fn(original, improved)
        parts["judge"] = jr.score
        parts.update({f"judge_{k}": v for k, v in jr.components.items()})
        feedback.append("Judge: " + jr.feedback_text)
        active["judge"] = weights.get("judge", 0.0)

    total_w = sum(active.values()) or 1.0
    score = sum(parts[k] * w for k, w in active.items()) / total_w

    return MetricResult(
        score=score,
        components=parts,
        feedback_text=" ".join(feedback),
        details={"missed_identifiers": idr.details.get("missed", [])},
    )


def composite(result, judge_fn: Optional[Callable[[str, str], MetricResult]] = None,
              weights: Optional[Dict[str, float]] = None) -> MetricResult:
    """Whole-program fitness for a RoundtripResult (delegates to score_pair)."""
    if result.error:
        return MetricResult(0.0, feedback_text=f"Round-trip failed: {result.error}")

    mr = score_pair(result.original_source, result.improved_source,
                    result.raw_source, judge_fn=judge_fn, weights=weights)
    mr.components["syntax_compile_ok"] = float(result.syntax_ok)
    return mr
