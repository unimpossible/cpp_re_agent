"""Load the corpus manifest into Program records, with content hashing."""
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

from .paths import CORPUS_DIR, REPO_ROOT

MANIFEST = CORPUS_DIR / "manifest.yaml"


@dataclass
class Program:
    id: str
    tier: int
    path: Path
    description: str
    stdin_cases: List[str] = field(default_factory=list)
    # Every translation unit + header making up the program. For a single-file
    # program this is just [path]; for a multi-file one `path` is the directory.
    sources: List[Path] = field(default_factory=list)

    def __post_init__(self):
        if not self.sources:
            self.sources = [self.path]

    @property
    def is_multi_file(self) -> bool:
        return self.path.is_dir()

    @property
    def translation_units(self) -> List[Path]:
        """Just the .cpp files — what the compiler is handed."""
        return [p for p in self.sources if p.suffix in (".cpp", ".cc", ".cxx")]

    @property
    def source(self) -> str:
        """
        The program's full original text. For multi-file programs the sources
        are concatenated (headers first, so struct definitions precede the code
        that uses them) — every consumer of this scans it for functions and
        types, and both live across the file boundary.
        """
        if len(self.sources) == 1:
            return self.sources[0].read_text(encoding="utf-8")
        parts = []
        for p in self.sources:
            parts.append(f"// ===== {p.name} =====\n{p.read_text(encoding='utf-8')}")
        return "\n".join(parts)

    @property
    def source_hash(self) -> str:
        """Stable content hash — keys the round-trip's compile/decompile cache."""
        digest = hashlib.sha256()
        for p in self.sources:
            digest.update(p.name.encode("utf-8"))
            digest.update(p.read_bytes())
        return digest.hexdigest()[:16]


# Headers before implementation files, each group sorted, so `source` and
# `source_hash` are stable regardless of filesystem ordering.
_HEADER_SUFFIXES = (".h", ".hpp", ".hh")


def collect_sources(root: Path) -> List[Path]:
    """All C++ sources under a program directory, headers first."""
    headers = sorted(p for p in root.rglob("*") if p.suffix in _HEADER_SUFFIXES)
    units = sorted(p for p in root.rglob("*") if p.suffix in (".cpp", ".cc", ".cxx"))
    return headers + units


def load_corpus(tier: int | None = None, ids: List[str] | None = None) -> List[Program]:
    """
    Returns corpus programs, optionally filtered to a single `tier` or an
    explicit list of `ids`. Missing source files are skipped with a warning so a
    partial corpus still runs.
    """
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    programs: List[Program] = []
    for entry in data.get("programs", []):
        if tier is not None and entry["tier"] != tier:
            continue
        if ids is not None and entry["id"] not in ids:
            continue
        path = (REPO_ROOT / entry["path"]).resolve()
        if not path.exists():
            print(f"[corpus] WARNING: missing source for {entry['id']}: {path}")
            continue

        # `path` may name a single .cpp or a directory holding a multi-file
        # program; the rest of the harness only sees `sources`.
        sources = collect_sources(path) if path.is_dir() else [path]
        if not sources:
            print(f"[corpus] WARNING: no C++ sources for {entry['id']}: {path}")
            continue

        programs.append(Program(
            id=entry["id"],
            tier=entry["tier"],
            path=path,
            description=entry.get("description", ""),
            stdin_cases=entry.get("stdin_cases", []),
            sources=sources,
        ))
    return programs
