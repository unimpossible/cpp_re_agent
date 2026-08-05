"""
Command-line entry point for the RE pipeline — the only interface.

Stage 1 decompiles the binary and improves each function leaves-first, so a
caller is improved after the callees it depends on. Stage 2 synthesizes a
`project.h` from the recovered types and refines every function against it.

    cpp-re path/to/binary
    cpp-re path/to/binary --dry-run              # what would run, no LLM calls
    cpp-re path/to/binary --stages improve       # stage 1 only
    cpp-re path/to/binary --provider gemini --workers 8

Also runnable as `python -m cpp_re_agent`.
"""
import argparse
import json
import os
import sys

from . import decompiler, pipeline
from .llm_factory import get_llm
from .progress import Progress

__version__ = "0.1.0"

STAGES = {
    "all": "improve + synthesize project.h + contextual refine + name",
    "header": "improve + synthesize project.h (no refine pass)",
    "improve": "stage 1 only: per-function improvement",
    "name": "only the final naming pass, over an existing workspace",
}


def _positive(name):
    """argparse type for a strictly positive integer, with a useful message."""
    def parse(text):
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be an integer, got {text!r}")
        if value < 1:
            raise argparse.ArgumentTypeError(f"{name} must be >= 1, got {value}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    stage_help = "  ".join(f"{k} ({v})" for k, v in STAGES.items())
    p = argparse.ArgumentParser(
        prog="cpp-re",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Decompile a binary and improve it back toward readable C++.",
        epilog=(
            "examples:\n"
            "  cpp-re examples/sample_app\n"
            "  cpp-re examples/sample_app --dry-run\n"
            "  cpp-re eval/bin/inventory-O2 --stages improve --limit 10\n"
            "  cpp-re eval/bin/inventory-O2 --provider gemini --workers 8\n"
            "  cpp-re app.bin --output-dir /tmp/ws --json summary.json\n"
            "\n"
            "runs are resumable: functions already written to <workspace>/improved/\n"
            "are skipped, and failures are never written, so a re-run retries them.\n"
        ),
    )
    p.add_argument("binary", help="Path to the target binary.")
    p.add_argument("--version", action="version", version=f"cpp-re {__version__}")

    model = p.add_argument_group("model")
    model.add_argument("--provider", default="local",
                       choices=sorted(pipeline.DEFAULT_MODELS),
                       help="LLM provider (default: local).")
    model.add_argument("--model", default=None,
                       help="Model id (default: per-provider, e.g. "
                            f"{pipeline.DEFAULT_MODELS['local']}).")
    model.add_argument("--workers", type=_positive("--workers"),
                       default=pipeline.DEFAULT_WORKERS,
                       help=f"Concurrent LLM calls per callgraph level (default: "
                            f"{pipeline.DEFAULT_WORKERS}). Use 1 for a local "
                            "endpoint that serves requests serially.")

    work = p.add_argument_group("what to run")
    work.add_argument("--stages", default="all", choices=list(STAGES),
                      help=f"How far to run (default: all). {stage_help}")
    work.add_argument("--limit", type=_positive("--limit"), default=None,
                      help="Cap how many functions are actually improved. "
                           "Library/stub functions are filtered out before "
                           "counting, so --limit 10 means 10 real functions.")
    work.add_argument("--no-naming", action="store_true",
                      help="Skip the final pass that names functions still "
                           "left as FUN_<address>. That pass only runs when "
                           "something is still unnamed, which in practice "
                           "means a stripped binary.")
    work.add_argument("--max-depth", type=int, default=None, metavar="N",
                      help="Only improve functions within N calls of the entry "
                           "point. Defaults to "
                           f"{pipeline.DEFAULT_MAX_DEPTH} for a stripped binary "
                           "(where nothing else separates the program from the "
                           "libstdc++ linked into it) and off when symbols are "
                           "present. Use 0 to disable.")
    work.add_argument("--dry-run", action="store_true",
                      help="Decompile and report what would be improved — "
                           "function counts, callgraph levels, parallel rounds — "
                           "without making a single LLM call.")

    out = p.add_argument_group("output")
    out.add_argument("--output-dir", default=None,
                     help="Workspace dir (default: ./workspace/<binary>).")
    out.add_argument("--json", dest="json_path", default=None,
                     help="Write the run summary to this JSON file.")
    out.add_argument("-q", "--quiet", action="store_true",
                     help="Only print the final summary and errors.")
    return p


def _preflight(provider: str, model: str) -> str | None:
    """
    Returns an error message if the LLM is unreachable.

    Checked before decompiling: Ghidra can run for minutes, and failing after
    that because an API key is missing wastes the whole run.
    """
    try:
        if get_llm(provider, model) is None:
            key = "GEMINI_API_KEY" if provider == "gemini" else "LOCAL_LLM_URL"
            return (f"no LLM client for provider={provider!r} — check {key} "
                    f"in your environment or .env")
    except Exception as e:
        return f"could not initialize provider={provider!r}: {e}"
    return None


def _dry_run(binary: str, output_dir, limit, workers: int, status,
             limit_depth=None) -> int:
    """Decompile (cached), then report the plan without calling an LLM."""
    import math

    from . import ai_improver, callgraph

    workspace = pipeline.workspace_for(binary, output_dir)
    raw_dir = workspace / "raw"
    status(f"decompiling {binary} -> {raw_dir} ...")
    decompiler.decompile_binary(binary, str(raw_dir))
    functions = decompiler.get_functions(str(raw_dir))

    improved_dir = workspace / "improved"
    done = {p.stem for p in improved_dir.glob("*.cpp")} if improved_dir.is_dir() else set()

    graph = callgraph.build_callgraph(functions)
    scores = {n: ai_improver.score_function(c) for n, c in functions.items()}

    # Mirror batch_improve's depth filter, or a dry run of a stripped binary
    # would promise several hundred calls the real run will not make.
    in_scope, depth_note = None, ""
    depth_limit = limit_depth
    if depth_limit is None and pipeline.looks_stripped(functions):
        depth_limit = pipeline.DEFAULT_MAX_DEPTH
    if depth_limit == 0:
        depth_limit = None          # explicitly disabled
    if depth_limit is not None:
        entry = callgraph.primary_entry(graph)
        reach = len(callgraph.reachable_from(graph, entry)) if entry else 0
        if entry and reach >= pipeline.MIN_ENTRY_REACH * len(functions):
            depths = callgraph.call_depths(graph, entry)
            in_scope = {n for n, d in depths.items() if d <= depth_limit}
            depth_note = (f"  (stripped: within {depth_limit} call(s) of "
                          f"{entry})")
        else:
            depth_note = "  (stripped, but call graph too sparse to filter)"

    todo = [n for n, c in functions.items()
            if n not in done and (in_scope is None or n in in_scope)
            and ai_improver.should_improve(c, name=n)]
    levels = [[n for n in level if n in todo]
              for level in callgraph.topological_levels(graph, priority=scores.get)]
    levels = [lv for lv in levels if lv]

    if limit is not None:
        print(f"(--limit {limit} would stop after {limit} of them)")
    rounds = sum(math.ceil(len(lv) / workers) for lv in levels)

    print("-" * 70)
    print(f"decompiled:      {len(functions)}")
    print(f"already improved:{len(done):>4}  (skipped on a real run)")
    print(f"library/stub:    {len(functions) - len(todo) - len(done):>4}  (filtered out)")
    print(f"would improve:   {len(todo):>4}{depth_note}")
    print(f"callgraph levels:{len(levels):>4}  sizes={[len(lv) for lv in levels]}")
    print(f"LLM rounds:      {rounds:>4}  at --workers {workers} "
          f"({len(todo)} sequential)")
    print(f"workspace -> {workspace}")
    if todo:
        preview = ", ".join(sorted(todo, key=lambda n: -scores.get(n, 0))[:8])
        print(f"highest-value first: {preview}{' ...' if len(todo) > 8 else ''}")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if not os.path.exists(args.binary):
        print(f"error: binary not found: {args.binary}", file=sys.stderr)
        return 2

    model = args.model or pipeline.default_model_for(args.provider)
    workspace = pipeline.workspace_for(args.binary, args.output_dir)

    # One bar per stage, created by the pipeline. Held here so `status` can
    # route through it: printing straight to stdout would scribble over the
    # bar's line.
    live: dict = {"bar": None}

    def make_bar(label: str) -> Progress:
        bar = Progress(0, enabled=not args.quiet, label=label)
        live["bar"] = bar
        return bar

    def status(msg):
        if args.quiet:
            return
        bar = live.get("bar")
        if bar is not None and bar.enabled:
            bar.note(msg)
        else:
            print(msg)

    if not args.quiet:
        endpoint = (os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1")
                    if args.provider == "local" else args.provider)
        print("=" * 70)
        print(f"binary     {args.binary}")
        print(f"model      {args.provider}:{model}  ({endpoint})")
        print(f"workspace  {workspace}")
        print(f"stages     {args.stages} — {STAGES[args.stages]}")
        print(f"workers    {args.workers}" + (f"   limit {args.limit}" if args.limit else ""))
        print("=" * 70)

    try:
        if args.dry_run:
            return _dry_run(args.binary, args.output_dir, args.limit,
                            args.workers, status,
                            limit_depth=args.max_depth)

        problem = _preflight(args.provider, model)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 2

        if args.stages == "name":
            # Naming needs nothing but an existing workspace, so it can be
            # re-run on its own without paying for the two LLM stages again.
            if not (workspace / "improved").is_dir():
                print(f"error: no improved/ in {workspace} — run a full pass first",
                      file=sys.stderr)
                return 2
            with make_bar("naming") as bar:
                named = pipeline.run_naming(workspace, provider=args.provider,
                                            model_name=model, status=status,
                                            progress=bar,
                                            max_workers=args.workers)
            print("-" * 70)
            print(f"named {len(named)} function(s)  ->  {workspace}")
            return 0

        result = pipeline.run_all(
            args.binary, provider=args.provider, model_name=model,
            output_dir=args.output_dir, limit=args.limit,
            do_synthesis=args.stages != "improve",
            do_contextual=args.stages == "all",
            do_naming=args.stages == "all" and not args.no_naming,
            max_workers=args.workers,
            max_depth=args.max_depth,
            status=status,
            progress_factory=make_bar,
        )
    except decompiler.DecompilationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted — progress is on disk; re-run to resume.", file=sys.stderr)
        return 130

    print("-" * 70)
    print(f"improved {len(result.improved)}   skipped {len(result.skipped)}   "
          f"failed {len(result.failed)}   "
          f"project.h {'yes' if result.header_written else 'no'}   "
          f"refined {len(result.refined)}   "
          f"named {len(result.named)}")
    print(f"output -> {result.workspace}")
    if result.failed:
        # Nothing was written for these, so a re-run retries them.
        print(f"failed (not written, will retry on re-run): "
              f"{', '.join(result.failed)}")

    if args.json_path:
        payload = {
            "binary": args.binary,
            "provider": args.provider,
            "model": model,
            "workspace": str(result.workspace),
            "improved": result.improved,
            "skipped": result.skipped,
            "failed": result.failed,
            "refined": result.refined,
            "named": result.named,
            "header_written": result.header_written,
        }
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"summary -> {args.json_path}")

    # Non-zero when nothing succeeded but something was attempted, so a failed
    # run is visible to a script or CI step.
    return 1 if result.failed and not result.improved else 0


if __name__ == "__main__":
    raise SystemExit(main())
