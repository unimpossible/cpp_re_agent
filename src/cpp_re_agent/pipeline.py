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
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import ai_improver, callgraph, contextual_improver, decompiler
from . import knowledge_graph, namer, scanner, symbol_map
from .progress import Progress

# Default task model per provider; the CLI resolves a model from this when the
# user does not pass one explicitly.
DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "local": "openai/gpt-oss-20b"}

# Concurrent LLM calls per callgraph level. LLM latency dominates stage 1, so
# this is the main throughput knob. Local single-GPU endpoints usually serve
# requests near-serially, so `--workers 1` can be the honest setting there.
DEFAULT_WORKERS = 4

# A stripped binary has no symbol table, so Ghidra names almost everything
# FUN_<addr> and the name-based library filter in `ai_improver.should_improve`
# stops working: on a real corpus binary it went from keeping 67 functions to
# keeping 518, i.e. ~80% of the LLM budget spent on statically linked
# libstdc++. Call depth from the entry point is what still separates the two —
# measured on tier4_taskflow, filtering to depth <= 2 lifted precision from
# 18% to 64%. Applied only when the binary looks stripped; with symbols the
# name filter is already accurate and depth would just lose work.
STRIPPED_NAME_RATIO = 0.5      # share of FUN_-named functions that means "stripped"
DEFAULT_MAX_DEPTH = 2          # hops from the entry point to keep, when stripped
# Depth only means something if the entry actually reaches the program. When
# the call graph is sparse — indirect/virtual calls, a scanner that failed, a
# handful of unrelated functions — the "entry" reaches almost nothing and
# filtering by depth would discard nearly everything. Better to spend a wider
# budget than to silently drop the work.
MIN_ENTRY_REACH = 0.5


def _noop(_msg: str) -> None:
    pass


def looks_stripped(functions: Dict[str, str]) -> bool:
    """True when most functions carry no symbol, i.e. Ghidra invented the name."""
    if not functions:
        return False
    unnamed = sum(1 for n in functions if n.startswith("FUN_"))
    return unnamed / len(functions) >= STRIPPED_NAME_RATIO


def in_scope_functions(functions: Dict[str, str], graph: Dict[str, set],
                       max_depth: Optional[int] = None,
                       status: Callable[[str], None] = _noop) -> Optional[set]:
    """
    The functions close enough to an entry point to be worth improving, or
    None when no depth filter applies.

    `max_depth` of None means "decide from the binary" — a depth limit only
    when it looks stripped. 0 means "no filter", explicitly, however the binary
    looks.

    Split out so the eval harness can score exactly the selection the pipeline
    makes (`eval.stripped`) instead of a copy of this logic that would drift.
    """
    if max_depth == 0:
        return None
    depth_limit = max_depth
    if depth_limit is None and looks_stripped(functions):
        depth_limit = DEFAULT_MAX_DEPTH
    if depth_limit is None:
        return None

    entries = callgraph.entry_points(graph, dominance=MIN_ENTRY_REACH)
    covered: set = set()
    for e in entries:
        covered |= callgraph.reachable_from(graph, e)

    if not entries:
        status("could not identify an entry point; depth filter skipped")
        return None
    if len(covered) < MIN_ENTRY_REACH * len(functions):
        status(f"call graph too sparse for a depth filter "
               f"({len(entries)} entry point(s) reach {len(covered)} of "
               f"{len(functions)}); improving on name/shape alone")
        return None

    depths = callgraph.call_depths(graph, entries)
    in_scope = {n for n, d in depths.items() if d <= depth_limit}
    where = (entries[0] if len(entries) == 1
             else f"{len(entries)} exported/entry function(s)")
    status(f"no symbols: keeping functions within {depth_limit} call(s) "
           f"of {where} — {len(in_scope)} of {len(functions)}")
    return in_scope


def selected_functions(functions: Dict[str, str],
                       max_depth: Optional[int] = None,
                       graph: Optional[Dict[str, set]] = None,
                       status: Callable[[str], None] = _noop) -> List[str]:
    """
    Every function stage 1 would send to the LLM for a fresh workspace.

    The selection the eval measures, without decompiling or calling anything.
    """
    if graph is None:
        graph = callgraph.build_callgraph(functions)
    in_scope = in_scope_functions(functions, graph, max_depth, status)
    return [n for n, code in functions.items()
            if (in_scope is None or n in in_scope)
            and ai_improver.should_improve(code, name=n)]


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
    named: Dict[str, str] = field(default_factory=dict)  # placeholder -> recovered name
    error: Optional[str] = None


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
                  max_workers: int = DEFAULT_WORKERS,
                  progress: Optional[Progress] = None,
                  max_depth: Optional[int] = None) -> StageResult:
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

    # --- plan first ------------------------------------------------------
    # Classify every function before calling the LLM at all. `should_improve`
    # and `target.exists()` are local and cheap, and knowing the real total up
    # front is what lets progress report an honest "18/76" instead of counting
    # against every decompiled symbol (most of which are library noise).
    # Without symbols the name-based library filter is blind, so fall back to
    # the shape of the call graph: keep only what sits near the entry point.
    in_scope = in_scope_functions(functions, graph, max_depth, status)

    # The naming pass renames improved files, so `<raw name>.cpp` may now be
    # `search_container.cpp`. Without this the resume check would miss it and
    # improve the function again, leaving two copies of the same code.
    renamed_files = namer.load_file_map(workspace)

    plan: List[List[str]] = []
    for level in levels:
        todo: List[str] = []
        for name in level:
            target = improved_dir / f"{name}.cpp"
            if not target.exists() and name in renamed_files:
                target = improved_dir / f"{renamed_files[name]}.cpp"
            if target.exists():
                # Resume: keep the signature available to later callers.
                if name not in symbols:
                    symbol_map.record_improvement(
                        symbols, name, target.read_text(encoding="utf-8"))
                result.skipped.append(name)
            elif in_scope is not None and name not in in_scope:
                result.skipped.append(name)      # too deep to be the user's code
            elif ai_improver.should_improve(functions[name], name=name):
                todo.append(name)
            else:
                result.skipped.append(name)
        plan.append(todo)

    # `limit` caps the number of functions actually improved (LLM calls), not
    # how many we look at: library/stub/already-improved functions are filtered
    # out *before* they count against the budget, so e.g. --limit 40 improves 40
    # real functions rather than burning the budget skipping libstdc++ code.
    if limit is not None:
        budget, capped = limit, []
        for todo in plan:
            capped.append(todo[:budget])
            budget -= len(capped[-1])
        plan = capped

    # Re-level over just the work that survived the filters. Whole-graph levels
    # scatter the survivors across levels that skipped functions occupy, which
    # serializes calls that have no dependency on each other at all.
    planned = [n for todo in plan for n in todo]
    if max_workers > 1 and planned:
        plan = callgraph.levels_over_subset(graph, set(planned),
                                            priority=scores.get)
        planned = [n for todo in plan for n in todo]
    total = len(planned)
    resumed = len(result.skipped)

    already = sum(1 for n in result.skipped if (improved_dir / f"{n}.cpp").exists())
    status(f"{len(functions)} function(s): {total} to improve, "
           f"{already} already done, {resumed - already} filtered out "
           f"(library/stub)")
    if total:
        sizes = [len(todo) for todo in plan if todo]
        status(f"{len(sizes)} callgraph level(s) {sizes}; "
               f"up to {min(max_workers, max(sizes))} in parallel")

    if on_progress:
        on_progress(0.0)
    if progress is None:
        progress = Progress(total, enabled=False)
    progress.total = total

    # --- execute ---------------------------------------------------------

    def improve_one(name: str, base_symbols: Dict[str, dict]):
        """
        Runs in a worker thread. Reports its own start so a stalled level names
        what is in flight; everything that mutates shared state or the
        filesystem still happens on the calling thread.
        """
        progress.start(name)
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
        if isinstance(outcome, BaseException):
            progress.finish(name, elapsed, ok=False, detail=str(outcome)[:120])
            result.failed.append(name)
            return
        # Never cache output we can't parse: the `target.exists()` check above
        # means anything written here is treated as final on every later run,
        # and its bogus signature would propagate to every caller via the
        # symbol map. Same guard as `run_contextual`.
        if not scanner.is_valid_cpp(outcome):
            progress.finish(name, elapsed, ok=False,
                            detail="output does not parse; not written")
            result.failed.append(name)
            return
        (improved_dir / f"{name}.cpp").write_text(outcome, encoding="utf-8")
        symbol_map.record_improvement(symbols, name, outcome)
        progress.finish(name, elapsed, ok=True)
        result.improved.append(name)

    processed = 0

    def tick() -> None:
        nonlocal processed
        processed += 1
        if on_progress and total:
            on_progress(processed / total)

    for depth, todo in enumerate(plan, start=1):
        if not todo:
            continue

        workers = min(max_workers, len(todo))
        progress.note(f"level {depth}/{len(plan)}: {len(todo)} function(s), "
                      f"{workers} worker(s)")

        if workers == 1:
            for name in todo:
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

    if limit is not None and len(result.improved) >= limit:
        status(f"reached --limit ({limit})")

    # Completion order varies with concurrency; report in planned order so runs
    # stay comparable.
    position = {name: i for i, name in enumerate(order)}
    for bucket in (result.improved, result.failed, result.skipped):
        bucket.sort(key=lambda n: position.get(n, len(order)))

    if on_progress:
        on_progress(1.0)
    symbol_map.save_symbols(workspace, symbols)
    return result


def run_stage1(binary_path: str, provider: str = "local",
               model_name: str = "openai/gpt-oss-20b",
               output_dir: Optional[str] = None, limit: Optional[int] = None,
               decompile: bool = True, max_workers: int = DEFAULT_WORKERS,
               progress: Optional[Progress] = None,
               max_depth: Optional[int] = None,
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
                           limit=limit, status=status, max_workers=max_workers,
                           progress=progress, max_depth=max_depth)
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


_CONSOLIDATE_ATTEMPTS = 3
_CONSOLIDATE_BACKOFF = 15.0  # seconds between retries


def _consolidate_with_retries(all_defs: str, provider: str, model_name: str,
                              status: Callable[[str], None]) -> Optional[str]:
    """
    Calls `knowledge_graph.consolidate_definitions`, retrying on any exception
    (timeouts, transient HTTP errors) with a visible log per attempt.

    `get_llm` already sets `max_retries=2` on the underlying LangChain client,
    but that retries silently inside a single call — from the outside a slow
    or wedged provider just looks like a long, unexplained hang. This adds an
    outer retry loop with logging so a failure here is visible and doesn't
    take the whole run down with it.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, _CONSOLIDATE_ATTEMPTS + 1):
        t0 = time.time()
        try:
            header = knowledge_graph.consolidate_definitions(all_defs, provider, model_name)
        except Exception as e:
            last_err = e
            status(f"header synthesis attempt {attempt}/{_CONSOLIDATE_ATTEMPTS} failed "
                   f"after {time.time() - t0:.1f}s: {e!r}")
            if attempt < _CONSOLIDATE_ATTEMPTS:
                status(f"retrying header synthesis in {_CONSOLIDATE_BACKOFF:.0f}s...")
                time.sleep(_CONSOLIDATE_BACKOFF)
            continue
        status(f"header synthesis succeeded on attempt {attempt}/{_CONSOLIDATE_ATTEMPTS} "
               f"({time.time() - t0:.1f}s)")
        return header

    status(f"header synthesis failed after {_CONSOLIDATE_ATTEMPTS} attempts "
           f"({last_err!r}); skipping project.h — stage 2 will continue without it")
    return None


def synthesize_header(workspace: Path, provider: str = "local",
                      model_name: str = "openai/gpt-oss-20b",
                      status: Callable[[str], None] = print) -> Optional[str]:
    """
    Merge the type fragments scattered across the improved files into one
    `project.h` (DB-free; uses `knowledge_graph.consolidate_definitions`).
    Returns the header text, or None if there were no types to consolidate or
    consolidation failed after retries (logged; does not raise).
    """
    improved_dir = workspace / "improved"
    if not improved_dir.exists():
        status("no improved/ dir — run stage 1 first")
        return None

    all_defs = _collect_definitions(improved_dir)
    if not all_defs.strip():
        status("no struct/class definitions found across improved files")
        return None

    fragments = all_defs.count("// From ")
    status(f"consolidating {fragments} type fragment(s) "
           f"({len(all_defs):,} chars) into project.h — one LLM call...")
    header = _consolidate_with_retries(all_defs, provider, model_name, status)
    if header is None:
        return None
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
                   status: Callable[[str], None] = print,
                   progress: Optional[Progress] = None,
                   max_workers: int = DEFAULT_WORKERS) -> List[str]:
    """
    Refine every improved function in place with `project.h` + callee context.
    Skips files whose refinement errored or no longer parses (keeps the prior
    version rather than overwriting with garbage).

    Runs at full width, with no ordering constraint — unlike stage 1, which
    has to go leaves-first. Everything shared here (`project.h` and the
    stage-1 signature map) is fixed before the pass begins, and no refinement
    feeds another, so there is nothing for one worker to wait on. Each writes
    only its own file.
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
    workers = max(1, min(int(max_workers), total))
    status(f"refining {total} improved function(s) against project.h"
           + (f" ({workers} in parallel)" if workers > 1 else ""))
    if progress is None:
        progress = Progress(total, enabled=False)
    progress.total = total

    def refine_one(fpath: Path):
        """Worker: read + LLM call only; writing stays on the calling thread."""
        code = fpath.read_text(encoding="utf-8")
        callees = _callee_context(code, symbols, fpath.stem)
        progress.start(fpath.stem)
        t0 = time.time()
        new_code = contextual_improver.refine_function(
            code, header, callees, provider=provider, model_name=model_name)
        return new_code, time.time() - t0

    def commit(fpath: Path, outcome, elapsed: float) -> None:
        if isinstance(outcome, BaseException):
            progress.finish(fpath.stem, elapsed, ok=False,
                            detail=f"{str(outcome)[:100]} (kept prior)")
            return
        if not scanner.is_valid_cpp(outcome):
            progress.finish(fpath.stem, elapsed, ok=False,
                            detail="does not parse (kept prior)")
            return
        fpath.write_text(outcome, encoding="utf-8")
        progress.finish(fpath.stem, elapsed, ok=True)
        refined.append(fpath.stem)

    if workers == 1:
        for fpath in files:
            try:
                outcome, elapsed = refine_one(fpath)
            except Exception as e:  # one bad function shouldn't kill the pass
                outcome, elapsed = e, 0.0
            commit(fpath, outcome, elapsed)
    else:
        pending = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for fpath in files:
                pending[pool.submit(refine_one, fpath)] = fpath
            for future in as_completed(pending):
                fpath = pending[future]
                try:
                    outcome, elapsed = future.result()
                except Exception as e:
                    outcome, elapsed = e, 0.0
                commit(fpath, outcome, elapsed)

    # Completion order varies with concurrency; report in file order so runs
    # stay comparable.
    refined.sort()
    return refined


def run_naming(workspace: Path, provider: str = "local",
               model_name: str = "openai/gpt-oss-20b",
               status: Callable[[str], None] = print,
               progress: Optional[Progress] = None,
               max_workers: int = DEFAULT_WORKERS) -> Dict[str, str]:
    """
    Name every function still carrying a decompiler placeholder, then apply
    those names across the whole workspace.

    Runs last because naming needs the most context: `project.h`, every
    improved body, and a call graph over recovered code. Within that, it goes
    leaves-first — a caller is much easier to name once you can see it calls
    `reset_metrics` and `find_task_by_id` rather than two hex addresses.

    Returns the applied {old name: new name} mapping.
    """
    improved_dir = workspace / "improved"
    files = sorted(improved_dir.glob("*.cpp"))
    if not files:
        status("no improved files to name")
        return {}

    bodies = {f.stem: f.read_text(encoding="utf-8") for f in files}
    symbols = symbol_map.load_symbols(workspace)
    project_h = workspace / "project.h"
    header = project_h.read_text(encoding="utf-8") if project_h.exists() else ""

    # What each file's function is currently called, which is what appears at
    # the call sites and therefore what a rename has to replace.
    current: Dict[str, str] = {}
    for stem in bodies:
        entry = symbols.get(stem) or {}
        current[stem] = entry.get("new_name") or namer.strip_address(stem)

    def align_filenames() -> int:
        """Make each file's name match the function inside it."""
        wanted = {stem: name for stem, name in current.items()
                  if name != stem and not namer.is_synthetic(name)}
        moved = namer.rename_files(improved_dir, wanted)
        if wanted:
            namer.save_names(workspace, {}, files=wanted)
        return moved

    todo = [stem for stem, name in current.items() if namer.is_synthetic(name)]
    if not todo:
        moved = align_filenames()
        status(f"every function already has a name"
               + (f"; renamed {moved} file(s) to match" if moved else ""))
        return {}

    status(f"naming {len(todo)} of {len(bodies)} function(s) still unnamed")
    if progress is None:
        progress = Progress(len(todo), enabled=False)
    progress.total = len(todo)

    graph = callgraph.build_callgraph(bodies)
    callers = callgraph.callers_of(graph)
    # Leaves-first either way: naming a caller is much easier once its callees
    # have real names, so the sequential path must keep that order too — it is
    # the whole reason this pass walks the graph rather than the file list.
    levels = callgraph.levels_over_subset(graph, set(todo))
    if max_workers <= 1:
        levels = [[stem] for level in levels for stem in level]

    taken = namer.existing_names(symbols, extra=current.values())
    renames: Dict[str, str] = {}

    def propose(stem: str):
        progress.start(current[stem])
        t0 = time.time()
        name = namer.propose_name(
            bodies[stem], header,
            callees=[current.get(d, d) for d in graph.get(stem, ())],
            callers=[current.get(c, c) for c in callers.get(stem, ())],
            provider=provider, model_name=model_name)
        return name, time.time() - t0

    def commit(stem: str, outcome, elapsed: float) -> None:
        old = current[stem]
        if isinstance(outcome, BaseException):
            progress.finish(old, elapsed, ok=False, detail=str(outcome)[:100])
            return
        if not outcome:
            progress.finish(old, elapsed, ok=False, detail="model declined to name it")
            return
        # Resolve against everything already in use, including names chosen
        # earlier in this same pass.
        final = namer.unique_name(outcome, taken)
        if not final:
            progress.finish(old, elapsed, ok=False,
                            detail=f"rejected proposal {outcome!r}")
            return
        taken.add(final)
        renames[old] = final
        current[stem] = final
        progress.finish(old, elapsed, ok=True, detail=f"-> {final}")

    for level in levels:
        if not level:
            continue
        workers = max(1, min(max_workers, len(level)))
        if workers == 1:
            for stem in level:
                try:
                    outcome, elapsed = propose(stem)
                except Exception as e:
                    outcome, elapsed = e, 0.0
                commit(stem, outcome, elapsed)
        else:
            pending = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for stem in level:
                    pending[pool.submit(propose, stem)] = stem
                for future in as_completed(pending):
                    stem = pending[future]
                    try:
                        outcome, elapsed = future.result()
                    except Exception as e:
                        outcome, elapsed = e, 0.0
                    commit(stem, outcome, elapsed)

        # No need to rewrite files between levels: the next level's prompts
        # take callee names from `current`, which `commit` has already updated.
        # Files are rewritten once, at the end.

    touched = namer.apply_renames(improved_dir, renames) if renames else 0

    # Align every filename with the name of the function inside it — including
    # functions stage 1 named, whose file kept the placeholder stem. On the
    # tier4 workspace that was 17 files reading FUN_0010ca7a.cpp while the code
    # inside was already `SearchContainerForValue`.
    moved = align_filenames()

    if renames:
        namer.save_names(workspace, renames)
        # Keep the signature map consistent with what the files now say.
        for stem, entry in symbols.items():
            new_name = entry.get("new_name")
            if new_name in renames:
                entry["new_name"] = renames[new_name]
                entry["signature"] = _word_sub(entry.get("signature", ""),
                                               new_name, renames[new_name])
        symbol_map.save_symbols(workspace, symbols)
        status(f"named {len(renames)} function(s); rewrote {touched} file(s), "
               f"renamed {moved}")
    elif moved:
        status(f"no new names recovered; renamed {moved} file(s) to match "
               f"their function")
    else:
        status("no names could be recovered")
    return renames


def _word_sub(text: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, text) if text and old else text


def run_stage2(workspace: Path, provider: str = "local",
               model_name: str = "openai/gpt-oss-20b", contextual: bool = True,
               status: Callable[[str], None] = print,
               progress: Optional[Progress] = None,
               max_workers: int = DEFAULT_WORKERS) -> StageResult:
    """Header synthesis, then (optionally) the contextual refinement pass."""
    result = StageResult(workspace=workspace)
    header = synthesize_header(workspace, provider, model_name, status=status)
    result.header_written = header is not None
    if contextual:
        result.refined = run_contextual(workspace, provider, model_name,
                                        status=status, progress=progress,
                                        max_workers=max_workers)
    record_run(workspace, "stage2", provider, model_name, result)
    return result


# --- Both stages ----------------------------------------------------------

def run_all(binary_path: str, provider: str = "local",
            model_name: Optional[str] = None, output_dir: Optional[str] = None,
            limit: Optional[int] = None, do_synthesis: bool = True,
            do_contextual: bool = True, do_naming: bool = True,
            max_workers: int = DEFAULT_WORKERS,
            max_depth: Optional[int] = None,
            status: Callable[[str], None] = print,
            progress_factory: Optional[Callable[[str], Progress]] = None) -> StageResult:
    """
    Run the whole pipeline against `binary_path` autonomously and return a
    `StageResult`. `do_synthesis=False` stops after stage 1; `do_contextual=
    False` runs header synthesis but skips the per-function refinement pass.
    """
    model_name = model_name or default_model_for(provider)

    status("=== Stage 1: decompile + improve ===")
    # A fresh bar per stage: the two stages have different totals, and one bar
    # spanning both would jump backwards when stage 2 starts.
    def bar(label: str) -> Progress:
        return (progress_factory(label) if progress_factory
                else Progress(0, enabled=False))

    with bar("stage 1") as p1:
        s1 = run_stage1(binary_path, provider, model_name, output_dir, limit,
                        max_workers=max_workers, status=status, progress=p1,
                        max_depth=max_depth)
    workspace = s1.workspace

    if not do_synthesis:
        status(f"stage 1 complete: {len(s1.improved)} improved, "
               f"{len(s1.skipped)} skipped, {len(s1.failed)} failed -> {workspace}")
        return s1

    status("=== Stage 2: header synthesis + contextual refine ===")
    with bar("stage 2") as p2:
        s2 = run_stage2(workspace, provider, model_name, contextual=do_contextual,
                        status=status, progress=p2, max_workers=max_workers)

    if do_naming:
        status("=== Stage 3: name the functions that still have none ===")
        with bar("naming") as p3:
            s1.named = run_naming(workspace, provider, model_name, status=status,
                                  progress=p3, max_workers=max_workers)

    s1.header_written = s2.header_written
    s1.refined = s2.refined
    status(f"done: {len(s1.improved)} improved, header={'yes' if s2.header_written else 'no'}, "
           f"{len(s2.refined)} refined -> {workspace}")
    return s1
