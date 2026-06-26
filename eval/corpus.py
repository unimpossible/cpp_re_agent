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

    @property
    def source(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def source_hash(self) -> str:
        """Stable content hash — keys the round-trip's compile/decompile cache."""
        return hashlib.sha256(self.path.read_bytes()).hexdigest()[:16]


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
        programs.append(Program(
            id=entry["id"],
            tier=entry["tier"],
            path=path,
            description=entry.get("description", ""),
            stdin_cases=entry.get("stdin_cases", []),
        ))
    return programs
