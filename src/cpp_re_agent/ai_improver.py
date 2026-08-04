import os
import re
from pathlib import Path

from . import scanner
from . import symbol_map
from .llm_factory import ImprovementError, get_llm

_CODE_BLOCK_RE = re.compile(r"```(?:cpp|c\+\+|c)?\s*\n(.*?)```", re.DOTALL)

# The base improvement instruction. Exposed as a module constant so the
# experiment framework can use it as the control/seed prompt and inject evolved
# variants via improve_function(..., base_prompt=...). The conditional
# "### CONTEXT PROVIDED" appendix is added separately at call time.
DEFAULT_SYSTEM_PROMPT = (
    "You are an expert C++ Reverse Engineer. "
    "Your task is to improve the readability of the following decompiled C++ code. "
    "1. Rename variables to be meaningful (e.g., iVar1 -> distinct_count). "
    "2. Fix data types where obvious. "
    "3. Add comments explaining the logic. "
    "4. Do NOT change the functional behavior. "
    "5. Create structs or classes where appropriate. "
    "6. If you aren't sure what something does, do not rename it. "
    "7. Return ONLY the C++ code, no markdown fencing."
)

# Ghidra decompiler artifacts. Their density is the strongest signal of how
# much readability an LLM pass can recover (generic names, unknown types,
# raw pointer math). Used by both should_improve (gate) and score_function.
_ARTIFACT_RE = re.compile(
    r"\bFUN_[0-9a-fA-F]+\b"        # unnamed functions
    r"|\b_?DAT_[0-9a-fA-F]+\b"     # unnamed data / globals
    r"|\b[a-z]Var\d+\b"            # iVar1, uVar2, fVar3, ...
    r"|\b[a-z]Stack_[0-9a-fA-F]+\b"  # uStack_10, aStack_28, ...
    r"|\blocal_[0-9a-fA-F]+\b"     # local_18
    r"|\bparam_\d+\b"              # param_1
    r"|\bundefined\d?\b"           # undefined, undefined4
    r"|\bcode \*"                  # code * (unrecovered function pointers)
)

# A name Ghidra invented because it had no symbol (vs. a real name from debug
# info or imports). FUN_-named functions almost always have artifacts to fix.
_FUN_NAME_RE = re.compile(r"^FUN_[0-9a-fA-F]+$")

# Demangled namespaces that mark a function as standard-library / runtime code.
# Ghidra emits one decompiled .c per function across the *whole* statically
# linked binary, so a typical output is dominated by libstdc++ template
# instantiations and GNU extensions. These are already named and well
# understood -- improving them just burns LLM calls on code the user is not
# reverse-engineering.
_LIBRARY_NAMESPACES = ("std::", "__gnu_cxx::", "__cxxabiv1::", "__cxx11::")

# C-runtime / CRT / compiler-generated symbols that carry no namespace. Matched
# against the (un)qualified name and the raw-file stem (which keeps the prefix).
_LIBRARY_NAME_RE = re.compile(
    r"^(?:"
    r"__cxa_"                       # C++ ABI runtime (__cxa_finalize, ...)
    r"|__cxxabiv"                   # C++ ABI internals
    r"|_Unwind_"                    # stack unwinder
    r"|_ITM_"                       # transactional-memory clone tables
    r"|__gmon_start"               # profiling hook
    r"|__libc_"                     # glibc startup/teardown
    r"|__do_global_"               # static ctor/dtor runners
    r"|_GLOBAL__sub_"              # per-TU static init/destruct
    r"|register_tm_clones|deregister_tm_clones"
    r"|frame_dummy"                # CRT init stub
    r"|__stack_chk_"               # stack protector
    r"|__throw_"                    # libstdc++ throw helpers (often bare stems)
    r")"
)

# CRT entry/teardown stubs that forward into the runtime rather than calling
# themselves, so the forwarding-thunk detector below misses them. Matched
# exactly (these short names could otherwise be user-code prefixes).
_LIBRARY_EXACT_NAMES = frozenset({
    "_init", "_fini", "_start", "_dl_relocate_static_pie",
})

# Decompiler stubs for functions Ghidra could not recover (PLT entries, bad
# instruction data, throw/abort helpers): the body is a single call to a
# non-returning intrinsic. There is nothing to improve even though the stub
# still carries param_N artifacts that would otherwise pass the gate.
_STUB_BODY_RE = re.compile(
    r"\{\s*(?:halt_baddata|_Unwind_Resume|__assert_fail|abort)\s*\([^;{}]*\)\s*;?\s*\}"
)

# C-style block comments and line comments. Ghidra fills decompiled output with
# /* WARNING ... */ banners; stripping them first keeps the size heuristic from
# treating a one-statement stub as a substantial function.
_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def _strip_comments(code: str) -> str:
    return _COMMENT_RE.sub("", code)


def _function_name(code: str):
    """Name of the first function definition in `code`, or None."""
    try:
        items = scanner.scan_code(code)
    except Exception:
        return None
    fn = next((i for i in items if i.kind == "function"), None)
    return fn.name if fn else None


_ADDR_SUFFIX_RE = re.compile(r"-[0-9a-fA-F]+$")


def _base_names(candidate: str):
    """
    Plausible plain symbol names for a candidate, peeling off the things Ghidra
    bolts on: a `Class::method` qualifier, a leading calling-convention token
    (`processEntry _start`), and the `-<addr>` suffix on raw-file stems.
    """
    yield candidate
    base = candidate.split("::")[-1]                 # drop Class:: qualifier
    base = _ADDR_SUFFIX_RE.sub("", base)             # drop -<addr> file suffix
    yield base
    yield base.split()[-1] if base.split() else base  # drop "processEntry " etc.


def _is_library_function(qualified: str = None, name: str = None) -> bool:
    """
    True when the function is standard-library / C-runtime code rather than the
    user's own. Checks the qualified name parsed from the body (which carries
    the `std::` namespace the raw-file stem drops) plus the passed-in name.
    """
    candidates = [c for c in (qualified, name) if c]
    for c in candidates:
        if any(c.startswith(ns) or f"::{ns}" in c for ns in _LIBRARY_NAMESPACES):
            return True

    bases = {b for c in candidates for b in _base_names(c)}
    return any(_LIBRARY_NAME_RE.match(b) or b in _LIBRARY_EXACT_NAMES
              for b in bases)


def _is_stub(code: str) -> bool:
    """A body whose only statement is a non-returning decompiler intrinsic."""
    return bool(_STUB_BODY_RE.search(_strip_comments(code)))


def _is_forwarding_thunk(code: str) -> bool:
    """
    A PLT/import thunk: a branch-free body whose only call is to the symbol of
    the same name. Ghidra renders imported libc/libstdc++ symbols this way
    (e.g. `strlen` calling `strlen`, `operator_new` calling `operator_new`).
    The branch-free check distinguishes these from genuine recursion.
    """
    try:
        items = scanner.scan_code(code)
        fn = next((i for i in items if i.kind == "function"), None)
        if not fn or set(fn.dependencies) != {fn.name}:
            return False
        return scanner.complexity_metrics(code)["cyclomatic"] == 1
    except Exception:
        return False


def should_improve(code: str, name: str = None) -> bool:
    """
    Gate deciding whether a function is worth an LLM call. Conservative: it
    only skips when there is high confidence that little can be gained, so the
    cost is a few wasted calls rather than silently dropping useful work.

    Skips:
      - standard-library / C-runtime code (`std::...`, `__cxa_*`, `_Unwind_*`,
        etc.) -- the bulk of a statically linked binary, already named and not
        what the user is reverse-engineering;
      - empty or trivially short bodies (< 5 non-comment, non-blank lines);
      - decompiler stubs whose only statement is a non-returning intrinsic
        (e.g. `halt_baddata()`), even when they carry param_N artifacts;
      - explicit thunks (`thunk_*`) and PLT import thunks (a branch-free body
        forwarding to its own symbol, e.g. `strlen` calling `strlen`);
      - already-named, artifact-free functions (Ghidra had real symbols, e.g.
        from debug info or imports, so there is little readability to recover).
    """
    if not code or len(code.strip()) == 0:
        return False

    qualified = _function_name(code)
    if name is None:
        name = qualified

    # Standard library / runtime code: nothing the user cares to recover.
    if _is_library_function(qualified, name):
        return False

    # Count only real code lines: Ghidra emits many /* WARNING */ comment lines
    # that would otherwise push a one-statement stub over the size threshold.
    lines = [l for l in _strip_comments(code).splitlines() if l.strip()]
    if len(lines) < 5:
        return False

    if name and name.startswith("thunk_"):
        return False

    # A pure decompiler stub (single non-returning intrinsic call) or a PLT
    # import thunk (forwards to its own symbol) has nothing to recover,
    # regardless of the param_N artifacts in its signature.
    if _is_stub(code) or _is_forwarding_thunk(code):
        return False

    # Real name + no decompiler artifacts => the decompiler already had symbols.
    artifacts = len(_ARTIFACT_RE.findall(code))
    if name and not _FUN_NAME_RE.match(name) and artifacts == 0:
        return False

    return True


def score_function(code: str) -> float:
    """
    Improvability score: higher means more readability to recover, so process
    it earlier in a batch (and it could later drive model selection). Combines
    Ghidra-artifact density with AST structural complexity. Cheap: one regex
    pass plus one tree-sitter walk, no LLM call.
    """
    if not code or not code.strip():
        return 0.0

    lines = [l for l in code.splitlines() if l.strip()] or [""]
    artifact_density = len(_ARTIFACT_RE.findall(code)) / len(lines)

    try:
        m = scanner.complexity_metrics(code)
    except Exception:
        m = {"cyclomatic": 1, "calls": 0, "max_depth": 0}

    return (
        3.0 * artifact_density
        + 1.0 * (m["cyclomatic"] - 1)
        + 0.5 * m["calls"]
        + 0.5 * m["max_depth"]
    )


def extract_code(text: str) -> str:
    """
    Pulls C++ out of an LLM response. Prefers the first fenced code block;
    falls back to the stripped text when the model returned bare code.
    """
    match = _CODE_BLOCK_RE.search(text)
    if match:
        return match.group(1).strip()
    # No fence: strip any stray fence markers and surrounding whitespace.
    return text.replace("```cpp", "").replace("```", "").strip()


def _project_function_names(raw_dir: Path) -> set:
    """Set of decompiled function names (raw .c file stems)."""
    if not raw_dir.exists():
        return set()
    return {p.stem for p in raw_dir.rglob("*.c")}


def _dependencies(code: str, valid_names: set, index=None) -> list:
    """
    Project functions called by `code`, resolved to the names `valid_names`
    uses. Matching goes through `scanner.NameIndex`: call sites carry a bare or
    qualified name while `valid_names` holds ghidrecomp's `<symbol>-<address>`
    stems, and a bare stem like `size` may belong to any of several functions.
    Ambiguous calls resolve to nothing rather than to a guess.

    `index` lets a batch caller build the index once instead of per function.
    """
    try:
        items = scanner.scan_code(code)
    except Exception as e:
        print(f"Dependency scan warning: {e}")
        return []

    target = next((i for i in items if i.kind == "function"), None)
    if not target or not target.dependencies:
        return []

    if index is None:
        if not valid_names:
            return sorted(set(target.dependencies))
        index = scanner.NameIndex(valid_names)

    # `target.name` is the bare name from the source while a resolved dep is the
    # `<symbol>-<address>` key, so self-recursion only shows up once normalized.
    # Left unfiltered it makes a function its own dependency, and the recursive
    # improve path would then try to improve it as its own callee.
    own = scanner.normalize_name(target.name)
    deps = set()
    for dep in target.dependencies:
        resolved = index.resolve(dep)
        if resolved and scanner.normalize_name(resolved) != own:
            deps.add(resolved)
    return sorted(deps)


def _stream_response(llm, convo, stream_callback=None) -> str:
    """
    Streams a single LLM response, forwarding the running text to
    `stream_callback` as it arrives. Falls back to a blocking invoke if the
    client does not support streaming. Returns the full raw response text.
    """
    raw = ""
    try:
        for chunk in llm.stream(convo):
            piece = getattr(chunk, "content", "") or ""
            if piece:
                raw += piece
                if stream_callback:
                    stream_callback(raw)
        return raw
    except (AttributeError, NotImplementedError):
        # Client without streaming support: one blocking call.
        response = llm.invoke(convo)
        raw = response.content
        if stream_callback:
            stream_callback(raw)
        return raw


def _invoke_with_validation(
    llm, messages, status_callback=None, stream_callback=None, attempts=2
) -> str:
    """
    Streams the LLM response and validates that the output parses as C++. On
    failure it retries once with corrective feedback. Returns the first valid
    result, or the last attempt if none validate.
    """
    import time

    last = ""
    convo = list(messages)
    for attempt in range(attempts):
        if status_callback:
            label = "Generating" if attempt == 0 else f"Retrying (attempt {attempt + 1})"
            status_callback(f"{label} — streaming response from model...")

        start = time.monotonic()
        raw = _stream_response(llm, convo, stream_callback)
        elapsed = time.monotonic() - start
        last = extract_code(raw)

        if status_callback:
            status_callback(
                f"Received {len(raw)} chars in {elapsed:.1f}s; validating C++..."
            )

        if scanner.is_valid_cpp(last):
            if status_callback:
                status_callback("✅ Output parsed as valid C++.")
            return last
        if status_callback:
            status_callback(f"⚠️ Output did not parse as C++ (attempt {attempt + 1}); retrying...")
        convo = list(messages) + [
            ("assistant", raw),
            (
                "user",
                "Your previous output did not parse as valid C++ (unbalanced braces, "
                "truncation, or stray prose). Return ONLY corrected, compilable C++ "
                "code with no commentary outside of code comments.",
            ),
        ]

    if status_callback:
        status_callback("⚠️ Warning: returning unvalidated output after retries.")
    return last


def improve_function(
    code: str,
    provider="gemini",
    model_name="openai/gpt-oss-20b",
    binary_path=None,
    recursive=False,
    status_callback=None,
    stream_callback=None,
    symbols=None,
    valid_names=None,
    base_prompt=None,
    extra_context=None,
) -> str:
    """
    Sends the code to the selected LLM provider for improvement.

    Args:
        code: The C++ code to improve.
        provider: LLM provider name.
        model_name: Model identifier.
        binary_path: Path to the binary (used to locate workspace/context).
        recursive: If True, improves direct project callees (1 layer deep) first.
        status_callback: Optional function(str) to report progress / debug.
        stream_callback: Optional function(str) called with the running raw
            model output as it streams in, for live display.
        symbols: Shared signature map (dict). When the caller owns it (batch),
            improve_function only reads/updates it; otherwise it is loaded and
            saved here so single-function runs still accumulate context.
        valid_names: Set of known project function names for dependency filtering.
        base_prompt: Optional base system instruction. Defaults to
            DEFAULT_SYSTEM_PROMPT; the experiment framework injects evolved
            prompt variants here.
        extra_context: Optional pre-formatted context block to inject verbatim.
            When supplied (even ""), it REPLACES the binary_path-based context
            gathering, so the context-retrieval experiment controls exactly what
            the model sees while reusing this same prompt assembly. None (default)
            keeps the normal disk/symbol-map gathering.
    """
    if not should_improve(code):
        return code  # Return original if we skip

    additional_context = "" if extra_context is None else extra_context
    owns_symbols = symbols is None
    if symbols is None:
        symbols = {}
    workspace_dir = None

    if binary_path and extra_context is None:
        bin_name = Path(binary_path).name
        workspace_dir = Path(os.getcwd()) / "workspace" / bin_name
        improved_dir = workspace_dir / "improved"
        raw_dir = workspace_dir / "raw"
        project_h = workspace_dir / "project.h"

        os.makedirs(improved_dir, exist_ok=True)

        if owns_symbols:
            symbols = symbol_map.load_symbols(workspace_dir)

        if valid_names is None:
            valid_names = _project_function_names(raw_dir)

        deps = _dependencies(code, valid_names)
        if status_callback:
            status_callback(
                f"Found {len(deps)} project dependency(ies)"
                + (f": {', '.join(deps)}" if deps else ".")
            )

        # 0. Recursive improvement of direct callees (one layer deep).
        if recursive and deps:
            for dep in deps:
                dep_improved_path = improved_dir / f"{dep}.cpp"
                if dep_improved_path.exists():
                    continue
                raw_matches = list(raw_dir.rglob(f"{dep}*.c"))
                if not raw_matches:
                    continue
                try:
                    if status_callback:
                        status_callback(f"Recursively improving dependency: {dep}")

                    raw_dep_code = raw_matches[0].read_text(encoding="utf-8")
                    new_dep_code = improve_function(
                        raw_dep_code,
                        provider,
                        model_name,
                        binary_path,
                        recursive=False,
                        status_callback=status_callback,
                        symbols=symbols,
                        valid_names=valid_names,
                    )
                    # Same rule as the batch path: only cache output that parses,
                    # since a written file is never revisited.
                    if not scanner.is_valid_cpp(new_dep_code):
                        raise ImprovementError("improved output does not parse as C++")
                    dep_improved_path.write_text(new_dep_code, encoding="utf-8")
                    symbol_map.record_improvement(symbols, dep, new_dep_code)
                except Exception as e:
                    msg = f"Recursive improve failed for {dep}: {e}"
                    if status_callback:
                        status_callback(msg)
                    print(msg)

        # 1. Project types.
        if project_h.exists():
            try:
                additional_context += (
                    "### Project Types (project.h)\n```cpp\n"
                    f"{project_h.read_text(encoding='utf-8')}\n```\n\n"
                )
            except Exception:
                pass

        # 2. Improved callee signatures, pulled from the symbol map (populating
        #    it from disk for any callee already improved in a previous run).
        for dep in deps:
            if dep not in symbols:
                dep_file = improved_dir / f"{dep}.cpp"
                if dep_file.exists():
                    symbol_map.record_improvement(symbols, dep, dep_file.read_text(encoding="utf-8"))
        additional_context += symbol_map.format_callee_context(symbols, deps)

    # --- Construct Prompt ---
    # Use the caller-supplied base instruction when given (this is the knob the
    # experiment framework optimizes); otherwise fall back to the default.
    system_prompt = base_prompt if base_prompt is not None else DEFAULT_SYSTEM_PROMPT

    if additional_context:
        system_prompt += (
            "\n\n### CONTEXT PROVIDED\n"
            "You have been provided with 'Project Types' and/or 'Improved Callee Signatures'. "
            "USE THEM. If the code calls a function found in the context, match its improved "
            "name and argument types exactly. "
            "Use the defined structs from Project Types instead of raw pointers/bytes where applicable."
        )

    try:
        if status_callback:
            endpoint = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1") if provider == "local" else provider
            status_callback(f"Initializing LLM (provider={provider}, model={model_name}, endpoint={endpoint})...")

        llm = get_llm(provider, model_name)
        if not llm:
            raise ImprovementError(
                f"LLM client could not be initialized for provider={provider} "
                "(check API keys/env)."
            )

        user_msg = f"{additional_context}Code to Improve:\n{code}"
        messages = [
            ("system", system_prompt),
            ("user", user_msg),
        ]

        if status_callback:
            ctx_note = f" (incl. {len(additional_context)} chars of context)" if additional_context else ""
            status_callback(f"Sending {len(user_msg)} chars to model{ctx_note}; waiting for first token...")

        result = _invoke_with_validation(
            llm, messages, status_callback=status_callback, stream_callback=stream_callback
        )
    except ImprovementError:
        raise
    except Exception as e:
        raise ImprovementError(f"AI improvement failed: {e}") from e
    finally:
        # Persist whatever context we accumulated (including any recursively
        # improved callees) even when this call failed.
        if owns_symbols and workspace_dir is not None:
            symbol_map.save_symbols(workspace_dir, symbols)

    return result
