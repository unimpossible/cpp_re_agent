"""
Score function *selection* on a stripped binary against ground truth.

A shipped binary has no symbol table, so Ghidra names everything `FUN_<addr>`
and the name-based library filter goes blind — on a corpus binary it went from
keeping 67 functions to keeping 518, i.e. most of the LLM budget spent
improving the libstdc++ that was linked in. The pipeline falls back to call
depth from the entry point; this measures whether that actually works.

Ground truth comes from the fact that **stripping does not move code**. The
same program built twice, with and without symbols, has its functions at
identical addresses, so the unstripped decompilation says what each stripped
`FUN_<addr>` really is. A function counts as the program's own if its
unstripped name matches a function defined in the corpus source.

    python -m eval.stripped --case tier4_taskflow
    python -m eval.stripped --case tier4_taskflow --depths 1,2,3,4
    python -m eval.stripped --plain <dir> --stripped <dir> --case tier4_taskflow

Decompilations are cached under `eval/experiments/stripped/`, since Ghidra
takes minutes per binary.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from cpp_re_agent import decompiler, pipeline, scanner

from . import roundtrip
from .corpus import load_corpus
from .paths import EXPERIMENTS_DIR, REPO_ROOT, ensure_dir

# ghidrecomp names every file `<symbol>-<address>.c`; the address is what pairs
# the two builds, since it is the one thing stripping cannot change.
_ADDRESS_RE = re.compile(r"-([0-9a-fA-F]{4,})$")

BIN_DIR = REPO_ROOT / "eval" / "bin"
CACHE = EXPERIMENTS_DIR / "stripped"


def address_of(name: str) -> Optional[int]:
    m = _ADDRESS_RE.search(name)
    return int(m.group(1), 16) if m else None


def by_address(raw_dir: Path) -> Dict[int, Tuple[str, str]]:
    """{address: (decompiled name, body)} for one decompilation."""
    out: Dict[int, Tuple[str, str]] = {}
    for name, code in decompiler.get_functions(str(raw_dir)).items():
        addr = address_of(name)
        if addr is not None:
            out[addr] = (name, code)
    return out


def program_function_names(source: str) -> Set[str]:
    """Bare names of every function defined in the original C++ source."""
    return {scanner.normalize_name(n)
            for n in roundtrip.original_functions(source)}


@dataclass
class Selection:
    """How well one setting picked the program's own code out of a binary."""
    depth: Optional[int]
    selected: int
    true_positives: int
    false_positives: int
    total_user: int

    @property
    def precision(self) -> float:
        return self.true_positives / self.selected if self.selected else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.total_user if self.total_user else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def as_dict(self) -> dict:
        return {"depth": self.depth, "selected": self.selected,
                "true_positives": self.true_positives,
                "false_positives": self.false_positives,
                "total_user": self.total_user,
                "precision": round(self.precision, 4),
                "recall": round(self.recall, 4),
                "f1": round(self.f1, 4)}


def ground_truth(plain: Dict[int, Tuple[str, str]],
                 program_names: Set[str]) -> Dict[int, bool]:
    """{address: is this the program's own code} from the unstripped build."""
    return {addr: scanner.normalize_name(name) in program_names
            for addr, (name, _code) in plain.items()}


def score_selection(stripped: Dict[int, Tuple[str, str]],
                    truth: Dict[int, bool],
                    depth: Optional[int]) -> Selection:
    """Run the pipeline's real selection at `depth` and score it."""
    functions = {name: code for name, code in stripped.values()}
    chosen = set(pipeline.selected_functions(functions, max_depth=depth))

    addr_of_name = {name: addr for addr, (name, _c) in stripped.items()}
    tp = fp = 0
    for name in chosen:
        addr = addr_of_name.get(name)
        if addr is None or addr not in truth:
            fp += 1              # not in the unstripped build: not the program
        elif truth[addr]:
            tp += 1
        else:
            fp += 1
    return Selection(depth=depth, selected=len(chosen), true_positives=tp,
                     false_positives=fp,
                     total_user=sum(1 for v in truth.values() if v))


def decompile_cached(binary: Path, label: str,
                     status=print) -> Path:
    """Decompile `binary` into a cache dir, reusing it when already present."""
    out = ensure_dir(CACHE / label)
    if any(out.rglob("*.c")):
        status(f"  reusing cached decompilation: {out}")
        return out
    status(f"  decompiling {binary.name} (minutes)...")
    decompiler.decompile_binary(str(binary), str(out))
    return out


def format_table(rows: List[Selection], header: str = "") -> str:
    lines = []
    if header:
        lines.append(header)
    lines.append(f"{'--max-depth':<14}{'selected':>10}{'precision':>11}"
                 f"{'recall':>9}{'f1':>8}")
    lines.append("-" * 52)
    for r in rows:
        label = "off" if r.depth in (None, 0) else str(r.depth)
        lines.append(f"{label:<14}{r.selected:>10}{r.precision:>10.1%}"
                     f"{r.recall:>9.1%}{r.f1:>8.1%}")
    if rows:
        lines.append(f"\n({rows[0].total_user} functions in the binary belong "
                     f"to the program)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m eval.stripped",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    p.add_argument("--case", required=True,
                   help="Corpus program id (e.g. tier4_taskflow).")
    p.add_argument("--opt", default="O0",
                   help="Optimization level of the binaries to use (default: O0).")
    p.add_argument("--depths", default="none,1,2,3,4",
                   help="Comma-separated --max-depth settings to compare; "
                        "'none' means no depth filter (default: none,1,2,3,4).")
    p.add_argument("--plain", type=Path, default=None,
                   help="Existing unstripped decompilation dir (skips Ghidra).")
    p.add_argument("--stripped", type=Path, default=None,
                   help="Existing stripped decompilation dir (skips Ghidra).")
    p.add_argument("--json", type=Path, default=None,
                   help="Write the per-setting metrics to this JSON file.")
    args = p.parse_args(argv)

    programs = {prog.id: prog for prog in load_corpus()}
    if args.case not in programs:
        p.error(f"unknown corpus case {args.case!r}; "
                f"known: {', '.join(sorted(programs))}")
    program = programs[args.case]

    if args.plain and args.stripped:
        plain_dir, strip_dir = args.plain, args.stripped
    else:
        plain_bin = BIN_DIR / f"{args.case}-{args.opt}"
        strip_bin = BIN_DIR / f"{args.case}-{args.opt}-stripped"
        for b in (plain_bin, strip_bin):
            if not b.exists():
                p.error(f"missing {b} — run eval/build_binaries.sh first")
        print(f"case {args.case} ({args.opt})")
        plain_dir = decompile_cached(plain_bin, f"{args.case}-{args.opt}")
        strip_dir = decompile_cached(strip_bin, f"{args.case}-{args.opt}-stripped")

    plain, stripped = by_address(plain_dir), by_address(strip_dir)
    shared = set(plain) & set(stripped)
    print(f"\nunstripped {len(plain)}  stripped {len(stripped)}  "
          f"paired by address {len(shared)} "
          f"({len(shared) / max(1, len(plain)):.0%} of unstripped)")

    functions = {name: code for name, code in stripped.values()}
    print(f"looks_stripped -> {pipeline.looks_stripped(functions)}")

    truth = ground_truth({a: plain[a] for a in shared},
                         program_function_names(program.source))

    rows = []
    for token in args.depths.split(","):
        token = token.strip()
        if not token:
            continue
        # 0 disables the filter; None would mean "auto", which on a
        # stripped binary is the default depth, not "off".
        depth = 0 if token.lower() in ("none", "off") else int(token)
        rows.append(score_selection(stripped, truth, depth))

    print()
    print(format_table(rows))

    if args.json:
        import json
        args.json.write_text(json.dumps([r.as_dict() for r in rows], indent=2),
                             encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
