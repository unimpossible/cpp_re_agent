"""
Header-synthesis experiment (the whole-program prompt).

Pipeline per program: improve each decompiled function (which emits messy,
duplicated, partial struct/class guesses) -> collect those type fragments ->
run the consolidation prompt under test -> synthesize one project.h -> score how
well it recovers the ORIGINAL program's struct/field layout.

This is a separate optimization target from the per-function improver and reuses
the same tracking + (optionally) the reflective optimizer.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Set

from cpp_re_agent import scanner

from . import roundtrip
from .corpus import Program
from .metrics import MetricResult, parses

HEADER_WEIGHTS = {"struct": 0.6, "judge": 0.4}


def _field_identifiers(node, code: str) -> Set[str]:
    """All member/method names (field_identifier nodes) under a struct/class body."""
    names: Set[str] = set()

    def walk(n):
        if n.type == "field_identifier":
            names.add(n.text.decode("utf-8"))
        for c in n.children:
            walk(c)

    walk(node)
    return names


def extract_structs(code: str) -> Dict[str, Set[str]]:
    """Map struct/class name -> set of member field/method names (no length filter)."""
    if not code:
        return {}
    parser = scanner.get_parser()
    root = parser.parse(code.encode("utf-8")).root_node
    structs: Dict[str, Set[str]] = {}

    def walk(n):
        if n.type in ("struct_specifier", "class_specifier"):
            name_node = n.child_by_field_name("name")
            body = n.child_by_field_name("body")
            if name_node and body:
                name = name_node.text.decode("utf-8")
                fields = _field_identifiers(body, code)
                if fields:
                    structs[name] = fields
        for c in n.children:
            walk(c)

    walk(root)
    return structs


def struct_recovery(original: str, project_h: str) -> MetricResult:
    """
    For each original struct, find the best-matching synthesized struct (by field
    overlap, since names may differ) and score field-name recall. Overall = mean
    recall across original structs.
    """
    orig = extract_structs(original)
    synth = extract_structs(project_h)
    if not orig:
        return MetricResult(1.0, {"orig_structs": 0}, "Original has no structs to recover.")

    per_struct = []
    feedback = []
    for name, fields in orig.items():
        best_recall, best_match, best_overlap = 0.0, None, set()
        for sname, sfields in synth.items():
            overlap = fields & sfields
            recall = len(overlap) / len(fields)
            if recall > best_recall:
                best_recall, best_match, best_overlap = recall, sname, overlap
        per_struct.append(best_recall)
        missed = sorted(fields - best_overlap)
        feedback.append(
            f"{name}: {len(best_overlap)}/{len(fields)} fields recovered"
            + (f" as '{best_match}'" if best_match else " (no match)")
            + (f"; missing {', '.join(missed)}" if missed else "")
        )

    score = sum(per_struct) / len(per_struct)
    return MetricResult(
        score=score,
        components={"orig_structs": len(orig), "synth_structs": len(synth)},
        feedback_text=" | ".join(feedback),
    )


@dataclass
class HeaderResult:
    program_id: str
    original_source: str
    fragments: List[str] = field(default_factory=list)
    project_h: str = ""
    error: str | None = None


def run_header_roundtrip(program: Program, improve_fn: Callable[[str], str],
                         consolidate_fn: Callable[[str], str],
                         status: Callable[[str], None] | None = None) -> HeaderResult:
    """Improve functions -> gather type fragments -> synthesize project.h."""
    def _log(m):
        if status:
            status(f"[{program.id}] {m}")
    try:
        targets = roundtrip.decompiled_targets(program)
        groups, reps = roundtrip._dedup_targets(targets)

        fragments: List[str] = []
        for rep, code in reps.items():
            improved = improve_fn(code)
            for item in scanner.scan_code(improved):
                if item.kind in ("struct", "class"):
                    fragments.append(f"// from {rep}\n{item.body}")
        _log(f"{len(fragments)} type fragment(s) extracted")

        if not fragments:
            return HeaderResult(program.id, program.source, [], "// no types extracted")

        project_h = consolidate_fn("\n\n".join(fragments))
        return HeaderResult(program.id, program.source, fragments, project_h)
    except Exception as e:
        return HeaderResult(program.id, program.source, error=str(e))


def score_header(result: HeaderResult,
                 judge_fn: Callable[[str, str], MetricResult] | None = None,
                 weights: Dict[str, float] | None = None) -> MetricResult:
    """Parse-gated blend of struct/field recovery and (optional) judge."""
    weights = weights or HEADER_WEIGHTS
    if result.error:
        return MetricResult(0.0, feedback_text=f"Header round-trip failed: {result.error}")
    if not parses(result.project_h):
        return MetricResult(0.0, {"parses": 0.0},
                            "Synthesized project.h does not parse as valid C++.")

    parts = {"parses": 1.0}
    feedback = []
    sr = struct_recovery(result.original_source, result.project_h)
    parts["struct"] = sr.score
    parts.update(sr.components)
    feedback.append(sr.feedback_text)
    active = {"struct": weights.get("struct", 0.0)}

    if judge_fn is not None:
        jr = judge_fn(result.original_source, result.project_h)
        parts["judge"] = jr.score
        feedback.append("Judge: " + jr.feedback_text)
        active["judge"] = weights.get("judge", 0.0)

    total = sum(active.values()) or 1.0
    score = sum(parts[k] * w for k, w in active.items()) / total
    return MetricResult(score, parts, " | ".join(feedback))
