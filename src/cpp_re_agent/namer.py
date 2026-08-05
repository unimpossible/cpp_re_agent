"""
Final pass: give a name to every function that still has none.

On a stripped binary Ghidra invents `FUN_00401820`, and the per-function
improve pass usually declines to rename it — with only one function body in
view there is often nothing to name it *after*. Measured on the tier4 corpus
binary, 55 of 72 improved functions still carried a synthetic name at the end
of stage 2, and 414 call sites still read `FUN_...`.

By the end of the run there is much more to work with: a synthesized
`project.h`, every function's improved body, and a call graph whose edges now
connect recovered code. That is when naming is most tractable, so it happens
last rather than first.

A name is only useful if it is applied everywhere, so a rename rewrites the
definition *and* every call site across every file.
"""
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from .llm_factory import ImprovementError, require_llm, stream_text

# Ghidra's placeholders: FUN_/LAB_/SUB_ followed by an address.
_SYNTHETIC_RE = re.compile(r"^(?:FUN|LAB|SUB|UNK)_[0-9a-fA-F]+$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Names a rename must never produce.
_RESERVED = {
    "alignas", "alignof", "and", "asm", "auto", "bool", "break", "case",
    "catch", "char", "class", "const", "constexpr", "continue", "decltype",
    "default", "delete", "do", "double", "else", "enum", "explicit", "export",
    "extern", "false", "float", "for", "friend", "goto", "if", "inline", "int",
    "long", "main", "mutable", "namespace", "new", "noexcept", "not",
    "nullptr", "operator", "or", "private", "protected", "public", "register",
    "return", "short", "signed", "sizeof", "static", "struct", "switch",
    "template", "this", "throw", "true", "try", "typedef", "typename", "union",
    "unsigned", "using", "virtual", "void", "volatile", "while", "xor",
}

NAMES_FILE = "names.json"

NAMING_PROMPT = (
    "You are naming a function recovered from a stripped binary.\n"
    "Infer what it does from its body, the project's types, and the functions "
    "it calls and is called by.\n"
    "Reply with ONE identifier and nothing else — no explanation, no "
    "punctuation, no type, no parentheses.\n"
    "Use lower_snake_case. Be specific and behavioural (`find_task_by_id`, "
    "`reset_metrics`), never generic (`process`, `handler`, `func1`, `helper`) "
    "and never a restatement of the address.\n"
    "If the body genuinely gives no clue what it does, reply exactly: UNKNOWN"
)


def is_synthetic(name: str) -> bool:
    """True for a decompiler placeholder like `FUN_00401820`."""
    return bool(_SYNTHETIC_RE.match(strip_address(name)))


def strip_address(name: str) -> str:
    """`FUN_00401820-00401820` -> `FUN_00401820` (ghidrecomp's file stem)."""
    return re.sub(r"-[0-9a-fA-F]{4,}$", "", name)


def valid_identifier(name: str, taken: Set[str]) -> bool:
    return (bool(_IDENTIFIER_RE.match(name))
            and name not in _RESERVED
            and not is_synthetic(name)
            and name not in taken
            and len(name) <= 60)


def unique_name(proposal: str, taken: Set[str]) -> Optional[str]:
    """`proposal`, or a numbered variant when it is already in use."""
    if not _IDENTIFIER_RE.match(proposal) or proposal in _RESERVED:
        return None
    if is_synthetic(proposal):
        return None
    if proposal not in taken:
        return proposal
    for n in range(2, 100):
        candidate = f"{proposal}_{n}"
        if candidate not in taken:
            return candidate
    return None


def parse_proposal(text: str) -> Optional[str]:
    """Pull a bare identifier out of the model's reply."""
    if not text:
        return None
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    # Tolerate `void foo(int)` / `foo()` / backticks even though we asked for
    # a bare identifier.
    line = line.strip("`*_ \t").replace("()", "")
    if "(" in line:
        line = line.split("(", 1)[0]
    token = line.split()[-1] if line.split() else ""
    token = token.strip("*&;:")
    if not token or token.upper() == "UNKNOWN":
        return None
    return token if _IDENTIFIER_RE.match(token) else None


def build_prompt(code: str, header: str, callees: Iterable[str],
                 callers: Iterable[str], max_header: int = 4000) -> str:
    """Assemble the naming prompt from whole-program context."""
    parts = [NAMING_PROMPT, ""]
    if header:
        clipped = header[:max_header]
        parts += ["### Project types (project.h)", "```cpp", clipped, "```", ""]
    callee_list = [c for c in callees if not is_synthetic(c)]
    caller_list = [c for c in callers if not is_synthetic(c)]
    if callee_list:
        parts += [f"### It calls: {', '.join(sorted(set(callee_list))[:20])}", ""]
    if caller_list:
        parts += [f"### It is called by: {', '.join(sorted(set(caller_list))[:20])}", ""]
    parts += ["### Function to name", "```cpp", code, "```", "",
              "Identifier:"]
    return "\n".join(parts)


def propose_name(code: str, header: str = "", callees: Iterable[str] = (),
                 callers: Iterable[str] = (), provider: str = "local",
                 model_name: str = "openai/gpt-oss-20b") -> Optional[str]:
    """Ask the model for one identifier. None when it declines or misbehaves."""
    llm = require_llm(provider, model_name)
    prompt = build_prompt(code, header, callees, callers)
    try:
        reply = stream_text(llm, prompt)
    except Exception as e:
        raise ImprovementError(f"Naming failed: {e}") from e
    return parse_proposal(reply)


def _word_re(name: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(name)}\b")


def apply_renames(improved_dir: Path, renames: Dict[str, str]) -> int:
    """
    Rewrite `renames` (old identifier -> new) across every improved file.

    Whole-word only, and every file is rewritten, not just the one that defines
    the function: a name that is not applied at the call sites leaves the
    output reading `FUN_0010a4b2(container)` inside an otherwise recovered
    function. Returns the number of files changed.
    """
    if not renames:
        return 0
    patterns = [(_word_re(old), new) for old, new in renames.items() if old != new]
    changed = 0
    for path in sorted(Path(improved_dir).glob("*.cpp")):
        text = original = path.read_text(encoding="utf-8")
        for pattern, new in patterns:
            text = pattern.sub(new, text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed += 1
    return changed


def _read_names(workspace: Path) -> Dict[str, dict]:
    path = Path(workspace) / NAMES_FILE
    if not path.exists():
        return {"functions": {}, "files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"functions": {}, "files": {}}
    if not isinstance(data, dict):
        return {"functions": {}, "files": {}}
    # Tolerate the earlier flat {old: new} shape.
    if "functions" not in data and "files" not in data:
        return {"functions": data, "files": {}}
    return {"functions": data.get("functions", {}) or {},
            "files": data.get("files", {}) or {}}


def load_names(workspace: Path) -> Dict[str, str]:
    """{old function name: recovered name} from a previous naming pass."""
    return _read_names(workspace)["functions"]


def load_file_map(workspace: Path) -> Dict[str, str]:
    """
    {original file stem: current file stem} for files this pass renamed.

    Stage 1 resumes by checking whether `improved/<raw name>.cpp` exists, and
    renaming that file to `search_container.cpp` would hide it — the function
    would be improved again and written back under its old name, leaving two
    copies. This map is how the resume check still finds it.
    """
    return _read_names(workspace)["files"]


def save_names(workspace: Path, renames: Dict[str, str],
               files: Optional[Dict[str, str]] = None) -> None:
    """Record old -> new so a rename can be traced back to its address."""
    current = _read_names(workspace)
    current["functions"].update(renames)
    current["files"].update(files or {})
    (Path(workspace) / NAMES_FILE).write_text(
        json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")


def rename_files(improved_dir: Path, file_renames: Dict[str, str]) -> int:
    """
    Rename `improved/<stem>.cpp` to `improved/<new stem>.cpp`.

    A workspace full of `FUN_0010991a-0010991a.cpp` is unreadable even when the
    code inside has been recovered. Skips a rename whose target already exists
    rather than overwriting someone else's output. Returns the count renamed.
    """
    improved_dir = Path(improved_dir)
    renamed = 0
    for old_stem, new_stem in sorted(file_renames.items()):
        if old_stem == new_stem:
            continue
        src = improved_dir / f"{old_stem}.cpp"
        dst = improved_dir / f"{new_stem}.cpp"
        if not src.exists() or dst.exists():
            continue
        src.rename(dst)
        renamed += 1
    return renamed


def existing_names(symbols: Dict[str, dict], extra: Iterable[str] = ()) -> Set[str]:
    """Every identifier already in use, so a proposal cannot collide."""
    taken: Set[str] = set(_RESERVED)
    for entry in symbols.values():
        new_name = entry.get("new_name")
        if new_name and not is_synthetic(new_name):
            taken.add(new_name)
    taken.update(n for n in extra if n)
    return taken


def unnamed_functions(symbols: Dict[str, dict]) -> List[str]:
    """Keys whose recovered name is still a decompiler placeholder."""
    return sorted(k for k, v in symbols.items()
                  if is_synthetic(v.get("new_name", "")) or is_synthetic(k)
                  and not v.get("new_name"))
