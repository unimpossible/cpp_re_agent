#!/usr/bin/env python3
"""Evaluate decompiled/improved output against known original sources.

Corpus layout: corpus/<case>.cpp  (the original source)

`--candidates` accepts any of these, no copying or renaming required:

    <dir>/<case>.cpp                  single-file output per case
    <dir>/<case>/**/*.{c,cpp,cc}      per-function files, concatenated
    workspace/                        the app's workspace: every <binary>/
                                      whose name maps to a corpus case
    workspace/inventory-O2/           one binary's dir (uses improved/)
    workspace/inventory-O2/improved/  one binary's output dir directly

Binary directory names are mapped to corpus cases by stripping the usual
suffixes, so `inventory-O2`, `inventory-clang-O0` and `inventory.exe` all
score against `corpus/inventory.cpp`. Use --case to force the mapping.

This is the deterministic, offline half of `eval/`: no Ghidra, no LLM, no
judge — just four similarity metrics against known ground truth, which is what
makes it cheap enough to gate CI. The judged prompt-optimization experiments
live alongside it in `run_experiment.py`.

Usage:
    python -m eval.run_eval                                   # scores ./workspace
    python -m eval.run_eval --candidates workspace/inventory-O2/improved
    python -m eval.run_eval --candidates eval/examples/improved --compare eval/examples/raw
    python -m eval.run_eval --candidates workspace --json results.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

from .similarity import SimilarityReport, compare

SOURCE_SUFFIXES = (".cpp", ".cc", ".c")
HEADER_SUFFIXES = (".h", ".hpp", ".hh")
EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_CORPUS = EVAL_DIR / "corpus"
# The app's output root: <repo>/workspace/<binary>/{raw,improved}/
DEFAULT_CANDIDATES = EVAL_DIR.parent / "workspace"
DEFAULT_STAGE = "improved"

# Decorations the pipeline appends to a case name when naming a binary:
# optimization level, compiler, build flavor, project hash, file extension.
NAME_NOISE_RE = re.compile(
    r"(?:[-_.](?:O[0-3sz]|Ofast|g\+\+|gcc|clang\+\+|clang|msvc|debug|release"
    r"|stripped|static|[0-9a-f]{6,})|\.exe)+$",
    re.IGNORECASE,
)


def _read_program_dir(directory: Path) -> str:
    """Concatenate a multi-file corpus program, headers first."""
    headers = sorted(p for p in directory.rglob("*") if p.suffix in HEADER_SUFFIXES)
    units = sorted(p for p in directory.rglob("*") if p.suffix in SOURCE_SUFFIXES)
    return "\n\n".join(p.read_text(encoding="utf-8") for p in headers + units)


def find_cases(corpus_dir: Path) -> dict[str, str]:
    """
    Map case name -> original source text.

    Covers all three shapes the corpus uses: a single `<case>.cpp`, a
    `<case>/` directory of translation units (tier4_taskflow), and manifest
    entries whose source lives outside the corpus directory. The manifest is
    consulted best-effort so this stays runnable with nothing but tree-sitter
    installed — CI depends on that.
    """
    cases: dict[str, str] = {}
    if corpus_dir.is_dir():
        for path in sorted(corpus_dir.iterdir()):
            if path.suffix in SOURCE_SUFFIXES:
                cases[path.stem] = path.read_text(encoding="utf-8")
            elif path.is_dir() and not path.name.startswith((".", "_")):
                text = _read_program_dir(path)
                if text:
                    cases[path.name] = text

    try:
        from .corpus import load_corpus
        for program in load_corpus():
            cases.setdefault(program.id, program.source)
    except Exception:
        pass  # manifest/pyyaml unavailable: the directory scan above stands
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


def infer_case(name: str, cases: list[str]) -> Optional[str]:
    """Map a binary/directory name onto a corpus case name.

    'inventory-O2', 'inventory-clang-O0', 'inventory.exe' -> 'inventory'.
    """
    stem = NAME_NOISE_RE.sub("", name).lower()
    for case in sorted(cases, key=len, reverse=True):
        if stem == case.lower():
            return case
    return None


def read_provenance(directory: Path) -> Optional[str]:
    """Model recorded in the workspace's runs.json (written by the pipeline).

    Looked up on the candidate dir and its parent, so it is found whether the
    caller pointed at `workspace/<bin>` or `workspace/<bin>/improved`.
    """
    for owner in (directory, directory.parent):
        path = owner / "runs.json"
        if not path.is_file():
            continue
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        models = [r.get("model") for r in records if isinstance(r, dict) and r.get("model")]
        if models:
            unique = list(dict.fromkeys(models))
            return unique[0] if len(unique) == 1 else " + ".join(unique)
    return None


def read_sources(directory: Path) -> Optional[str]:
    """Concatenate every source file under a directory, or None if there are none."""
    parts = [p.read_text(encoding="utf-8")
             for suffix in SOURCE_SUFFIXES
             for p in sorted(directory.rglob(f"*{suffix}"))]
    return "\n\n".join(parts) if parts else None


def _stage_dir(directory: Path, stage: str) -> Path:
    """Prefer <dir>/<stage> (e.g. workspace/<bin>/improved) when it exists."""
    staged = directory / stage
    return staged if staged.is_dir() else directory


def collect_sources(candidates_dir: Path,
                    cases: list[str],
                    case: Optional[str] = None,
                    stage: str = DEFAULT_STAGE) -> dict[str, Path]:
    """Resolve a candidates path into {case: directory holding that case's output}.

    Handles the eval's own `<dir>/<case>` layout, a single binary's output
    directory, and the app's whole `workspace/` root — so output can be scored
    where the pipeline wrote it.
    """
    if case:
        return {case: _stage_dir(candidates_dir, stage)}

    # Classic eval layout: <dir>/<case>.cpp or <dir>/<case>/.
    found = {c: candidates_dir for c in cases
             if load_candidate(candidates_dir, c) is not None}
    if found:
        return found

    # The directory *is* one case's output (workspace/inventory-O2[/improved]).
    for owner in (candidates_dir, candidates_dir.parent):
        inferred = infer_case(owner.name, cases)
        if inferred:
            return {inferred: _stage_dir(candidates_dir, stage)}

    # The directory is a workspace root of per-binary output directories.
    for child in sorted(p for p in candidates_dir.iterdir() if p.is_dir()):
        inferred = infer_case(child.name, cases)
        if inferred and inferred not in found:
            found[inferred] = _stage_dir(child, stage)
    return found


def collect_candidates(candidates_dir: Path,
                       cases: list[str],
                       case: Optional[str] = None,
                       stage: str = DEFAULT_STAGE) -> dict[str, str]:
    """Resolve a candidates path into {case: source blob}."""
    found = {}
    for name, source in collect_sources(candidates_dir, cases, case, stage).items():
        # Classic layout resolves to the shared root (<root>/<case>.cpp);
        # workspace layouts resolve to a directory of per-function files.
        text = load_candidate(source, name) or read_sources(source)
        if text:
            found[name] = text
    return found


def collect_models(candidates_dir: Path,
                   cases: list[str],
                   case: Optional[str] = None,
                   stage: str = DEFAULT_STAGE) -> dict[str, Optional[str]]:
    """{case: model that produced it}, from each candidate's runs.json."""
    return {name: read_provenance(source)
            for name, source in collect_sources(candidates_dir, cases, case, stage).items()}


def evaluate(corpus_dir: Path,
             candidates_dir: Path,
             case: Optional[str] = None,
             stage: str = DEFAULT_STAGE) -> dict[str, Optional[SimilarityReport]]:
    """Score every corpus case; None where the candidate is missing."""
    originals = find_cases(corpus_dir)
    candidates = collect_candidates(candidates_dir, list(originals), case, stage)
    return {name: compare(original, candidates[name]) if name in candidates else None
            for name, original in originals.items()}


def _fmt(value: Optional[float]) -> str:
    return f"{value:.3f}" if value is not None else "  n/a"


def format_table(results: dict[str, Optional[SimilarityReport]],
                 baseline: Optional[dict[str, Optional[SimilarityReport]]] = None,
                 models: Optional[dict[str, Optional[str]]] = None,
                 ) -> str:
    models = {k: v for k, v in (models or {}).items() if v}
    header = f"{'case':<16} {'struct':>7} {'ast':>7} {'ident':>7} {'literal':>7} {'overall':>8}"
    if baseline:
        header += f" {'baseline':>9} {'delta':>7}"
    if models:
        header += f"  {'model':<24}"
    lines = [header, "-" * len(header)]

    # The corpus is much larger than any one run's candidates, so unscored
    # cases are summarized in one line instead of one row each.
    missing = [case for case, report in results.items() if report is None]

    for case, report in results.items():
        if report is None:
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
        if models:
            row += f"  {models.get(case) or '(unrecorded)':<24}"
        lines.append(row)

    scored = [r.overall for r in results.values() if r is not None]
    if not scored:
        lines.append("(no candidates resolved)")
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

    if missing:
        shown = ", ".join(missing[:6]) + (", ..." if len(missing) > 6 else "")
        lines.append(f"({len(missing)} corpus case(s) without candidates: {shown})")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS,
                        help=f"Directory of original sources (default: {DEFAULT_CORPUS})")
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES,
                        help="Output to score: a candidates root, a binary's "
                             "workspace directory, or the workspace root "
                             f"(default: {DEFAULT_CANDIDATES})")
    parser.add_argument("--compare", type=Path, default=None,
                        help="Baseline candidate directory to diff against "
                             "(e.g. raw decompilation vs. AI-improved)")
    parser.add_argument("--case", default=None,
                        help="Force the corpus case for --candidates instead of "
                             "inferring it from the directory name")
    parser.add_argument("--stage", default=DEFAULT_STAGE,
                        help="Subdirectory to score inside a binary's workspace "
                             f"directory (default: {DEFAULT_STAGE})")
    parser.add_argument("--json", type=Path, default=None,
                        help="Also write per-case metrics to this JSON file")
    args = parser.parse_args(argv)

    if not args.corpus.is_dir():
        parser.error(f"corpus directory not found: {args.corpus}")
    if not args.candidates.is_dir():
        parser.error(f"candidates directory not found: {args.candidates}")

    cases = list(find_cases(args.corpus))
    results = evaluate(args.corpus, args.candidates, args.case, args.stage)
    baseline = (evaluate(args.corpus, args.compare, args.case, args.stage)
                if args.compare else None)
    models = collect_models(args.candidates, cases, args.case, args.stage)

    print(format_table(results, baseline, models))

    if not any(results.values()):
        print(f"\nNo candidates resolved under {args.candidates}. Point "
              "--candidates at a binary's workspace directory, or name the "
              "corpus case explicitly with --case.", file=sys.stderr)
        return 1

    if args.json:
        payload = {case: {**report.as_dict(), "model": models.get(case)} if report else None
                   for case, report in results.items()}
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
