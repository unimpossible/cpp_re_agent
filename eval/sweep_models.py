#!/usr/bin/env python3
"""Run the pipeline over one binary with several models and score each result.

Answers "which model improves decompiled code best?" with a number: each model
gets its own workspace under `--out`, and every workspace is scored against the
matching `corpus/<case>.cpp` by the same metrics `run_eval.py` uses.

Ghidra decompilation is model-independent, so the first model's `raw/` is
reused for the rest (or seeded from `--reuse-raw`) instead of re-decompiling.

    python -m eval.sweep_models --binary eval/bin/inventory-O2
    python -m eval.sweep_models --binary eval/bin/inventory-O2 --models qwen/qwen3-8b openai/gpt-oss-20b
    python -m eval.sweep_models --binary eval/bin/inventory-O2 --reuse-raw workspace/inventory-O2/raw

With no --models, the local endpoint (LOCAL_LLM_URL) is asked what it serves.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .run_eval import DEFAULT_CORPUS, evaluate, find_cases, format_table, infer_case

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent

DEFAULT_OUT = EVAL_DIR / "runs"


def discover_models(timeout: float = 20.0) -> list[str]:
    """Model ids served by the configured local/OpenAI-compatible endpoint."""
    base = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1").rstrip("/")
    request = urllib.request.Request(
        f"{base}/models",
        # Some deployments sit behind a WAF that rejects the default UA.
        headers={"User-Agent": "Mozilla/5.0",
                 "Authorization": f"Bearer {os.getenv('LOCAL_LLM_KEY', 'lm-studio')}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return [m["id"] for m in payload.get("data", []) if m.get("id")]


def workspace_name(model: str) -> str:
    """Filesystem-safe directory name for a model id."""
    return model.replace("/", "__").replace(":", "_")


def seed_raw(workspace: Path, reuse_raw: Optional[Path]) -> bool:
    """Copy an existing decompilation into the workspace; True if seeded."""
    target = workspace / "raw"
    if target.exists():
        return True
    if not reuse_raw or not Path(reuse_raw).is_dir():
        return False
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(reuse_raw, target)
    return True


def run_model(binary: Path, model: str, out_dir: Path, provider: str,
              reuse_raw: Optional[Path], limit: Optional[int],
              contextual: bool) -> tuple[Path, Optional[str]]:
    """Run the full pipeline for one model. Returns (workspace, error)."""
    from cpp_re_agent import pipeline

    workspace = out_dir / workspace_name(model) / binary.name
    seeded = seed_raw(workspace, reuse_raw)
    print(f"--- {model} -> {workspace} {'(raw reused)' if seeded else ''}", flush=True)

    try:
        pipeline.run_all(str(binary), provider=provider, model_name=model,
                         output_dir=str(workspace), limit=limit,
                         do_synthesis=True, do_contextual=contextual)
        return workspace, None
    except Exception as exc:  # one bad model shouldn't end the sweep
        print(f"    ERROR {type(exc).__name__}: {exc}", flush=True)
        return workspace, f"{type(exc).__name__}: {exc}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=EVAL_DIR / "bin" / "inventory-O2",
                        help="Corpus binary to run through the pipeline")
    parser.add_argument("--models", nargs="*", default=None,
                        help="Model ids (default: everything the endpoint serves)")
    parser.add_argument("--provider", default="local", help="LLM provider (default: local)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"Root for per-model workspaces (default: {DEFAULT_OUT})")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--reuse-raw", type=Path, default=None,
                        help="Existing raw/ decompilation to seed each workspace with, "
                             "skipping Ghidra (default: reuse the first model's)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap functions improved per run")
    parser.add_argument("--no-contextual", action="store_true",
                        help="Skip the stage-2 contextual refine pass")
    parser.add_argument("--json", type=Path, default=None,
                        help="Write the leaderboard to this JSON file")
    args = parser.parse_args(argv)

    if not args.binary.is_file():
        parser.error(f"binary not found: {args.binary}")

    # Langfuse's exporter can stall a long batch against an unreachable server.
    os.environ.setdefault("DISABLE_LANGFUSE", "1")

    try:  # pick up LOCAL_LLM_URL/KEY before the endpoint is queried
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    models = args.models
    if not models:
        try:
            models = discover_models()
        except Exception as exc:
            parser.error(f"could not list models from LOCAL_LLM_URL: {exc}")
    if not models:
        parser.error("no models to run")

    case = infer_case(args.binary.name, list(find_cases(args.corpus)))
    print(f"binary={args.binary}  case={case or '(unmatched)'}  models={len(models)}")

    reuse_raw = args.reuse_raw
    rows: list[dict] = []
    for model in models:
        started = time.time()
        workspace, error = run_model(args.binary, model, args.out, args.provider,
                                     reuse_raw, args.limit, not args.no_contextual)
        elapsed = time.time() - started
        if reuse_raw is None and (workspace / "raw").is_dir():
            reuse_raw = workspace / "raw"  # decompile once, reuse for the rest

        results = evaluate(args.corpus, workspace, case=case)
        report = results.get(case) if case else None
        rows.append({"model": model, "workspace": str(workspace), "seconds": round(elapsed, 1),
                     "error": error,
                     **(report.as_dict() if report else {"overall": None})})
        print(format_table(results, models={case: model}), flush=True)

    rows.sort(key=lambda r: (r.get("overall") is None, -(r.get("overall") or 0)))
    print("\n" + "=" * 78)
    print(f"{'model':<28} {'struct':>7} {'ast':>7} {'ident':>7} {'literal':>7} {'overall':>8} {'sec':>6}")
    print("-" * 78)
    for row in rows:
        if row.get("overall") is None:
            print(f"{row['model']:<28} {'failed: ' + (row['error'] or 'no output'):>50}")
            continue
        print(f"{row['model']:<28} {row['structure']:>7.3f} {row['ast_shape']:>7.3f} "
              f"{row['identifier_recovery']:>7.3f} {row['literal_recovery']:>7.3f} "
              f"{row['overall']:>8.3f} {row['seconds']:>6.0f}")

    if args.json:
        args.json.write_text(json.dumps({"binary": str(args.binary), "case": case,
                                         "results": rows}, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
