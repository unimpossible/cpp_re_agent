"""
Per-function optimization dataset: pair each decompiled target function with its
original source counterpart, matched by (normalized) name. This is the train/val
signal the reflective optimizer scores prompts against. Compile/decompile is
cached (roundtrip), so building the dataset is cheap after the first warm-up.
"""
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

from cpp_re_agent import scanner

from . import roundtrip
from .corpus import Program, load_corpus


@dataclass
class FunctionExample:
    program_id: str
    name: str          # decompiled name (e.g. gcd-001012e0)
    tier: int
    decompiled: str    # raw decompiled body (also the "raw" for identifier recovery)
    original: str      # original source body of the matched function


def _original_bodies(source: str) -> Dict[str, str]:
    """
    match-key -> original function body.

    Keyed by identity (class + name + arity + param types), not bare name:
    `Scheduler::reset` and `MetricsCollector::reset` are different functions,
    and so are `describe(int)` and `describe(const Summary&)`. Keying on the
    name let one silently overwrite the other, after which every decompiled
    `reset` was scored against whichever original happened to be scanned last.
    """
    functions = roundtrip.original_functions(source)
    index = roundtrip.build_match_index(functions)
    return {key: functions[name] for key, name in index.items()}


def build_function_dataset(programs: List[Program] | None = None,
                           skip_main: bool = True) -> List[FunctionExample]:
    """
    Build examples for all (or given) corpus programs. `main` is skipped by
    default — it is mostly I/O boilerplate and a noisy recovery target.
    """
    programs = programs if programs is not None else load_corpus()
    examples: List[FunctionExample] = []
    for prog in programs:
        originals = _original_bodies(prog.source)
        targets = roundtrip.decompiled_targets(prog)
        for name, decompiled in targets.items():
            if skip_main and roundtrip._normalize_name(name) == "main":
                continue
            original = originals.get(roundtrip.match_key(name, decompiled))
            if not original:
                # Retry ignoring parameter types: the two sides can spell a
                # type differently even when the function is unambiguous.
                loose = {
                    scanner.function_key(k).match_key(with_params=False): v
                    for k, v in originals.items()
                }
                original = loose.get(
                    scanner.function_key(name, decompiled).match_key(with_params=False))
            if not original:
                continue
            examples.append(FunctionExample(
                program_id=prog.id, name=name, tier=prog.tier,
                decompiled=decompiled, original=original,
            ))
    return examples


def split(examples: List[FunctionExample], val_frac: float = 0.4, seed: int = 0
          ) -> Tuple[List[FunctionExample], List[FunctionExample]]:
    """Deterministic train/val split, stratified loosely by shuffling with seed."""
    rng = random.Random(seed)
    shuffled = examples[:]
    rng.shuffle(shuffled)
    n_val = max(1, int(len(shuffled) * val_frac))
    return shuffled[n_val:], shuffled[:n_val]
