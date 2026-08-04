"""Path helpers, including Windows <-> WSL translation for the g++ toolchain."""
import os
from pathlib import Path

# Repo root = parent of the eval/ package directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
EXPERIMENTS_DIR = REPO_ROOT / "experiments"


def to_wsl_path(win_path) -> str:
    """
    Convert a Windows path (e.g. C:\\Users\\x\\f.cpp) to its WSL mount form
    (/mnt/c/Users/x/f.cpp). Accepts str or Path; returns a POSIX string usable
    as an argument to `wsl <cmd>`.
    """
    p = Path(win_path).resolve()
    drive = p.drive.rstrip(":").lower()  # "C:" -> "c"
    rest = p.as_posix()[len(p.drive):].lstrip("/")
    return f"/mnt/{drive}/{rest}"


def ensure_dir(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
