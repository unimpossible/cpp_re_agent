#!/usr/bin/env python3
"""Evaluate decompiled/improved output against known original sources.

Corpus layout:    corpus/<case>.cpp            (the original source)
Candidate layout: <dir>/<case>.cpp | .cc | .c  (single-file output), or
                  <dir>/<case>/**/*.{c,cpp,cc} (per-function files, e.g. the
                                                app's workspace/<bin>/improved
                                                directory; concatenated)

Usage:
    python run_eval.py --candidates examples/improved
    python run_eval.py --candidates examples/improved --compare examples/raw
    python run_eval.py --candidates workspace_out --json results.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from similarity import SimilarityReport, compare

SOURCE_SUFFIXES = (".cpp", ".cc", ".c")
DEFAULT_CORPUS = Path(__file__).resolve().parent / "corpus"


def find_cases(corpus_dir: Path) -> dict[str, str]:
    """Map case name -> original source text."""
    cases = {}
    for path in sorted(corpus_dir.iterdir()):
        if path.suffix in SOURCE_SUFFIXES:
            cases[path.stem] = path.read_text(encoding="utf-8")
    return cases


def load_candidate(candidates_dir: Path, case: str) -> Optional[str]:
    """Load a candidate as one blob: single file, or concatenated directory."""
    for suffix in SOURCE_SUFFIXES:
        path = candidates_dir / f"{case}{suffix}"
        if path.is_file():
            return path.read_text(encoding="utf-8")

    case_dir = candidates_dir / case
    if case_dir.is_dir():
        parts = [p.read_text(encoding="utf-8")
                 for suffix in SOURCE_SUFFIXES
                 for p in sorted(case_dir.rglob(f"*{suffix}"))]
        if parts:
            return "\n\n".join(parts)
    return None


def evaluate(corpus_dir: Path,
             candidates_dir: Path) -> dict[str, Optional[SimilarityReport]]:
    """Score every corpus case; None where the candidate is missing."""
    results: dict[str, Optional[SimilarityReport]] = {}
    for case, original in find_cases(corpus_dir).items():
        candidate = load_candidate(candidates_dir, case)
        results[case] = compare(original, candidate) if candidate else None
    return results


def _fmt(value: Optional[float]) -> str:
    return f"{value:.3f}" if value is not None else "  n/a"


def format_table(results: dict[str, Optional[SimilarityReport]],
                 baseline: Optional[dict[str, Optional[SimilarityReport]]] = None,
                 ) -> str:
    header = f"{'case':<16} {'struct':>7} {'ast':>7} {'ident':>7} {'literal':>7} {'overall':>8}"
    if baseline:
        header += f" {'baseline':>9} {'delta':>7}"
    lines = [header, "-" * len(header)]

    for case, report in results.items():
        if report is None:
            lines.append(f"{case:<16} (no candidate found)")
            continue
        row = (f"{case:<16} {_fmt(report.structure):>7} {_fmt(report.ast_shape):>7}"
               f" {_fmt(report.identifier_recovery):>7}"
               f" {_fmt(report.literal_recovery):>7} {_fmt(report.overall):>8}")
        if baseline:
            base = baseline.get(case)
            if base is not None:
                delta = report.overall - base.overall
                row += f" {_fmt(base.overall):>9} {delta:>+7.3f}"
            else:
                row += f" {'n/a':>9} {'n/a':>7}"
        lines.append(row)

    scored = [r.overall for r in results.values() if r is not None]
    if scored:
        lines.append("-" * len(header))
        mean = sum(scored) / len(scored)
        summary = f"{'MEAN':<16} {'':>7} {'':>7} {'':>7} {'':>7} {mean:>8.3f}"
        if baseline:
            base_scored = [r.overall for r in baseline.values() if r is not None]
            if base_scored:
                base_mean = sum(base_scored) / len(base_scored)
                summary += f" {base_mean:>9.3f} {mean - base_mean:>+7.3f}"
        lines.append(summary)
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS,
                        help="Directory of original sources (default: ./corpus)")
    parser.add_argument("--candidates", type=Path, required=True,
                        help="Directory of decompiled/improved output to score")
    parser.add_argument("--compare", type=Path, default=None,
                        help="Baseline candidate directory to diff against "
                             "(e.g. raw decompilation vs. AI-improved)")
    parser.add_argument("--json", type=Path, default=None,
                        help="Also write per-case metrics to this JSON file")
    args = parser.parse_args(argv)

    if not args.corpus.is_dir():
        parser.error(f"corpus directory not found: {args.corpus}")
    if not args.candidates.is_dir():
        parser.error(f"candidates directory not found: {args.candidates}")

    results = evaluate(args.corpus, args.candidates)
    baseline = evaluate(args.corpus, args.compare) if args.compare else None

    print(format_table(results, baseline))

    if args.json:
        payload = {case: report.as_dict() if report else None
                   for case, report in results.items()}
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
