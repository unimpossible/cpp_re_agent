import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

class DecompilationError(Exception):
    """
    Decompilation could not be completed.

    Raised rather than returning quietly: a silent return is indistinguishable
    from "the binary had no functions", so callers reported success and the
    pipeline went on to improve an empty function set.
    """


def _tail(stream, limit: int = 2000) -> str:
    """Decode a captured stdout/stderr stream and keep the last `limit` chars."""
    if not stream:
        return ""
    if isinstance(stream, bytes):
        stream = stream.decode("utf-8", errors="replace")
    stream = stream.strip()
    return stream if len(stream) <= limit else "..." + stream[-limit:]


def _ghidrecomp_command(output_dir: str, binary_path: str) -> list:
    """
    How to invoke ghidrecomp.

    Prefers `python -m ghidrecomp` over the bare `ghidrecomp` command: it runs
    the interpreter we are already in (so it always finds the venv's install),
    needs nothing on PATH, and sidesteps the console-script `.exe` shim, which
    Windows Application Control policies block on some machines.
    """
    if importlib.util.find_spec("ghidrecomp") is not None:
        return [sys.executable, "-m", "ghidrecomp", "-o", output_dir, binary_path]
    return ["ghidrecomp", "-o", output_dir, binary_path]


def get_binary_md5(filepath: str) -> str:
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def decompile_binary(binary_path: str, output_dir: str):
    """
    Runs ghidrecomp on the binary.

    Raises `DecompilationError` on any failure, with ghidrecomp's captured
    stderr attached — callers must surface it rather than assume success.
    """
    if not binary_path or not os.path.exists(binary_path):
        raise DecompilationError(f"Binary not found: {binary_path!r}")

    os.makedirs(output_dir, exist_ok=True)

    # Check if we already have the output for this specific binary hash.
    # Ghidrecomp structure: output_dir/bins/<name>-<md5>
    try:
        binary_name = Path(binary_path).name
        binary_hash = get_binary_md5(binary_path)
    except OSError as e:
        raise DecompilationError(f"Could not read binary {binary_path!r}: {e}") from e

    for cached in (Path(output_dir) / "bins" / f"{binary_name}-{binary_hash}",
                   Path(output_dir) / "results" / "bins" / f"{binary_name}-{binary_hash[:6]}"):
        if cached.exists():
            print(f"Skipping decompilation: Output already exists at {cached}")
            return

    cmd = _ghidrecomp_command(output_dir, binary_path)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as e:
        raise DecompilationError(
            "ghidrecomp not found — install it with `pip install ghidrecomp` "
            "and make sure GHIDRA_INSTALL_DIR is set."
        ) from e
    except subprocess.CalledProcessError as e:
        # `str(CalledProcessError)` reports only the exit status, so the actual
        # Ghidra error is lost unless we pull it out of the captured streams.
        detail = _tail(e.stderr) or _tail(e.stdout) or "(no output captured)"
        raise DecompilationError(
            f"ghidrecomp failed for {binary_path!r} (exit {e.returncode}):\n{detail}"
        ) from e
    except OSError as e:
        raise DecompilationError(f"Could not run ghidrecomp: {e}") from e

def get_functions(output_dir: str):
    """
    Lists functions available in the output directory.
    Returns a dict of {func_name: code_content}.

    Raises `DecompilationError` if the directory does not exist: `rglob` on a
    missing path yields nothing, so this would otherwise be indistinguishable
    from a binary that decompiled to zero functions.
    """
    root = Path(output_dir)
    if not root.is_dir():
        raise DecompilationError(
            f"Decompilation output directory does not exist: {output_dir!r}"
        )

    functions = {}
    for file_path in root.rglob("*.c"):
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            functions[file_path.stem] = f.read()

    _warn_about_lost_functions(root, len(functions))
    return functions


# On Windows, a decompiled function whose demangled name contains a colon --
# every `std::string` returner, via the `[abi:cxx11]` tag -- makes ghidrecomp
# write to `name[abi:cxx11]-<addr>.c`, which NTFS interprets as an alternate
# data stream. The result is a zero-byte `name[abi` file with the real content
# hidden in a stream that `rglob("*.c")` cannot see.
_ADS_TRUNCATION_MARKER = "[abi"


def _warn_about_lost_functions(root: Path, found: int) -> None:
    """Report decompiled output that exists on disk but could not be read."""
    def is_lost(p: Path) -> bool:
        if not p.is_file() or p.suffix == ".c":
            return False
        # Ghidra's own project database lives alongside the decompilation and
        # is full of extension-less and empty files; only the decomps output
        # can hold lost functions.
        if "ghidra_projects" in p.parts or p.parent.name != "decomps":
            return False
        return _ADS_TRUNCATION_MARKER in p.name or p.stat().st_size == 0

    suspicious = [p for p in root.rglob("*") if is_lost(p)]
    if not suspicious:
        return

    names = ", ".join(sorted(p.name for p in suspicious)[:5])
    print(
        f"WARNING: {len(suspicious)} decompiled file(s) in {root} are unreadable "
        f"and were skipped ({names}). On Windows this is ghidrecomp writing "
        f"names containing ':' (e.g. `describe[abi:cxx11]-1234.c`), which NTFS "
        f"turns into an alternate data stream -- every std::string-returning "
        f"function is affected. {found} function(s) were loaded. Decompile "
        f"under WSL/Linux to recover them."
    )
