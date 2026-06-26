"""
Headless command-line entry point for the RE pipeline.

Runs both stages against a binary in one command — no Streamlit, no config file
(a handful of flags is enough). The Streamlit pages remain the interactive
front-end; both call the same `pipeline` orchestration.

    cpp-re path/to/binary
    cpp-re path/to/binary --provider local --model openai/gpt-oss-20b
    cpp-re path/to/binary --no-synthesis            # stage 1 only
    cpp-re path/to/binary --no-contextual --limit 5 # header synth, no refine

Also runnable as `python -m cpp_re_agent`.
"""
import argparse
import os
import sys

from . import pipeline


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="cpp-re",
        description="Decompile a binary and improve it back toward readable C++ "
                    "(stage 1: per-function improve; stage 2: header synthesis + "
                    "contextual refine).")
    p.add_argument("binary", help="Path to the target binary.")
    p.add_argument("--provider", default="local", help="LLM provider (default: local).")
    p.add_argument("--model", default=None,
                   help="Model id (default: per-provider, e.g. openai/gpt-oss-20b).")
    p.add_argument("--output-dir", default=None,
                   help="Workspace dir for outputs (default: ./workspace/<binary>).")
    p.add_argument("--no-synthesis", action="store_true",
                   help="Stop after stage 1 (skip header synthesis + contextual pass).")
    p.add_argument("--no-contextual", action="store_true",
                   help="Run header synthesis but skip the per-function contextual refine.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the number of functions actually improved in stage 1 "
                        "(library/stub functions are skipped before counting).")
    args = p.parse_args(argv)

    if not os.path.exists(args.binary):
        print(f"error: binary not found: {args.binary}", file=sys.stderr)
        return 2

    model = args.model or pipeline.default_model_for(args.provider)
    workspace = pipeline.workspace_for(args.binary, args.output_dir)

    print("=" * 70)
    print(f"binary={args.binary}")
    print(f"provider={args.provider}  model={model}")
    print(f"workspace={workspace}  limit={args.limit}")
    print(f"stages={'1 only' if args.no_synthesis else ('1+header' if args.no_contextual else 'all')}")
    if args.provider == "local":
        print(f"endpoint={os.getenv('LOCAL_LLM_URL', 'http://localhost:1234/v1')}")
    print("=" * 70)

    result = pipeline.run_all(
        args.binary, provider=args.provider, model_name=model,
        output_dir=args.output_dir, limit=args.limit,
        do_synthesis=not args.no_synthesis,
        do_contextual=not args.no_contextual,
    )

    print("-" * 70)
    print(f"improved: {len(result.improved)}   skipped: {len(result.skipped)}   "
          f"project.h: {'yes' if result.header_written else 'no'}   "
          f"refined: {len(result.refined)}")
    print(f"output -> {result.workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
