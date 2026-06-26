"""
C++ toolchain wrappers. Compilation runs through WSL g++ (Debian) because that
is what is available on this machine; binaries are ELF, which Ghidra decompiles
fine. All paths are translated to /mnt/c form before being handed to wsl.exe.
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import to_wsl_path

# Default build flags. -g0 (no debug info) + strip keeps the binary close to a
# "released" artifact so the decompilation is realistically gnarly.
DEFAULT_STD = "c++17"
DEFAULT_OPT = "-O2"


@dataclass
class CompileResult:
    ok: bool
    stderr: str
    binary: Path | None


def _run_wsl(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["wsl", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def compile_cpp(src: Path, out_bin: Path, std: str = DEFAULT_STD,
                opt: str = DEFAULT_OPT, strip: bool = True) -> CompileResult:
    """Compile `src` to `out_bin` with WSL g++, optionally stripping symbols."""
    src_w = to_wsl_path(src)
    out_w = to_wsl_path(out_bin)
    out_bin.parent.mkdir(parents=True, exist_ok=True)

    proc = _run_wsl(["g++", f"-std={std}", opt, "-g0", "-o", out_w, src_w])
    if proc.returncode != 0:
        return CompileResult(False, proc.stderr.strip(), None)

    if strip:
        strip_proc = _run_wsl(["strip", out_w])
        if strip_proc.returncode != 0:
            return CompileResult(False, strip_proc.stderr.strip(), None)

    return CompileResult(True, "", out_bin)


def syntax_check(code: str, tmp_dir: Path, std: str = DEFAULT_STD) -> CompileResult:
    """
    Best-effort `g++ -fsyntax-only` on a code string. Decompiled fragments rarely
    compile standalone, so this is recorded as a diagnostic signal, not a hard
    gate (the hard gate is scanner.is_valid_cpp). Returns ok + compiler stderr.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / "_syntax_check.cpp"
    tmp.write_text(code, encoding="utf-8")
    proc = _run_wsl(["g++", f"-std={std}", "-fsyntax-only", to_wsl_path(tmp)])
    return CompileResult(proc.returncode == 0, proc.stderr.strip(), None)


def wsl_available() -> bool:
    try:
        return _run_wsl(["g++", "--version"], timeout=30).returncode == 0
    except Exception:
        return False
