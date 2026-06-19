import json
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from . import scanner

"""
Persistent per-binary map of original function name -> improved signature.

This lets a caller be improved with the *real* improved signatures of its
callees injected as context, instead of re-reading the first N lines of a
file. It is the consistency backbone for whole-binary improvement: renames
and type choices made in a callee propagate up to everyone that calls it.
"""

SYMBOLS_FILE = "symbols.json"


def _map_path(workspace_dir) -> Path:
    return Path(workspace_dir) / SYMBOLS_FILE


def load_symbols(workspace_dir) -> Dict[str, dict]:
    path = _map_path(workspace_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Could not load symbol map: {e}")
    return {}


def save_symbols(workspace_dir, symbols: Dict[str, dict]) -> None:
    path = _map_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(symbols, indent=2), encoding="utf-8")


def extract_signature(code: str) -> Optional[Tuple[str, str]]:
    """
    Returns (new_name, signature) for the first function definition in `code`,
    where signature is everything up to the opening brace, whitespace-collapsed.
    """
    try:
        items = scanner.scan_code(code)
    except Exception:
        return None

    func = next((i for i in items if i.kind == "function"), None)
    if not func:
        return None

    brace = func.body.find("{")
    sig = func.body[:brace] if brace != -1 else func.body
    sig = " ".join(sig.split())
    return func.name, sig


def record_improvement(symbols: Dict[str, dict], original_name: str, improved_code: str) -> Dict[str, dict]:
    """Records the improved signature for `original_name` in-place."""
    result = extract_signature(improved_code)
    if result:
        new_name, sig = result
        symbols[original_name] = {"new_name": new_name, "signature": sig}
    return symbols


def format_callee_context(symbols: Dict[str, dict], callee_names: Iterable[str]) -> str:
    """
    Renders the improved signatures of the given callees as prompt context.
    Returns "" when none of the callees have a recorded signature.
    """
    lines = []
    for original in sorted(set(callee_names)):
        entry = symbols.get(original)
        if not entry:
            continue
        note = f"  // was {original}" if entry["new_name"] != original else ""
        lines.append(f"{entry['signature']};{note}")

    if not lines:
        return ""

    return "### Improved Callee Signatures (use these names/types)\n```cpp\n" + "\n".join(lines) + "\n```\n\n"
