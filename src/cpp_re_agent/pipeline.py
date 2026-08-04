"""
Headless orchestration of the two-stage RE pipeline, driven by the CLI
(`cpp_re_agent.cli`).

Stage 1 (`run_stage1` / `batch_improve`): decompile a binary, then improve each
function leaves-first using the callgraph order + the shared signature map.

Stage 2 (`run_stage2`): synthesize a `project.h` from the improved types, then
refine each function with that header + its callees' signatures as context.

Deliberately DB-free: type definitions and callee context are gathered by
scanning the improved files directly (`scanner` / `symbol_map`) and merged via
the shared `knowledge_graph.consolidate_definitions` / `contextual_improver.
refine_function`. This keeps the pipeline runnable where chromadb's native deps
are unavailable (a blocked grpc DLL on some machines), which is also why the
`eval/` harness uses the same DB-free paths.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import ai_improver, callgraph, contextual_improver, decompiler
from . import knowledge_graph, scanner, symbol_map

# Default task model per provider; the CLI resolves a model from this when the
# user does not pass one explicitly.
DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "local": "openai/gpt-oss-20b"}

# Concurrent LLM calls per callgraph level. LLM latency dominates stage 1, so
# this is the main throughput knob. Local single-GPU endpoints usually serve
# requests near-serially, so `--workers 1` can be the honest setting there.
DEFAULT_WORKERS = 4


def default_model_for(provider: str) -> str:
    return DEFAULT_MODELS.get(provider, "openai/gpt-oss-20b")


def workspace_for(binary_path: str, output_dir: Optional[str] = None) -> Path:
    """Per-binary workspace dir: `<output_dir or cwd/workspace>/<binary name>`."""
    if output_dir:
        return Path(output_dir)
    return Path(os.getcwd()) / "workspace" / Path(binary_path).name


@dataclass
class StageResult:
    workspace: Path
    improved: List[str] = field(default_factory=list)   # functions written this run
    skipped: List[str] = field(default_factory=list)     # already-improved (resumed)
    failed: List[str] = field(default_factory=list)      # errored/rejected; nothing written
    header_written: bool = False
    refined: List[str] = field(default_factory=list)     # contextual pass overwrites
    error: Optional[str] = None


def _noop(_msg: str) -> None:
    pass


# --- Run provenance -------------------------------------------------------

# Which model produced a workspace is not recoverable from the output itself.
# Each stage appends a record here so a workspace (and anything scoring it,
# e.g. the eval harness) can say what produced it.
RUNS_FILE = "runs.json"


def load_runs(workspace: Path) -> List[dict]:
    """Provenance records for a workspace, oldest first ([] if never recorded)."""
    path = Path(workspace) / RUNS_FILE
    if not path.exists():
        return []
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return records if isinstance(records, list) else []


def record_run(workspace: Path, stage: str, provider: str, model_name: str,
               result: StageResult, binary_path: Optional[str] = None) -> dict:
    """Append one stage's provenance to `<workspace>/runs.json`."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": stage,
        "provider": provider,
        "model": model_name,
        "binary": binary_path,
        "improved": len(result.improved),
        "skipped": len(result.skipped),
        "refined": len(result.refined),
        "header_written": result.header_written,
    }
    if provider == "local":
        record["endpoint"] = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1")

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    records = load_runs(workspace) + [record]
    (workspace / RUNS_FILE).write_text(json.dumps(records, indent=2), encoding="utf-8")
    return record


def models_used(workspace: Path) -> List[str]:
    """Distinct models that wrote into a workspace, in first-use order."""
    seen: List[str] = []
    for record in load_runs(workspace):
        model = record.get("model")
        if model and model not in seen:
            seen.append(model)
    return seen


# --- Stage 1: decompile + batch improve -----------------------------------

def batch_improve(functions: Dict[str, str], workspace: Path, binary_path: str,
                  provider: str = "local", model_name: str = "openai/gpt-oss-20b",
                  limit: Optional[int] = None,
                  status: Callable[[str], None] = _noop,
                  on_progress: Optional[Callable[[float], None]] = None,
                  max_workers: int = DEFAULT_WORKERS) -> StageResult:
    """
    Improve a {name: raw_code} mapping leaves-first into `workspace/improved/`,
    accumulating the shared signature map. Resumable: functions already written
    are skipped (but still contribute their signature as callee context).

    LLM latency dominates, so functions are improved concurrently — but only
    *within a callgraph level*. Functions in one level have no dependency on one
    another, while a caller in level N still runs after every callee it needs
    has been improved and recorded. Parallelizing the flat topological order
    instead would break that signature propagation. `max_workers=1` restores the
    strictly sequential path.
    """
    result = StageResult(workspace=workspace)
    improved_dir = workspace / "improved"
    improved_dir.mkdir(parents=True, exist_ok=True)

    # Leaves-first so callers see improved callee signatures (via the symbol
    # map) as context; score only breaks ties so high-value fns go earlier.
    # The callgraph spans all functions so ordering/dependencies stay correct
    # even when `limit` later stops us early.
    graph = callgraph.build_callgraph(functions)
    scores = {n: ai_improver.score_function(c) for n, c in functions.items()}
    order = callgraph.topological_order(graph, priority=scores.get)
    valid_names = set(functions.keys())
    symbols = symbol_map.load_symbols(workspace)

    max_workers = max(1, int(max_workers))
    if max_workers > 1:
        levels = callgraph.topological_levels(graph, priority=scores.get)
    else:
        # Singleton levels == the exact sequential order, unchanged.
        levels = [[name] for name in order]

    total = len(order)
    position = {name: i + 1 for i, name in enumerate(order)}
    processed = 0

    def tick(n: int = 1) -> None:
        nonlocal processed
        processed += n
        if on_progress:
            on_progress(processed / total)

    def improve_one(name: str, base_symbols: Dict[str, dict]):
        """
        Runs in a worker thread: LLM call only. Never touches shared state, the
        filesystem, or `status`; all reporting and writing happens on the
        calling thread, so there is exactly one writer and `status` callbacks
        stay ordered.
        """
        t0 = time.time()
        # Private copy: improve_function may add callee signatures it reads off
        # disk, and dict mutation from several threads is not safe.
        local_symbols = dict(base_symbols)
        code = ai_improver.improve_function(
            functions[name], provider=provider, model_name=model_name,
            binary_path=binary_path, recursive=False,
            symbols=local_symbols, valid_names=valid_names,
        )
        return code, local_symbols, time.time() - t0

    def commit(name: str, outcome, elapsed: float) -> None:
        """Main thread: validate, write, record. `outcome` is code or an exception."""
        tag = f"[{position[name]}/{total}] {name}"
        if isinstance(outcome, BaseException):
            status(f"{tag}: ERROR {outcome}")
            result.failed.append(name)
            return
        # Never cache output we can't parse: the `target.exists()` check above
        # means anything written here is treated as final on every later run,
        # and its bogus signature would propagate to every caller via the
        # symbol map. Same guard as `run_contextual`.
        if not scanner.is_valid_cpp(outcome):
            status(f"{tag}: REJECTED (output does not parse); not written")
            result.failed.append(name)
            return
        (improved_dir / f"{name}.cpp").write_text(outcome, encoding="utf-8")
        symbol_map.record_improvement(symbols, name, outcome)
        status(f"{tag}: done ({elapsed:.0f}s)")
        result.improved.append(name)

    # `limit` caps the number of functions actually improved (LLM calls), not
    # how many we look at: library/stub/already-improved functions are filtered
    # out *before* they count against the budget, so e.g. --limit 40 improves 40
    # real functions rather than burning the budget skipping libstdc++ code.
    stop = False
    for level in levels:
        if stop:
            break

        # Resolve the cheap decisions up front, on this thread, so only real
        # LLM work is scheduled.
        todo: List[str] = []
        for name in level:
            tag = f"[{position[name]}/{total}] {name}"
            target = improved_dir / f"{name}.cpp"
            if target.exists():
                # Resume: keep the signature available to later callers.
                if name not in symbols:
                    symbol_map.record_improvement(
                        symbols, name, target.read_text(encoding="utf-8"))
                status(f"{tag}: already improved (skip)")
                result.skipped.append(name)
                tick()
            elif ai_improver.should_improve(functions[name], name=name):
                todo.append(name)
            else:
                status(f"{tag}: too simple / named (skip)")
                result.skipped.append(name)
                tick()

        if limit is not None:
            budget = limit - len(result.improved)
            if budget <= 0:
                status(f"reached --limit ({limit}); stopping")
                break
            if len(todo) > budget:
                todo = todo[:budget]
                stop = True

        if not todo:
            continue

        workers = min(max_workers, len(todo))
        if workers == 1:
            for name in todo:
                status(f"[{position[name]}/{total}] {name}: improving (LLM)...")
                try:
                    code, worker_symbols, elapsed = improve_one(name, symbols)
                    outcome = code
                except Exception as e:  # one bad function shouldn't kill the batch
                    outcome, worker_symbols, elapsed = e, {}, 0.0
                for key, value in worker_symbols.items():
                    symbols.setdefault(key, value)
                commit(name, outcome, elapsed)
                tick()
        else:
            status(f"level of {len(todo)} independent function(s); {workers} workers")
            # Snapshot the symbol map for the whole level: nothing in this level
            # depends on anything else in it, so no worker can miss a signature
            # it needed, and no worker observes another's partial writes.
            snapshot = dict(symbols)
            pending = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for name in todo:
                    pending[pool.submit(improve_one, name, snapshot)] = name
                for future in as_completed(pending):
                    name = pending[future]
                    try:
                        code, worker_symbols, elapsed = future.result()
                        outcome = code
                    except Exception as e:
                        outcome, worker_symbols, elapsed = e, {}, 0.0
                    for key, value in worker_symbols.items():
                        symbols.setdefault(key, value)
                    commit(name, outcome, elapsed)
                    tick()

        if stop:
            status(f"reached --limit ({limit}); stopping")

    # Completion order varies with concurrency; report in planned order so runs
    # stay comparable.
    result.improved.sort(key=position.__getitem__)
    result.failed.sort(key=position.__getitem__)
    result.skipped.sort(key=position.__getitem__)

    if on_progress:
        on_progress(1.0)
    symbol_map.save_symbols(workspace, symbols)
    return result


def run_stage1(binary_path: str, provider: str = "local",
               model_name: str = "openai/gpt-oss-20b",
               output_dir: Optional[str] = None, limit: Optional[int] = None,
               decompile: bool = True, max_workers: int = DEFAULT_WORKERS,
               status: Callable[[str], None] = print) -> StageResult:
    """Decompile `binary_path` (cached/skipped if present) then batch-improve."""
    workspace = workspace_for(binary_path, output_dir)
    raw_dir = workspace / "raw"

    if decompile:
        status(f"decompiling {binary_path} -> {raw_dir} ...")
        decompiler.decompile_binary(binary_path, str(raw_dir))

    functions = decompiler.get_functions(str(raw_dir))
    if not functions:
        # Reaching here means ghidrecomp exited 0 (or the output was cached)
        # but produced nothing — worth saying out loud rather than reporting a
        # clean run over zero functions.
        status(f"WARNING: no decompiled functions found in {raw_dir}; nothing to improve")
    status(f"{len(functions)} function(s) decompiled; improving...")
    result = batch_improve(functions, workspace, binary_path, provider, model_name,
                           limit=limit, status=status, max_workers=max_workers)
    record_run(workspace, "stage1", provider, model_name, result, binary_path)
    return result


# --- Stage 2: header synthesis + contextual refinement --------------------

_TYPE_KINDS = ("struct", "class", "declaration")


def _collect_definitions(improved_dir: Path) -> str:
    """Concatenate the struct/class/declaration fragments across improved files."""
    defs: List[str] = []
    for fpath in sorted(improved_dir.glob("*.cpp")):
        code = fpath.read_text(encoding="utf-8")
        try:
            items = scanner.scan_code(code)
        except Exception:
            continue
        for item in items:
            if item.kind in _TYPE_KINDS:
                defs.append(f"// From {fpath.name}\n{item.body}")
    return "\n\n".join(defs)


def synthesize_header(workspace: Path, provider: str = "local",
                      model_name: str = "openai/gpt-oss-20b",
                      status: Callable[[str], None] = print) -> Optional[str]:
    """
    Merge the type fragments scattered across the improved files into one
    `project.h` (DB-free; uses `knowledge_graph.consolidate_definitions`).
    Returns the header text, or None if there were no types to consolidate.
    """
    improved_dir = workspace / "improved"
    if not improved_dir.exists():
        status("no improved/ dir — run stage 1 first")
        return None

    all_defs = _collect_definitions(improved_dir)
    if not all_defs.strip():
        status("no struct/class definitions found across improved files")
        return None

    status("consolidating types into project.h (LLM)...")
    header = knowledge_graph.consolidate_definitions(all_defs, provider, model_name)
    out = workspace / "project.h"
    out.write_text(header, encoding="utf-8")
    status(f"wrote {out}")
    return header


def _callee_context(code: str, symbols: Dict[str, dict], self_name: str) -> str:
    """
    Signatures of the functions `code` calls, drawn from the improved signature
    map. Matches improved (renamed) callees by new name; falls back to all
    sibling signatures when no direct callee is found (mirrors the eval harness).
    """
    by_new = {e["new_name"]: e["signature"] for e in symbols.values()}
    deps: set = set()
    try:
        for item in scanner.scan_code(code):
            if item.kind == "function":
                deps = set(item.dependencies)
                break
    except Exception:
        pass

    lines = [f"{by_new[d]};" for d in sorted(deps)
             if d in by_new and d != self_name]
    if not lines:  # no resolved callees — give siblings as general context
        lines = [f"{e['signature']};" for k, e in symbols.items()
                 if e["new_name"] != self_name and k != self_name]
    return "\n".join(lines) if lines else "// (no related functions)"


def run_contextual(workspace: Path, provider: str = "local",
                   model_name: str = "openai/gpt-oss-20b",
                   status: Callable[[str], None] = print) -> List[str]:
    """
    Refine every improved function in place with `project.h` + callee context.
    Skips files whose refinement errored or no longer parses (keeps the prior
    version rather than overwriting with garbage).
    """
    improved_dir = workspace / "improved"
    files = sorted(improved_dir.glob("*.cpp"))
    if not files:
        status("no improved files to refine")
        return []

    project_h = workspace / "project.h"
    header = project_h.read_text(encoding="utf-8") if project_h.exists() \
        else "// project.h not found"
    symbols = symbol_map.load_symbols(workspace)

    refined: List[str] = []
    total = len(files)
    for i, fpath in enumerate(files):
        code = fpath.read_text(encoding="utf-8")
        callees = _callee_context(code, symbols, fpath.stem)
        status(f"[{i + 1}/{total}] {fpath.stem}: contextual refine (LLM)...")
        t0 = time.time()
        try:
            new_code = contextual_improver.refine_function(
                code, header, callees, provider=provider, model_name=model_name)
        except Exception as e:  # one bad function shouldn't kill the pass
            status(f"[{i + 1}/{total}] {fpath.stem}: ERROR {e} (kept prior)")
            continue
        if not scanner.is_valid_cpp(new_code):
            status(f"[{i + 1}/{total}] {fpath.stem}: refinement rejected (kept prior)")
            continue
        fpath.write_text(new_code, encoding="utf-8")
        status(f"[{i + 1}/{total}] {fpath.stem}: refined ({time.time() - t0:.0f}s)")
        refined.append(fpath.stem)

    return refined


def run_stage2(workspace: Path, provider: str = "local",
               model_name: str = "openai/gpt-oss-20b", contextual: bool = True,
               status: Callable[[str], None] = print) -> StageResult:
    """Header synthesis, then (optionally) the contextual refinement pass."""
    result = StageResult(workspace=workspace)
    header = synthesize_header(workspace, provider, model_name, status=status)
    result.header_written = header is not None
    if contextual:
        result.refined = run_contextual(workspace, provider, model_name, status=status)
    record_run(workspace, "stage2", provider, model_name, result)
    return result


# --- Both stages ----------------------------------------------------------

def run_all(binary_path: str, provider: str = "local",
            model_name: Optional[str] = None, output_dir: Optional[str] = None,
            limit: Optional[int] = None, do_synthesis: bool = True,
            do_contextual: bool = True, max_workers: int = DEFAULT_WORKERS,
            status: Callable[[str], None] = print) -> StageResult:
    """
    Run the whole pipeline against `binary_path` autonomously and return a
    `StageResult`. `do_synthesis=False` stops after stage 1; `do_contextual=
    False` runs header synthesis but skips the per-function refinement pass.
    """
    model_name = model_name or default_model_for(provider)

    status("=== Stage 1: decompile + improve ===")
    s1 = run_stage1(binary_path, provider, model_name, output_dir, limit,
                    max_workers=max_workers, status=status)
    workspace = s1.workspace

    if not do_synthesis:
        status(f"stage 1 complete: {len(s1.improved)} improved, "
               f"{len(s1.skipped)} skipped, {len(s1.failed)} failed -> {workspace}")
        return s1

    status("=== Stage 2: header synthesis + contextual refine ===")
    s2 = run_stage2(workspace, provider, model_name, contextual=do_contextual, status=status)

    s1.header_written = s2.header_written
    s1.refined = s2.refined
    status(f"done: {len(s1.improved)} improved, header={'yes' if s2.header_written else 'no'}, "
           f"{len(s2.refined)} refined -> {workspace}")
    return s1
