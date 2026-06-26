"""
Contextual ("function merging") experiment.

The contextual pass takes an already-improved function plus project types and the
signatures of the functions it calls, and refines it for cross-function
consistency. This harness measures whether that second pass moves a function
CLOSER to the original than the first-pass improvement alone.

Per function: first-pass improve (improve_fn) -> contextual refine (refine_fn)
given project types + callee signatures extracted from the original program.
Score both vs the original with the shared per-function metric; the headline is
refined-minus-base (does the context pass help?).
"""
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

from cpp_re_agent import scanner, symbol_map

from . import roundtrip
from .corpus import Program
from .dataset import FunctionExample


def program_context(program: Program) -> Tuple[str, Dict[str, str]]:
    """
    Returns (project_header_text, {normalized_func_name: signature}) for a
    program, extracted from its ORIGINAL source — the ideal context the
    contextual prompt is meant to exploit.
    """
    type_defs: List[str] = []
    sigs: Dict[str, str] = {}
    for item in scanner.scan_code(program.source):
        if item.kind in ("struct", "class"):
            type_defs.append(item.body)
        elif item.kind == "function":
            res = symbol_map.extract_signature(item.body)
            if res:
                name, sig = res
                sigs[roundtrip._normalize_name(name)] = sig
    header = "\n\n".join(type_defs) if type_defs else "// (no project types)"
    return header, sigs


def callee_context(original_body: str, sigs: Dict[str, str], self_name: str) -> str:
    """Signatures of the functions this one calls; falls back to all siblings."""
    deps: List[str] = []
    for item in scanner.scan_code(original_body):
        if item.kind == "function":
            deps = item.dependencies
            break
    callees = {roundtrip._normalize_name(d) for d in deps}
    lines = [f"{sigs[c]};" for c in sorted(callees) if c in sigs and c != self_name]
    if not lines:  # no project callees — give sibling signatures as context
        lines = [f"{s};" for n, s in sigs.items() if n != self_name]
    return "\n".join(lines) if lines else "// (no related functions)"


@dataclass
class ContextualResult:
    name: str
    original: str
    decompiled: str
    base_improved: str
    refined: str
    error: str | None = None


def run_contextual_roundtrip(example: FunctionExample, header: str, callees: str,
                             improve_fn: Callable[[str], str],
                             refine_fn: Callable[[str, str, str], str],
                             status: Callable[[str], None] | None = None
                             ) -> ContextualResult:
    """First-pass improve, then contextual refine with project + callee context."""
    def _log(msg):
        if status:
            status(msg)
    try:
        _log("first-pass improve (LLM)...")
        base_improved = improve_fn(example.decompiled)
        _log("contextual refine (LLM)...")
        refined = refine_fn(base_improved, header, callees)
        return ContextualResult(example.name, example.original, example.decompiled,
                                base_improved, refined)
    except Exception as e:
        return ContextualResult(example.name, example.original, example.decompiled,
                                "", "", error=str(e))
