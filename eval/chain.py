"""
Whole-chain model sweep — which task model is best end-to-end.

Runs the full production chain for ONE task model: decompile (cached) -> stage-1
per-function improve -> synthesize project.h from the improved type fragments ->
stage-2 contextual refine each function with that header + its callee signatures.
The same model drives all three LLM stages (the direct reading of "best model for
the whole chain"); per-stage mix-and-match is a follow-up over this same code.

Scoring uses a FIXED judge, so the only thing varying across conditions is the
task model. We score both the stage-1 output and the final refined output, so the
table shows whether stage 2 helped or hurt for each model, plus header struct
recovery. Context for stage 2 is the model's OWN synthesized header (production-
faithful) — a model that synthesizes a bad header pays for it downstream, which
is exactly the cross-stage coupling a whole-chain test should capture.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from cpp_re_agent import pipeline, scanner, symbol_map

from . import roundtrip
from .corpus import Program


@dataclass
class ChainResult:
    program_id: str
    original_source: str
    raw_functions: Dict[str, str] = field(default_factory=dict)   # name -> decompiled
    improved: Dict[str, str] = field(default_factory=dict)        # name -> stage 1
    project_h: str = ""
    refined: Dict[str, str] = field(default_factory=dict)         # name -> stage 2
    error: str | None = None


def _improved_symbols(reps: Dict[str, str]) -> Dict[str, dict]:
    """symbol-map style {orig_name: {new_name, signature}} from improved bodies,
    so stage 2 can inject callee signatures exactly as the production pipeline
    does (`pipeline._callee_context`)."""
    syms: Dict[str, dict] = {}
    for name, code in reps.items():
        symbol_map.record_improvement(syms, roundtrip._normalize_name(name), code)
    return syms


def run_chain(program: Program,
              improve_fn: Callable[[str], str],
              consolidate_fn: Callable[[str], str],
              refine_fn: Callable[[str, str, str], str],
              status: Callable[[str], None] | None = None) -> ChainResult:
    """Decompile (cached) -> improve -> synthesize header -> contextual refine."""
    def _log(m):
        if status:
            status(f"[{program.id}] {m}")
    try:
        targets = roundtrip.decompiled_targets(program)
        groups, reps = roundtrip._dedup_targets(targets)
        _log(f"{len(targets)} target(s), {len(reps)} unique")

        # Stage 1: improve each unique body once, fan out to its duplicates.
        improved: Dict[str, str] = {}
        for rep, code in reps.items():
            _log(f"stage1 improve {rep}...")
            imp = improve_fn(code)
            for member in groups[rep]:
                improved[member] = imp

        # Stage 2a: collect struct/class fragments -> synthesize one project.h.
        fragments: List[str] = []
        for rep in reps:
            for item in scanner.scan_code(improved[rep]):
                if item.kind in ("struct", "class"):
                    fragments.append(f"// from {rep}\n{item.body}")
        if fragments:
            _log(f"stage2 synth header from {len(fragments)} fragment(s)...")
            project_h = consolidate_fn("\n\n".join(fragments))
        else:
            project_h = "// (no types extracted)"

        # Stage 2b: refine each unique function with the synthesized header + its
        # callee signatures (reusing the production context builder).
        reps_improved = {rep: improved[rep] for rep in reps}
        syms = _improved_symbols(reps_improved)
        refined: Dict[str, str] = {}
        for rep in reps:
            self_name = roundtrip._normalize_name(rep)
            callees = pipeline._callee_context(improved[rep], syms, self_name)
            _log(f"stage2 refine {rep}...")
            ref = refine_fn(improved[rep], project_h, callees)
            for member in groups[rep]:
                refined[member] = ref

        return ChainResult(program.id, program.source, targets, improved,
                           project_h, refined)
    except Exception as e:
        return ChainResult(program.id, program.source, error=str(e))
