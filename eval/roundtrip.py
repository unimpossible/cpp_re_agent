"""
The round-trip pipeline: compile -> strip(opt) -> decompile -> improve.

Compile + decompile are deterministic in the source (not the prompt), so their
result — the decompiled functions — is cached on disk keyed by the program's
source hash. Only the improve step (and metrics) re-run per experiment.

Decoupled from the LLM: callers pass `improve_fn(code) -> improved_code`, so the
prompt/model/provider live entirely in the experiment runner.
"""
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from cpp_re_agent import decompiler, scanner

from .corpus import Program
from .paths import EXPERIMENTS_DIR, ensure_dir
from . import toolchain

CACHE_ROOT = EXPERIMENTS_DIR / ".cache"

# Decompiled symbols that are CRT/runtime glue, never user code — excluded from
# the fallback target set when name-matching against the original fails.
_CRT_DENYLIST = {
    "_start", "_init", "_fini", "deregister_tm_clones", "register_tm_clones",
    "__do_global_dtors_aux", "frame_dummy", "__libc_csu_init",
    "__libc_csu_fini", "_dl_relocate_static_pie", "__stat",
}


@dataclass
class RoundtripResult:
    program_id: str
    source_hash: str
    original_source: str
    original_func_names: List[str]
    raw_functions: Dict[str, str]       # decompiled targets (name -> code)
    improved_functions: Dict[str, str]  # name -> improved code (deduped reuse)
    raw_source: str                     # concatenated decompiled targets
    improved_source: str                # concatenated UNIQUE improved bodies
    n_targets: int = 0                  # selected target functions
    n_unique: int = 0                   # distinct bodies actually sent to the LLM
    syntax_ok: bool = False
    syntax_stderr: str = ""
    timings: Dict[str, float] = field(default_factory=dict)
    error: str | None = None


def _normalize_name(name: str) -> str:
    """
    Bare identifier for a function, with decompiler decoration removed.

    Kept for display and self-reference checks only. It is NOT an identity:
    `Scheduler::reset` and `MetricsCollector::reset` both normalize to `reset`,
    as do `describe(int)` and `describe(const Summary&)`. Use `match_key` to
    pair functions across the original/decompiled boundary.
    """
    return scanner.normalize_name(name)


def match_key(name: str, code: str = "") -> str:
    """
    Namespace-insensitive identity used to pair an original function with its
    decompiled counterpart: enclosing class + method + arity + parameter types.

    Matching on the bare name instead silently pairs a decompiled function with
    a *different* original of the same name, which then scores recovered code
    against source it was never compiled from.
    """
    return scanner.function_key(name, code).match_key()


def build_match_index(items: Dict[str, str]) -> Dict[str, str]:
    """
    match_key -> name, dropping any key claimed by more than one function.

    A collision here means we cannot tell two functions apart, so neither is
    paired. Losing a sample is a gap in coverage; pairing the wrong one is a
    silently wrong score.
    """
    seen: Dict[str, List[str]] = {}
    for name, code in items.items():
        seen.setdefault(match_key(name, code), []).append(name)
    dropped = {k: v for k, v in seen.items() if len(v) > 1}
    if dropped:
        for key, names in sorted(dropped.items()):
            print(f"[roundtrip] WARNING: ambiguous match key {key!r} shared by "
                  f"{len(names)} functions ({', '.join(sorted(names)[:4])}); skipping")
    return {k: v[0] for k, v in seen.items() if len(v) == 1}


def _select_targets(all_funcs: Dict[str, str], original_names: List[str],
                    original_bodies: Dict[str, str] | None = None) -> Dict[str, str]:
    """
    Pick the decompiled functions that correspond to the original program's
    functions. Primary: identity match (class + name + arity + param types) to
    the original. Fallback (e.g. names lost to stripping): everything that isn't
    CRT glue and is worth improving.
    """
    if original_bodies:
        want = set(build_match_index(original_bodies))
    else:
        want = {match_key(n) for n in original_names}

    matched = {
        name: code for name, code in all_funcs.items()
        if match_key(name, code) in want
    }
    if matched:
        return matched

    # Same identity, but ignoring parameter types — the original and the
    # decompilation can disagree on a type spelling we failed to normalize.
    loose = {scanner.function_key(n).match_key(with_params=False)
             for n in (original_bodies or {})} or {
             scanner.function_key(n).match_key(with_params=False)
             for n in original_names}
    matched = {
        name: code for name, code in all_funcs.items()
        if scanner.function_key(name, code).match_key(with_params=False) in loose
    }
    if matched:
        return matched

    return {
        name: code for name, code in all_funcs.items()
        if name not in _CRT_DENYLIST and not name.startswith("_")
    }


def _body_signature(name: str, code: str) -> str:
    """
    Content fingerprint of a function body, ignoring its own name, hex
    addresses, and whitespace — so two decompiled functions that differ only by
    name/address (true duplicates: thunks, template instantiations) hash equal.
    """
    norm = _normalize_name(name)
    s = code
    if norm:
        s = s.replace(norm, "F")
    # Only collapse *long* hex (>=5 digits = addresses); keep short constants
    # like 0x10 so functions differing by a real constant stay distinct.
    s = re.sub(r"0x[0-9a-fA-F]{5,}", "0", s)
    s = re.sub(r"\b[0-9a-fA-F]{6,}\b", "0", s)   # bare addresses
    s = re.sub(r"\s+", " ", s).strip()
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _dedup_targets(targets: Dict[str, str]) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """
    Group target functions by body fingerprint so identical bodies are improved
    once. Returns (groups: rep_name -> [member names], reps: rep_name -> code).
    Insertion order is preserved; the first member of each group is its rep.
    """
    by_sig: Dict[str, str] = {}          # signature -> rep name
    groups: Dict[str, List[str]] = {}
    reps: Dict[str, str] = {}
    for name, code in targets.items():
        sig = _body_signature(name, code)
        rep = by_sig.get(sig)
        if rep is None:
            by_sig[sig] = name
            groups[name] = [name]
            reps[name] = code
        else:
            groups[rep].append(name)
    return groups, reps


def _decompiled_functions(program: Program) -> Dict[str, str]:
    """Compile + decompile `program`, caching the resulting functions by hash."""
    cache_dir = ensure_dir(CACHE_ROOT / f"{program.id}_{program.source_hash}")
    funcs_json = cache_dir / "raw_functions.json"
    if funcs_json.exists():
        return json.loads(funcs_json.read_text(encoding="utf-8"))

    binary = cache_dir / "bin" / f"{program.id}.bin"
    # strip=False for now: keeping symbols lets us match decompiled functions to
    # the original by name, giving clean per-function signal. Stripping is a
    # planned difficulty axis (see NOTES.md), wired as a future knob.
    res = toolchain.compile_cpp(program.translation_units, binary, strip=False)
    if not res.ok:
        raise RuntimeError(f"compile failed: {res.stderr}")

    decomp_dir = cache_dir / "decomp"
    decompiler.decompile_binary(str(binary), str(decomp_dir))
    if not any(decomp_dir.rglob("*.c")):
        raise RuntimeError("decompilation produced no .c files")

    funcs = decompiler.get_functions(str(decomp_dir))
    funcs_json.write_text(json.dumps(funcs, indent=2), encoding="utf-8")
    return funcs


def original_functions(source: str) -> Dict[str, str]:
    """
    name -> body for every function defined in an original program.

    Keyed by the scanner's name, which can repeat across overloads and classes;
    use `build_match_index` on the result when you need identity.
    """
    out: Dict[str, str] = {}
    for item in scanner.scan_code(source):
        if item.kind != "function":
            continue
        # Overloads share a name, so disambiguate the dict key by identity.
        key = item.name if item.name not in out else f"{item.name}#{match_key(item.name, item.body)}"
        out[key] = item.body
    return out


def decompiled_targets(program: Program) -> Dict[str, str]:
    """
    Public accessor: the selected decompiled target functions for a program
    (compile + decompile cached by source hash). Used to build the per-function
    optimization dataset without running the improve step.
    """
    all_funcs = _decompiled_functions(program)
    originals = original_functions(program.source)
    return _select_targets(all_funcs, list(originals), originals)


def run_roundtrip(program: Program, improve_fn: Callable[[str], str],
                  status: Callable[[str], None] | None = None) -> RoundtripResult:
    """Run one program through compile -> decompile (cached) -> improve."""
    timings: Dict[str, float] = {}
    originals = original_functions(program.source)
    original_names = list(originals)

    def _log(msg):
        if status:
            status(f"[{program.id}] {msg}")

    try:
        t0 = time.time()
        _log("compile + decompile (cached by source hash)...")
        all_funcs = _decompiled_functions(program)
        timings["compile_decompile"] = time.time() - t0

        targets = _select_targets(all_funcs, original_names, originals)
        groups, reps = _dedup_targets(targets)
        dupes = len(targets) - len(reps)
        _log(f"{len(targets)} target function(s); {len(reps)} unique"
             + (f" ({dupes} duplicate(s) skipped)" if dupes else "")
             + f": {', '.join(reps) or '(none)'}")

        # Improve each unique body once, then fan the result back out to every
        # member of its group so duplicates aren't analyzed (or emitted) twice.
        improved: Dict[str, str] = {}
        t1 = time.time()
        for rep, code in reps.items():
            _log(f"improving {rep}...")
            rep_improved = improve_fn(code)
            for member in groups[rep]:
                improved[member] = rep_improved
        timings["improve"] = time.time() - t1

        raw_source = "\n\n".join(targets.values())
        # Concatenate one improved body per unique group (not per target).
        improved_source = "\n\n".join(improved[rep] for rep in reps)

        syntax = toolchain.syntax_check(improved_source, CACHE_ROOT / "_syntax")

        return RoundtripResult(
            program_id=program.id,
            source_hash=program.source_hash,
            original_source=program.source,
            original_func_names=original_names,
            raw_functions=targets,
            improved_functions=improved,
            raw_source=raw_source,
            improved_source=improved_source,
            n_targets=len(targets),
            n_unique=len(reps),
            syntax_ok=syntax.ok,
            syntax_stderr=syntax.stderr,
            timings=timings,
        )
    except Exception as e:
        return RoundtripResult(
            program_id=program.id,
            source_hash=program.source_hash,
            original_source=program.source,
            original_func_names=original_names,
            raw_functions={},
            improved_functions={},
            raw_source="",
            improved_source="",
            timings=timings,
            error=str(e),
        )
