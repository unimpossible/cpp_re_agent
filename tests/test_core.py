import pytest
import io
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
from cpp_re_agent import decompiler
from cpp_re_agent import ai_improver
from cpp_re_agent import symbol_map
from cpp_re_agent.llm_factory import get_llm

# --- Fixtures ---

@pytest.fixture
def workspace_dir(tmp_path):
    """Creates a temporary workspace directory."""
    d = tmp_path / "workspace"
    d.mkdir()
    return d

@pytest.fixture
def mock_binary(tmp_path):
    """Creates a dummy binary file."""
    binary = tmp_path / "test_bin.exe"
    binary.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00") # Minimal fake header
    return binary

@pytest.fixture
def mock_llm_response():
    """Mocks the LangChain LLM response."""
    mock_msg = MagicMock()
    mock_msg.content = """
    struct User {
        int id;
        void login() {}
    };
    """
    return mock_msg

# --- Decompiler Tests ---

def test_decompiler_raises_on_missing_output_dir(workspace_dir):
    """
    A missing output dir must raise, not look like a binary with no functions.
    (This test previously asserted the MOCK_FUNCTIONS fallback, which was
    removed in 8b21fc9 — mock data must not masquerade as real results.)
    """
    with pytest.raises(decompiler.DecompilationError, match="does not exist"):
        decompiler.get_functions(str(workspace_dir / "non_existent"))


def test_decompiler_returns_empty_for_empty_output_dir(workspace_dir):
    """An existing but empty dir is a real (if useless) result, not an error."""
    empty = workspace_dir / "empty"
    empty.mkdir()
    assert decompiler.get_functions(str(empty)) == {}

def test_decompiler_finds_files(workspace_dir):
    """Test recursive globbing of C files."""
    # Setup nested structure mimicking ghidrecomp
    nested = workspace_dir / "bins" / "hash" / "decomps"
    nested.mkdir(parents=True)
    
    (nested / "main.c").write_text("int main() {}")
    (nested / "sub.c").write_text("void sub() {}")
    
    funcs = decompiler.get_functions(str(workspace_dir))
    
    assert "main" in funcs
    assert "sub" in funcs
    assert funcs["main"] == "int main() {}"

def test_decompiler_skips_if_cached(mock_binary, workspace_dir, capsys):
    """Test the MD5 caching logic."""
    output_dir = workspace_dir / "out"
    output_dir.mkdir()
    
    # Calculate hash of mock binary
    import hashlib
    with open(mock_binary, "rb") as f:
        digest = hashlib.md5(f.read()).hexdigest()
        
    expected_cache_dir = output_dir / "bins" / f"{mock_binary.name}-{digest}"
    expected_cache_dir.mkdir(parents=True)
    
    # Run decompiler
    with patch("subprocess.run") as mock_run:
        decompiler.decompile_binary(str(mock_binary), str(output_dir))
        
        # Should NOT have called ghidrecomp because cache exists
        mock_run.assert_not_called()
        
    captured = capsys.readouterr()
    assert "Skipping decompilation" in captured.out

# --- AI Improver Tests ---

def test_should_improve_filtering():
    """Test the heuristics for skipping simple functions."""
    # Too short
    short_code = "void thunk() { return; }"
    assert ai_improver.should_improve(short_code) is False
    
    # Long enough
    long_code = "\n".join([f"line {i};" for i in range(15)])
    assert ai_improver.should_improve(long_code) is True


def test_should_improve_skips_thunk_by_name():
    """Functions Ghidra names thunk_* are delegating wrappers, not worth a call."""
    code = (
        "void thunk_FUN_00401000(int param_1)\n"
        "{\n"
        "  FUN_00401000(param_1);\n"
        "  return;\n"
        "}\n"
    )
    assert ai_improver.should_improve(code, name="thunk_FUN_00401000") is False


def test_should_improve_skips_named_and_clean():
    """A real-named function with no decompiler artifacts has little to recover."""
    code = (
        "int calculate_checksum(int length, char *data)\n"
        "{\n"
        "    int total = 0;\n"
        "    for (int idx = 0; idx < length; idx = idx + 1)\n"
        "        total = total + data[idx];\n"
        "    return total;\n"
        "}\n"
    )
    assert ai_improver.should_improve(code, name="calculate_checksum") is False


def test_should_improve_keeps_artifact_heavy_function():
    """A FUN_-named, artifact-laden function is exactly what we want to improve."""
    code = (
        "undefined4 FUN_00401000(int param_1)\n"
        "{\n"
        "    int iVar1;\n"
        "    undefined4 uVar2;\n"
        "    iVar1 = *(int *)(param_1 + 0x10);\n"
        "    uVar2 = FUN_00402000(iVar1);\n"
        "    return uVar2;\n"
        "}\n"
    )
    assert ai_improver.should_improve(code, name="FUN_00401000") is True


def test_should_improve_skips_std_namespace():
    """Standard-library code (parsed std:: namespace) is not the user's code."""
    code = (
        "/* std::_Vector_base<int, std::allocator<int> >::_M_allocate(unsigned long) */\n"
        "undefined8 __thiscall\n"
        "std::_Vector_base<int,std::allocator<int>>::_M_allocate\n"
        "          (_Vector_base<int,std::allocator<int>> *this,ulong param_1)\n"
        "{\n"
        "  undefined8 uVar1;\n"
        "  if (param_1 == 0) { uVar1 = 0; }\n"
        "  else { uVar1 = __new_allocator<int>::allocate((ulong)this,(void *)param_1); }\n"
        "  return uVar1;\n"
        "}\n"
    )
    # std:: even though the raw-file stem ("_M_allocate") drops the namespace.
    assert ai_improver.should_improve(code, name="_M_allocate") is False


def test_should_improve_skips_c_runtime_by_name():
    """C-runtime symbols (__cxa_*, _Unwind_*, _ITM_*) are not user code."""
    code = (
        "void __cxa_finalize(void)\n"
        "{\n"
        "  /* filler */\n"
        "  int x = 0;\n"
        "  int y = 1;\n"
        "  int z = x + y;\n"
        "  return;\n"
        "}\n"
    )
    assert ai_improver.should_improve(code, name="__cxa_finalize-001021c0") is False


def test_should_improve_skips_comment_heavy_stub():
    """A halt_baddata() stub stays trivial once Ghidra's WARNING comments are
    stripped, and is skipped despite carrying a param_N artifact."""
    code = (
        "/* WARNING: Control flow encountered bad instruction data */\n"
        "/* WARNING: Unknown calling convention -- yet parameter storage is locked */\n"
        "void mystery_thrower(char *param_1)\n"
        "{\n"
        "  /* WARNING: Bad instruction - Truncating control flow here */\n"
        "  halt_baddata();\n"
        "}\n"
    )
    assert ai_improver.should_improve(code, name="mystery_thrower") is False


def test_should_improve_skips_plt_forwarding_thunk():
    """A branch-free body that only calls its own symbol is a PLT import thunk."""
    code = (
        "/* WARNING: Unknown calling convention -- yet parameter storage is locked */\n"
        "size_t strlen(char *__s)\n"
        "{\n"
        "  size_t sVar1;\n"
        "  sVar1 = strlen(__s);\n"
        "  return sVar1;\n"
        "}\n"
    )
    assert ai_improver.should_improve(code, name="strlen") is False


def test_should_improve_keeps_genuine_recursion():
    """Real recursion calls itself but branches, so it is NOT a forwarding thunk."""
    code = (
        "int factorial(int param_1)\n"
        "{\n"
        "  if (param_1 <= 1) { return 1; }\n"
        "  return param_1 * factorial(param_1 + -1);\n"
        "}\n"
    )
    assert ai_improver.should_improve(code, name="factorial") is True


def test_should_improve_keeps_user_namespaced_method():
    """A method in the user's own class/namespace must still be improved."""
    code = (
        "void __thiscall User::User(User *this,int param_1,string *param_2)\n"
        "{\n"
        "  *(int *)this = param_1;\n"
        "  std::__cxx11::string::string((string *)(this + 8),param_2);\n"
        "  *(undefined8 *)(this + 0x40) = 0;\n"
        "  return;\n"
        "}\n"
    )
    assert ai_improver.should_improve(code, name="User") is True


def test_score_prefers_artifact_heavy_code():
    """score_function ranks an artifact/branch-heavy function above a clean one."""
    clean = (
        "int add(int a, int b)\n"
        "{\n"
        "    return a + b;\n"
        "}\n"
    )
    gnarly = (
        "undefined4 FUN_00401000(int param_1)\n"
        "{\n"
        "    int iVar1;\n"
        "    undefined4 uVar2;\n"
        "    if (param_1 == 0) { uVar2 = 0; }\n"
        "    else { iVar1 = *(int *)(param_1 + 0x10); uVar2 = FUN_00402000(iVar1); }\n"
        "    return uVar2;\n"
        "}\n"
    )
    assert ai_improver.score_function(gnarly) > ai_improver.score_function(clean)

def test_improve_function_calls_llm(mock_llm_response):
    """Test that improve_function invokes the LLM correctly."""
    code = "\n".join([f"iVar{i} = 0;" for i in range(20)]) # 20 lines
    
    # Mock get_llm to return a mock object. improve_function streams the
    # response (llm.stream), so the mock must yield chunks with .content.
    with patch("cpp_re_agent.llm_factory.get_llm") as mock_get_llm:
        mock_chain = MagicMock()
        mock_chain.stream.return_value = iter([mock_llm_response])
        mock_get_llm.return_value = mock_chain

        result = ai_improver.improve_function(code, provider="local")

        # Verify result content
        assert "struct User" in result

        # Verify factory called
        mock_get_llm.assert_called_once()

        # Verify the model was streamed with the right prompt
        mock_chain.stream.assert_called_once()
        args, _ = mock_chain.stream.call_args
        messages = args[0]
        assert "expert C++ Reverse Engineer" in messages[0][1] # System prompt
        assert code in messages[1][1] # User prompt


# --- Failure handling: failed LLM calls must never be cached as code ---------
#
# Regression tests for the bug where improve_function/refine_function returned
# error *strings* ("// Error: ...") that callers wrote to disk. Because
# batch_improve skips any function whose improved/<name>.cpp already exists, a
# single transient failure produced a junk file that was never retried, and its
# bogus signature propagated to every later caller via the symbol map.

GNARLY = (
    "undefined4 FUN_00401000(int param_1)\n"
    "{\n"
    "    int iVar1;\n"
    "    undefined4 uVar2;\n"
    "    undefined4 uVar3;\n"
    "    if (param_1 == 0) { uVar2 = 0; }\n"
    "    else {\n"
    "        iVar1 = *(int *)(param_1 + 0x10);\n"
    "        uVar3 = *(undefined4 *)(param_1 + 0x14);\n"
    "        uVar2 = FUN_00402000(iVar1, uVar3);\n"
    "    }\n"
    "    return uVar2;\n"
    "}\n"
)


def _streaming_llm(content):
    """A mock LLM whose .stream() yields one chunk with `content`."""
    chunk = MagicMock()
    chunk.content = content
    llm = MagicMock()
    llm.stream.return_value = iter([chunk])
    return llm


def test_improve_function_raises_when_llm_unavailable():
    """A missing API key must raise, not return '// Error' text as if it were code."""
    from cpp_re_agent.llm_factory import ImprovementError

    with patch("cpp_re_agent.llm_factory.get_llm", return_value=None):
        with pytest.raises(ImprovementError, match="could not be initialized"):
            ai_improver.improve_function(GNARLY, provider="gemini")


def test_improve_function_raises_on_llm_exception():
    """A transient endpoint failure must propagate, not become the 'improved' code."""
    from cpp_re_agent.llm_factory import ImprovementError

    llm = MagicMock()
    llm.stream.side_effect = RuntimeError("429 rate limit")
    with patch("cpp_re_agent.llm_factory.get_llm", return_value=llm):
        with pytest.raises(ImprovementError) as exc:
            ai_improver.improve_function(GNARLY, provider="local")
    # The underlying cause is preserved for the status line / st.error.
    assert "429 rate limit" in str(exc.value)
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_refine_function_raises_instead_of_returning_error_text():
    """contextual_improver had the same bug, plus no null-check on get_llm."""
    from cpp_re_agent import contextual_improver
    from cpp_re_agent.llm_factory import ImprovementError

    # refine_function streams (see llm_factory.stream_text), so the failure
    # surfaces from .stream rather than .invoke.
    llm = MagicMock()
    llm.stream.side_effect = RuntimeError("connection reset")
    with patch("cpp_re_agent.llm_factory.get_llm", return_value=llm):
        with pytest.raises(ImprovementError, match="connection reset"):
            contextual_improver.refine_function("int f(){return 0;}", "// hdr", "// callees")

    with patch("cpp_re_agent.llm_factory.get_llm", return_value=None):
        with pytest.raises(ImprovementError, match="could not be initialized"):
            contextual_improver.refine_function("int f(){return 0;}", "// hdr", "// callees")


def test_batch_improve_writes_nothing_when_the_llm_fails(tmp_path):
    """The core poisoning scenario: a failed call must leave no file behind."""
    from cpp_re_agent import pipeline

    workspace = tmp_path / "ws"
    functions = {"FUN_00401000": GNARLY}

    with patch("cpp_re_agent.llm_factory.get_llm", return_value=None):
        result = pipeline.batch_improve(
            functions, workspace, binary_path=None, provider="gemini")

    improved = workspace / "improved" / "FUN_00401000.cpp"
    assert not improved.exists(), "a failed improvement must not be cached to disk"
    assert result.failed == ["FUN_00401000"]
    assert result.improved == []
    # And the bogus signature must not have entered the symbol map.
    assert "FUN_00401000" not in symbol_map.load_symbols(workspace)


def test_batch_improve_retries_a_failed_function_on_the_next_run(tmp_path):
    """
    The point of not writing: because batch_improve skips on file existence, a
    junk file would be permanent. A failure then a success must yield real code.
    """
    from cpp_re_agent import pipeline

    workspace = tmp_path / "ws"
    functions = {"FUN_00401000": GNARLY}

    # Run 1: endpoint is down.
    with patch("cpp_re_agent.llm_factory.get_llm", return_value=None):
        first = pipeline.batch_improve(functions, workspace, None, provider="gemini")
    assert first.failed == ["FUN_00401000"]

    # Run 2: endpoint is back. Nothing was cached, so it is retried.
    good = "int process_record(int handle)\n{\n    return handle;\n}\n"
    with patch("cpp_re_agent.llm_factory.get_llm", return_value=_streaming_llm(good)):
        second = pipeline.batch_improve(functions, workspace, None, provider="local")

    improved = workspace / "improved" / "FUN_00401000.cpp"
    assert second.improved == ["FUN_00401000"]
    assert improved.exists()
    assert "process_record" in improved.read_text(encoding="utf-8")
    assert "// Error" not in improved.read_text(encoding="utf-8")


def test_batch_improve_rejects_output_that_does_not_parse(tmp_path):
    """
    Even a 'successful' call must not be cached if the result isn't valid C++ —
    _invoke_with_validation returns its best effort after retries, and a written
    file is never revisited.
    """
    from cpp_re_agent import pipeline

    workspace = tmp_path / "ws"
    functions = {"FUN_00401000": GNARLY}
    garbage = "Sure! Here is the improved function: int f( {{{ unbalanced"

    with patch("cpp_re_agent.llm_factory.get_llm", return_value=_streaming_llm(garbage)):
        result = pipeline.batch_improve(functions, workspace, None, provider="local")

    assert not (workspace / "improved" / "FUN_00401000.cpp").exists()
    assert result.failed == ["FUN_00401000"]


def test_run_contextual_keeps_prior_version_when_refinement_fails(tmp_path):
    """Stage 2 must not overwrite good stage-1 output with an error."""
    from cpp_re_agent import pipeline

    workspace = tmp_path / "ws"
    improved_dir = workspace / "improved"
    improved_dir.mkdir(parents=True)
    original = "int process_record(int handle)\n{\n    return handle;\n}\n"
    target = improved_dir / "process_record.cpp"
    target.write_text(original, encoding="utf-8")

    llm = MagicMock()
    llm.invoke.side_effect = RuntimeError("500 server error")
    with patch("cpp_re_agent.llm_factory.get_llm", return_value=llm):
        refined = pipeline.run_contextual(workspace, status=lambda _m: None)

    assert refined == []
    assert target.read_text(encoding="utf-8") == original


# --- Decompilation failures must be visible ---------------------------------
#
# Regression tests for the bug where decompile_binary returned quietly on a
# missing binary and lost ghidrecomp's stderr, so the UI reported
# "Decompilation Complete!" for a run that never happened.

def test_decompile_binary_raises_on_missing_binary(tmp_path):
    """A silent return here is what let the UI claim success."""
    with pytest.raises(decompiler.DecompilationError, match="Binary not found"):
        decompiler.decompile_binary(str(tmp_path / "nope.exe"), str(tmp_path / "out"))


def test_decompile_binary_surfaces_ghidrecomp_stderr(mock_binary, tmp_path):
    """
    subprocess.run(capture_output=True) means CalledProcessError's str() reports
    only the exit status — the real Ghidra error must be pulled out of stderr.
    """
    import subprocess

    err = b"ERROR: Ghidra headless analyzer failed: unsupported loader for format\n"
    with patch("subprocess.run",
               side_effect=subprocess.CalledProcessError(1, "ghidrecomp", b"", err)):
        with pytest.raises(decompiler.DecompilationError) as exc:
            decompiler.decompile_binary(str(mock_binary), str(tmp_path / "out"))

    msg = str(exc.value)
    assert "unsupported loader for format" in msg, "ghidrecomp's stderr must reach the caller"
    assert "exit 1" in msg


def test_decompile_binary_reports_missing_tool(mock_binary, tmp_path):
    """A missing ghidrecomp should say so, not surface as a bare OSError."""
    with patch("subprocess.run", side_effect=FileNotFoundError("ghidrecomp")):
        with pytest.raises(decompiler.DecompilationError, match="ghidrecomp not found"):
            decompiler.decompile_binary(str(mock_binary), str(tmp_path / "out"))


def test_decompile_binary_prefers_the_module_invocation(mock_binary, tmp_path):
    """
    ghidrecomp is run as `python -m ghidrecomp`, not via the console-script
    shim: the shim depends on PATH and is blocked outright by Windows
    Application Control on some machines.
    """
    import sys

    with patch("subprocess.run") as run:
        decompiler.decompile_binary(str(mock_binary), str(tmp_path / "out"))

    cmd = run.call_args[0][0]
    assert cmd[:3] == [sys.executable, "-m", "ghidrecomp"], cmd
    assert str(mock_binary) in cmd


def test_run_stage1_propagates_decompilation_failure(tmp_path):
    """The pipeline must not go on to 'improve' an empty function set."""
    from cpp_re_agent import pipeline

    with pytest.raises(decompiler.DecompilationError):
        pipeline.run_stage1(str(tmp_path / "nope.exe"),
                            output_dir=str(tmp_path / "ws"),
                            status=lambda _m: None)


# --- Callgraph levels + parallel batch --------------------------------------

def test_topological_levels_places_callees_below_callers():
    from cpp_re_agent import callgraph

    # main -> {parse, emit};  parse -> {lex};  emit -> {lex};  lex -> {}
    graph = {"main": {"parse", "emit"}, "parse": {"lex"}, "emit": {"lex"}, "lex": set()}
    levels = callgraph.topological_levels(graph)

    depth = {n: i for i, level in enumerate(levels) for n in level}
    assert depth["lex"] == 0
    assert depth["parse"] == 1 and depth["emit"] == 1, "siblings share a level"
    assert depth["main"] == 2
    # The invariant, stated directly.
    for node, deps in graph.items():
        for dep in deps:
            assert depth[dep] < depth[node]


def test_topological_levels_flattens_to_a_valid_order():
    """Flattening levels must still be a leaves-first order."""
    from cpp_re_agent import callgraph

    graph = {"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set(), "e": {"a"}}
    flat = [n for level in callgraph.topological_levels(graph) for n in level]
    pos = {n: i for i, n in enumerate(flat)}
    assert sorted(flat) == sorted(graph)
    for node, deps in graph.items():
        for dep in deps:
            assert pos[dep] < pos[node]


def test_topological_levels_survives_cycles():
    """Recursion / mutual recursion must not hang or drop nodes."""
    from cpp_re_agent import callgraph

    graph = {"a": {"b"}, "b": {"a"}, "c": {"a"}, "self": {"self"}}
    levels = callgraph.topological_levels(graph)
    flat = [n for level in levels for n in level]
    assert sorted(flat) == ["a", "b", "c", "self"]


def test_parallel_batch_improves_callees_before_callers(tmp_path):
    """
    The reason parallelism is per-level: a caller must still see its callees'
    improved signatures. Records the order in which the LLM was actually asked
    for each function and asserts callees came first.
    """
    import threading
    from cpp_re_agent import pipeline

    functions = {
        "lex":   "int lex(int param_1)\n{\n" + "\n".join(
                 f"    int iVar{i} = param_1 + {i};" for i in range(12)) + "\n    return iVar1;\n}\n",
        "parse": "int parse(int param_1)\n{\n    int iVar1 = lex(param_1);\n" + "\n".join(
                 f"    int uVar{i} = iVar1 + {i};" for i in range(12)) + "\n    return uVar1;\n}\n",
        "emit":  "int emit(int param_1)\n{\n    int iVar1 = lex(param_1);\n" + "\n".join(
                 f"    int uVar{i} = iVar1 * {i};" for i in range(12)) + "\n    return uVar1;\n}\n",
        "main":  "int main(void)\n{\n    int iVar1 = parse(1);\n    int iVar2 = emit(2);\n" + "\n".join(
                 f"    int uVar{i} = iVar1 + {i};" for i in range(12)) + "\n    return uVar1;\n}\n",
    }

    started = []
    lock = threading.Lock()

    def fake_improve(code, **kwargs):
        name = next(n for n, c in functions.items() if c == code)
        with lock:
            started.append(name)
        return f"int {name}_improved(int handle)\n{{\n    return handle;\n}}\n"

    with patch("cpp_re_agent.ai_improver.improve_function", side_effect=fake_improve):
        result = pipeline.batch_improve(
            functions, tmp_path / "ws", None, max_workers=4)

    assert set(result.improved) == set(functions), result
    order = {n: i for i, n in enumerate(started)}
    assert order["lex"] < order["parse"], "callee must be improved before its caller"
    assert order["lex"] < order["emit"]
    assert order["parse"] < order["main"]
    assert order["emit"] < order["main"]


def test_parallel_batch_actually_overlaps_calls(tmp_path):
    """Independent functions in one level must run concurrently, not back to back."""
    import threading
    from cpp_re_agent import pipeline

    # Four mutually independent functions -> all in level 0.
    functions = {
        f"FUN_0040{i}000": (
            f"undefined4 FUN_0040{i}000(int param_1)\n{{\n"
            + "\n".join(f"    int iVar{j} = param_1 + {j};" for j in range(12))
            + "\n    return iVar1;\n}\n"
        )
        for i in range(4)
    }

    barrier = threading.Barrier(4, timeout=10)

    def fake_improve(code, **kwargs):
        # Deadlocks (and fails the test) unless 4 calls are in flight at once.
        barrier.wait()
        name = next(n for n, c in functions.items() if c == code)
        return f"int {name}_improved(int handle)\n{{\n    return handle;\n}}\n"

    with patch("cpp_re_agent.ai_improver.improve_function", side_effect=fake_improve):
        result = pipeline.batch_improve(
            functions, tmp_path / "ws", None, max_workers=4)

    assert sorted(result.improved) == sorted(functions)


def test_batch_improve_max_workers_1_is_sequential(tmp_path):
    """The sequential path must stay available and strictly ordered."""
    import threading
    import time
    from cpp_re_agent import pipeline

    functions = {
        f"FUN_0040{i}000": (
            f"undefined4 FUN_0040{i}000(int param_1)\n{{\n"
            + "\n".join(f"    int iVar{j} = param_1 + {j};" for j in range(12))
            + "\n    return iVar1;\n}\n"
        )
        for i in range(3)
    }

    concurrent, peak = 0, 0
    lock = threading.Lock()

    def fake_improve(code, **kwargs):
        nonlocal concurrent, peak
        with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        time.sleep(0.01)
        with lock:
            concurrent -= 1
        name = next(n for n, c in functions.items() if c == code)
        return f"int {name}_improved(int handle)\n{{\n    return handle;\n}}\n"

    with patch("cpp_re_agent.ai_improver.improve_function", side_effect=fake_improve):
        pipeline.batch_improve(functions, tmp_path / "ws", None, max_workers=1)

    assert peak == 1, f"max_workers=1 must not overlap calls (peak={peak})"


def test_parallel_batch_isolates_failures_and_respects_limit(tmp_path):
    """One failure in a level must not lose its siblings, and --limit still caps."""
    from cpp_re_agent import pipeline

    functions = {
        f"FUN_0040{i}000": (
            f"undefined4 FUN_0040{i}000(int param_1)\n{{\n"
            + "\n".join(f"    int iVar{j} = param_1 + {j};" for j in range(12))
            + "\n    return iVar1;\n}\n"
        )
        for i in range(4)
    }

    def fake_improve(code, **kwargs):
        name = next(n for n, c in functions.items() if c == code)
        if name == "FUN_00401000":
            raise RuntimeError("429 rate limit")
        return f"int {name}_improved(int handle)\n{{\n    return handle;\n}}\n"

    with patch("cpp_re_agent.ai_improver.improve_function", side_effect=fake_improve):
        result = pipeline.batch_improve(
            functions, tmp_path / "ws", None, max_workers=4)

    assert result.failed == ["FUN_00401000"]
    assert len(result.improved) == 3
    assert not (tmp_path / "ws" / "improved" / "FUN_00401000.cpp").exists()

    # --limit caps LLM calls even with a wide level.
    ws2 = tmp_path / "ws2"
    calls = []

    def counting_improve(code, **kwargs):
        calls.append(code)
        name = next(n for n, c in functions.items() if c == code)
        return f"int {name}_improved(int handle)\n{{\n    return handle;\n}}\n"

    with patch("cpp_re_agent.ai_improver.improve_function", side_effect=counting_improve):
        limited = pipeline.batch_improve(
            functions, ws2, None, limit=2, max_workers=4)

    assert len(calls) == 2, "limit must cap scheduled calls, not just recorded ones"
    assert len(limited.improved) == 2


# --- Decompiled name matching ------------------------------------------------
#
# ghidrecomp writes one file per function as "<symbol>-<address>.c", so
# get_functions keys carry an address suffix that call sites never do. Comparing
# them directly matched nothing, so build_callgraph returned an edgeless graph
# and the whole leaves-first ordering (plus callee-signature propagation) was
# silently inert on real decompiler output.

def test_normalize_name_strips_decompiler_decoration():
    from cpp_re_agent import scanner

    assert scanner.normalize_name("visit-00104000") == "visit"
    assert scanner.normalize_name("taskflow::DependencyGraph::visit") == "visit"
    assert scanner.normalize_name("add_task-00103770") == "add_task"
    assert scanner.normalize_name("main") == "main"
    # A hyphen that isn't an address suffix must survive.
    assert scanner.strip_address_suffix("some-name") == "some-name"
    assert scanner.strip_address_suffix("f-00ab12") == "f"


def test_name_index_resolves_unambiguous_names():
    from cpp_re_agent import scanner

    index = scanner.NameIndex(["visit-00104000", "run-00105000"])
    assert index.resolve("visit") == "visit-00104000"
    assert index.resolve("visit-00104000") == "visit-00104000"
    assert index.resolve("printf") is None


def test_build_callgraph_matches_ghidrecomp_address_suffixed_names():
    """The regression: address-suffixed keys must not produce an edgeless graph."""
    from cpp_re_agent import callgraph

    functions = {
        "helper-00104000": "int helper(int param_1)\n{\n    return param_1 + 1;\n}\n",
        "caller-00105000": "int caller(int param_1)\n{\n    int iVar1 = helper(param_1);\n"
                           "    return iVar1 * 2;\n}\n",
    }
    graph = callgraph.build_callgraph(functions)
    assert graph["caller-00105000"] == {"helper-00104000"}, graph
    assert graph["helper-00104000"] == set()

    levels = callgraph.topological_levels(graph)
    assert len(levels) == 2, "callee and caller must land in different levels"
    assert levels[0] == ["helper-00104000"]


def test_dependencies_excludes_self_recursion_across_name_forms():
    """`visit` calling `visit-00104000` is recursion, not a dependency."""
    from cpp_re_agent import ai_improver

    code = ("int visit(int param_1)\n{\n"
            "    if (param_1 < 1) { return 0; }\n"
            "    return visit(param_1 + -1) + 1;\n}\n")
    deps = ai_improver._dependencies(code, {"visit-00104000", "other-00105000"})
    assert deps == [], f"self-recursion must not be a dependency: {deps}"


# --- Overloads and same-named methods ---------------------------------------
#
# A decompiled binary routinely holds many functions sharing a bare name:
# Scheduler::reset vs MetricsCollector::reset (different classes), and
# describe(int) vs describe(const Summary&) (overloads). ghidrecomp's file stem
# keeps only the bare name, so matching on it alone conflates them.

# Ghidra's own output shape: a demangled-signature comment, then the definition.
GHIDRA_DEPGRAPH_SIZE = """
/* taskflow::DependencyGraph::size() const */

void __thiscall taskflow::DependencyGraph::size(DependencyGraph *this)

{
  return;
}
"""

GHIDRA_VECTOR_SIZE = """
/* std::vector<int, std::allocator<int> >::size() const */

long __thiscall std::vector<int,std::allocator<int>>::size(vector<int,std::allocator<int>> *this)

{
  return 0;
}
"""

GHIDRA_METRICS_RESET = """
/* taskflow::MetricsCollector::reset() */

void __thiscall taskflow::MetricsCollector::reset(MetricsCollector *this)

{
  return;
}
"""

GHIDRA_DESCRIBE_SUMMARY = """
/* taskflow::describe(taskflow::Summary const&) */

void taskflow::describe(Summary *param_1)

{
  return;
}
"""

GHIDRA_DESCRIBE_INT = """
/* taskflow::describe(int) */

void taskflow::describe(int param_1)

{
  return;
}
"""


def test_function_key_recovers_class_from_demangled_comment():
    from cpp_re_agent import scanner

    key = scanner.function_key("size-001024ea", GHIDRA_DEPGRAPH_SIZE)
    assert key.qualifier == "taskflow::DependencyGraph"
    assert key.base == "size"
    assert key.owner == "DependencyGraph"
    assert key.arity == 0

    stl = scanner.function_key("size-00103bde", GHIDRA_VECTOR_SIZE)
    assert stl.base == "size"
    assert stl.is_stdlib
    assert stl.qualified != key.qualified, "the two must not share an identity"


def test_function_key_distinguishes_overloads_by_param_type():
    from cpp_re_agent import scanner

    by_summary = scanner.function_key("describe-00109000", GHIDRA_DESCRIBE_SUMMARY)
    by_int = scanner.function_key("describe-0010a000", GHIDRA_DESCRIBE_INT)

    assert by_summary.base == by_int.base == "describe"
    assert by_summary.arity == by_int.arity == 1
    assert by_summary.params == ("Summary",)
    assert by_int.params == ("int",)
    assert by_summary.match_key() != by_int.match_key()


def test_name_index_refuses_to_guess_an_ambiguous_bare_name():
    """The regression: bare `size` must not silently resolve to std::vector::size."""
    from cpp_re_agent import scanner

    funcs = {
        "size-001024ea": GHIDRA_DEPGRAPH_SIZE,
        "size-00103bde": GHIDRA_VECTOR_SIZE,
    }
    index = scanner.NameIndex(funcs)

    assert index.resolve("size") is None, "ambiguous bare name must not resolve"
    assert index.resolve("taskflow::DependencyGraph::size") == "size-001024ea"
    assert index.resolve("std::vector<int,std::allocator<int>>::size") == "size-00103bde"
    assert set(index.ambiguous_names()["size"]) == set(funcs)


def test_name_index_handles_template_whitespace_mismatch():
    """
    Ghidra's comment writes `vector<int, std::allocator<int> >` while call sites
    write `vector<int,std::allocator<int>>` — the same type, different strings.
    """
    from cpp_re_agent import scanner

    index = scanner.NameIndex({"size-00103bde": GHIDRA_VECTOR_SIZE})
    assert index.resolve("std::vector<int,std::allocator<int>>::size") == "size-00103bde"
    assert index.resolve("std::vector<int, std::allocator<int> >::size") == "size-00103bde"


def test_callgraph_does_not_invent_edges_for_ambiguous_calls():
    """A wrong edge reorders the batch and injects the wrong callee signature."""
    from cpp_re_agent import callgraph

    caller = """
/* taskflow::Scheduler::run() */

void __thiscall taskflow::Scheduler::run(Scheduler *this)

{
  taskflow::DependencyGraph::size(this);
  size(this);
  return;
}
"""
    functions = {
        "size-001024ea": GHIDRA_DEPGRAPH_SIZE,
        "size-00103bde": GHIDRA_VECTOR_SIZE,
        "run-0010bd64": caller,
    }
    graph = callgraph.build_callgraph(functions)
    # The qualified call resolves; the bare one is ambiguous and is dropped.
    assert graph["run-0010bd64"] == {"size-001024ea"}, graph


def test_match_key_is_namespace_insensitive_across_the_eval_boundary():
    """
    The decompiled side says `taskflow::MetricsCollector::reset`; the original
    source, written inside `namespace taskflow {}`, says
    `MetricsCollector::reset`. They must pair.
    """
    from cpp_re_agent import scanner

    decompiled = scanner.function_key("reset-001097ca", GHIDRA_METRICS_RESET)
    original = scanner.function_key("reset", "void MetricsCollector::reset() { records_.clear(); }")
    assert decompiled.match_key() == original.match_key()

    other_class = scanner.function_key("reset", "void Scheduler::reset() { tick_ = 0; }")
    assert other_class.match_key() != decompiled.match_key(), \
        "same method name on a different class must not pair"


def test_get_functions_warns_about_unreadable_decompiled_files(workspace_dir, capsys):
    """
    ghidrecomp names a std::string-returning function `f[abi:cxx11]-1234.c`;
    on NTFS the ':' makes that an alternate data stream, leaving a zero-byte
    `f[abi` that rglob("*.c") skips. Silently losing whole functions is exactly
    the failure mode this file keeps having, so it must be reported.
    """
    decomps = workspace_dir / "decomps"
    decomps.mkdir()
    (decomps / "good-00401000.c").write_text("int good(void) { return 0; }")
    (decomps / "describe[abi").write_text("")          # truncated + empty

    # Ghidra's project database sits beside the decompilation and is full of
    # empty/extension-less files; none of them are lost functions.
    projects = workspace_dir / "ghidra_projects" / "app.rep"
    projects.mkdir(parents=True)
    (projects / "app.gpr").write_text("")
    (projects / "~index.dat").write_text("")

    funcs = decompiler.get_functions(str(workspace_dir))

    assert set(funcs) == {"good-00401000"}
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "describe[abi" in out
    assert "alternate data stream" in out
    assert "1 decompiled file(s)" in out, "Ghidra project files must not be counted"
    assert "app.gpr" not in out


# --- Progress reporting -----------------------------------------------------
#
# Stage 1 sits inside a few parallel LLM calls for minutes at a time. An
# event-only log went silent for long stretches and never said how far along it
# was, so the bar has to name what is in flight and estimate what is left.

class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_format_duration_reads_naturally():
    from cpp_re_agent.progress import format_duration

    # Sub-10s keeps a decimal: a fast call rounding to "0s" reads as nothing.
    assert format_duration(0.4) == "0.4s"
    assert format_duration(7.4) == "7.4s"
    assert format_duration(42) == "42s"
    assert format_duration(92) == "1m32s"
    assert format_duration(3600) == "1h00m"
    assert format_duration(4210) == "1h10m"
    assert format_duration(-5) == "0.0s"


def test_progress_bar_reports_position_and_running_work():
    from cpp_re_agent.progress import Progress

    bar = Progress(4, stream=_FakeTTY(), width=10)
    bar.start("alpha")
    bar.start("beta")
    rendered = bar.render()

    # What is in flight is the thing that was missing during a long level.
    assert "alpha" in rendered and "beta" in rendered
    assert "0/4" in rendered

    bar.finish("alpha", 12.0)
    rendered = bar.render()
    assert "1/4" in rendered
    assert " 25%" in rendered
    assert "alpha" not in rendered, "a finished unit must leave the running list"
    assert "beta" in rendered


def test_progress_eta_uses_throughput_not_mean_duration():
    """
    With N workers, mean-duration x remaining overestimates by ~N. Elapsed per
    completed unit already has the real concurrency baked in.
    """
    from cpp_re_agent.progress import Progress

    bar = Progress(10, stream=_FakeTTY())
    bar._started_at -= 20.0          # 20s of wall clock has passed
    for i in range(5):
        bar.finish(f"f{i}", 15.0)    # each took 15s, but they overlapped

    eta = bar._eta_seconds()
    # 20s for 5 done -> 4s each -> ~20s for the remaining 5.
    assert 15 < eta < 25, eta
    assert eta < 5 * 15, "must not assume the calls were sequential"


def test_progress_falls_back_to_plain_lines_without_a_tty():
    """CI logs and pipes must not get carriage returns or escape codes."""
    from cpp_re_agent.progress import Progress

    out = io.StringIO()          # StringIO has no isatty() -> not live
    bar = Progress(2, stream=out)
    bar.start("alpha")
    bar.finish("alpha", 3.0)
    bar.note("level 1/2")
    text = out.getvalue()

    assert "\r" not in text and "\033" not in text
    assert "alpha" in text and "level 1/2" in text


def test_progress_disabled_is_inert():
    from cpp_re_agent.progress import Progress

    out = io.StringIO()
    bar = Progress(5, stream=out, enabled=False)
    bar.start("a")
    bar.finish("a", 1.0)
    bar.note("hello")
    assert out.getvalue() == ""


def test_batch_improve_reports_each_function_as_it_starts(tmp_path):
    """
    The original complaint: a level announced itself, then printed nothing
    until a function finished. Workers must report at start, not just at end.
    """
    from cpp_re_agent import pipeline
    from cpp_re_agent.progress import Progress

    functions = {
        f"FUN_0040{i}000": (
            f"undefined4 FUN_0040{i}000(int param_1)\n{{\n"
            + "\n".join(f"    int iVar{j} = param_1 + {j};" for j in range(12))
            + "\n    return iVar1;\n}\n"
        )
        for i in range(3)
    }

    out = io.StringIO()
    bar = Progress(0, stream=out)

    def fake_improve(code, **kwargs):
        name = next(n for n, c in functions.items() if c == code)
        # Every function must already be announced by the time it runs.
        assert name in out.getvalue(), f"{name} not reported at start"
        return f"int {name}_improved(int handle)\n{{\n    return handle;\n}}\n"

    with patch("cpp_re_agent.ai_improver.improve_function", side_effect=fake_improve):
        result = pipeline.batch_improve(
            functions, tmp_path / "ws", None, max_workers=2, progress=bar)

    assert len(result.improved) == 3
    text = out.getvalue()
    for name in functions:
        assert text.count(name) >= 2, f"{name} should be reported at start and finish"


def test_batch_improve_totals_count_only_real_work(tmp_path):
    """
    'how much is left' must be measured against functions we will actually
    improve, not every decompiled symbol — most of which are library noise.
    """
    from cpp_re_agent import pipeline
    from cpp_re_agent.progress import Progress

    real = ("undefined4 FUN_00401000(int param_1)\n{\n"
            + "\n".join(f"    int iVar{j} = param_1 + {j};" for j in range(12))
            + "\n    return iVar1;\n}\n")
    functions = {
        "FUN_00401000": real,
        "__cxa_finalize-001021c0": "void __cxa_finalize(void)\n{\n  return;\n}\n",
        "thunk_x": "void thunk_x(void)\n{\n  x();\n  return;\n}\n",
    }

    lines = []
    bar = Progress(0, stream=io.StringIO())
    with patch("cpp_re_agent.ai_improver.improve_function",
               return_value="int f(int h)\n{\n    return h;\n}\n"):
        pipeline.batch_improve(functions, tmp_path / "ws", None,
                               status=lines.append, progress=bar)

    assert bar.total == 1, "only the real function counts toward the total"
    assert any("1 to improve" in line for line in lines), lines


def test_progress_always_names_at_least_one_running_unit(monkeypatch):
    """
    When a level stalls, "+4 more" alone is useless — the whole reason to look
    is to find out *which* call is slow, and for how long.
    """
    from cpp_re_agent import progress as progress_mod
    from cpp_re_agent.progress import Progress

    monkeypatch.setattr(progress_mod.Progress, "_term_width", lambda self: 100)

    bar = Progress(76, stream=_FakeTTY(), label="stage 1")
    bar.done, bar.failed = 18, 2
    bar._started_at -= 745
    now = __import__("time").monotonic()
    for name, age in [("consolidate_catalog-0040a110", 142),
                      ("emit_report-0040b220", 11),
                      ("scan_line-0040c330", 6)]:
        bar._running[name] = now - age

    rendered = bar.render()
    assert "running:" in rendered
    # The name may be clipped to fit, but it must identify the slow call...
    assert "consol" in rendered, "the longest-running unit must be named"
    assert "emit_report" not in rendered, "shorter/newer units yield space first"
    # ...and its elapsed time must survive, since "how slow" is the question.
    assert "2m22s" in rendered
    assert len(rendered) <= 100, f"must fit the terminal: {len(rendered)}"


def test_progress_running_list_drops_out_on_a_narrow_terminal(monkeypatch):
    from cpp_re_agent import progress as progress_mod
    from cpp_re_agent.progress import Progress

    monkeypatch.setattr(progress_mod.Progress, "_term_width", lambda self: 60)

    bar = Progress(76, stream=_FakeTTY(), label="stage 1")
    bar.done = 18
    bar._running["some_long_function_name-00401820"] = __import__("time").monotonic()

    rendered = bar.render()
    assert "18/76" in rendered, "the bar itself is what matters when space is tight"
    assert len(rendered) <= 60


# --- Stripped binaries ------------------------------------------------------
#
# A shipped binary has no symbol table: Ghidra names everything FUN_<addr>, so
# the name-based library filter goes blind and would send the whole statically
# linked libstdc++ to the LLM. Measured on the tier4 corpus binary, the gate
# went from keeping 67 functions to keeping 518. Call depth from the entry
# point is what still separates the program from the library it links.

def _stripped_program():
    """A FUN_-named call graph: main -> app code -> a deep library chain."""
    def body(name, calls=()):
        lines = [f"    int iVar{j} = param_1 + {j};" for j in range(12)]
        lines += [f"    {c}(param_1);" for c in calls]
        return (f"undefined4 {name}(int param_1)\n{{\n"
                + "\n".join(lines) + "\n    return iVar1;\n}\n")

    return {
        "FUN_00100000": body("FUN_00100000", ["FUN_00100100", "FUN_00100200"]),
        "FUN_00100100": body("FUN_00100100", ["FUN_00100300"]),   # depth 1
        "FUN_00100200": body("FUN_00100200"),                      # depth 1
        "FUN_00100300": body("FUN_00100300", ["FUN_00100400"]),   # depth 2
        "FUN_00100400": body("FUN_00100400", ["FUN_00100500"]),   # depth 3 (library)
        "FUN_00100500": body("FUN_00100500"),                      # depth 4 (library)
    }


def test_looks_stripped_detects_missing_symbols():
    from cpp_re_agent import pipeline

    assert pipeline.looks_stripped(_stripped_program()) is True
    assert pipeline.looks_stripped({
        "main-00401000": "int main(void){return 0;}",
        "process_order-00401100": "int process_order(int a){return a;}",
    }) is False
    assert pipeline.looks_stripped({}) is False


def test_primary_entry_finds_main_without_any_symbol():
    """
    Nothing calls the entry point and it reaches most of the program — true
    whether or not a `main` symbol survived stripping.
    """
    from cpp_re_agent import callgraph

    graph = callgraph.build_callgraph(_stripped_program())
    assert callgraph.primary_entry(graph) == "FUN_00100000"


def test_call_depths_measure_hops_from_the_entry():
    from cpp_re_agent import callgraph

    graph = callgraph.build_callgraph(_stripped_program())
    depths = callgraph.call_depths(graph, "FUN_00100000")

    assert depths["FUN_00100000"] == 0
    assert depths["FUN_00100100"] == 1 and depths["FUN_00100200"] == 1
    assert depths["FUN_00100300"] == 2
    assert depths["FUN_00100500"] == 4


def test_stripped_binary_improves_only_near_the_entry(tmp_path):
    """
    The regression this exists to prevent: without the depth filter every
    library function reached from main is sent to the LLM.
    """
    from cpp_re_agent import pipeline

    functions = _stripped_program()
    scheduled = []

    def fake(code, **kwargs):
        scheduled.append(next(n for n, c in functions.items() if c == code))
        return "int recovered(int handle)\n{\n    return handle;\n}\n"

    with patch("cpp_re_agent.ai_improver.improve_function", side_effect=fake):
        pipeline.batch_improve(functions, tmp_path / "auto", None, max_workers=1)

    # Auto-detected as stripped -> DEFAULT_MAX_DEPTH (2) hops from the entry.
    assert "FUN_00100400" not in scheduled, "depth-3 library code must be skipped"
    assert "FUN_00100500" not in scheduled, "depth-4 library code must be skipped"
    assert {"FUN_00100000", "FUN_00100100", "FUN_00100200"} <= set(scheduled)

    # An explicit depth reaches further.
    deep = []
    with patch("cpp_re_agent.ai_improver.improve_function",
               side_effect=lambda code, **kw: (
                   deep.append(next(n for n, c in functions.items() if c == code))
                   or "int r(int h)\n{\n    return h;\n}\n")):
        pipeline.batch_improve(functions, tmp_path / "deep", None,
                               max_workers=1, max_depth=4)
    assert "FUN_00100500" in deep


def test_symbols_present_means_no_depth_filter(tmp_path):
    """With real names the name filter is accurate; depth would only lose work."""
    from cpp_re_agent import pipeline

    def body(name):
        return (f"undefined4 {name}(int param_1)\n{{\n"
                + "\n".join(f"    int iVar{j} = param_1 + {j};" for j in range(12))
                + "\n    return iVar1;\n}\n")

    # Named (not FUN_) but artifact-heavy, so should_improve keeps them.
    functions = {
        "main-00100000": body("main").replace("    return iVar1;",
                                              "    stage_one(param_1);\n    return iVar1;"),
        "stage_one-00100100": body("stage_one").replace(
            "    return iVar1;", "    stage_two(param_1);\n    return iVar1;"),
        "stage_two-00100200": body("stage_two").replace(
            "    return iVar1;", "    stage_three(param_1);\n    return iVar1;"),
        "stage_three-00100300": body("stage_three"),
    }
    assert pipeline.looks_stripped(functions) is False

    scheduled = []
    with patch("cpp_re_agent.ai_improver.improve_function",
               side_effect=lambda code, **kw: (
                   scheduled.append(next(n for n, c in functions.items() if c == code))
                   or "int r(int h)\n{\n    return h;\n}\n")):
        pipeline.batch_improve(functions, tmp_path / "ws", None, max_workers=1)

    assert "stage_three-00100300" in scheduled, \
        "a deep but real-named function must not be filtered out"


def test_disconnected_functions_are_all_treated_as_entries(tmp_path):
    """
    Unrelated functions with no calls between them are all roots, so all of
    them are entry points and none is "deep library code". Measuring depth
    from a single best root instead would keep one and discard the rest.
    """
    from cpp_re_agent import pipeline

    # Four FUN_-named functions with no calls between them: looks stripped,
    # but no entry point reaches more than itself.
    functions = {
        f"FUN_0040{i}000": (
            f"undefined4 FUN_0040{i}000(int param_1)\n{{\n"
            + "\n".join(f"    int iVar{j} = param_1 + {j};" for j in range(12))
            + "\n    return iVar1;\n}\n"
        )
        for i in range(4)
    }
    assert pipeline.looks_stripped(functions) is True

    lines, scheduled = [], []
    with patch("cpp_re_agent.ai_improver.improve_function",
               side_effect=lambda code, **kw: (
                   scheduled.append(next(n for n, c in functions.items() if c == code))
                   or "int r(int h)\n{\n    return h;\n}\n")):
        pipeline.batch_improve(functions, tmp_path / "ws", None,
                               max_workers=1, status=lines.append)

    assert len(scheduled) == 4, "a disconnected graph must not gut the work list"


def test_entry_points_are_all_roots_for_a_library():
    """
    A shared object has no `main`. Its entry points are the exported functions,
    and no single root reaches the rest — picking only the biggest would treat
    most of the library's own API as unreachable library noise.
    """
    from cpp_re_agent import callgraph

    # Three independent exported functions, each over its own helper.
    graph = {
        "api_a": {"helper_a"}, "helper_a": set(),
        "api_b": {"helper_b"}, "helper_b": set(),
        "api_c": {"helper_c"}, "helper_c": set(),
    }
    entries = callgraph.entry_points(graph)
    assert entries == ["api_a", "api_b", "api_c"]

    depths = callgraph.call_depths(graph, entries)
    assert depths["api_a"] == depths["api_b"] == depths["api_c"] == 0
    assert depths["helper_c"] == 1

    # An executable-shaped graph still resolves to its single entry.
    exe = {"main": {"a"}, "a": {"b"}, "b": set(), "unused": set()}
    assert callgraph.entry_points(exe) == ["main"]


def test_levels_over_subset_restores_parallelism():
    """
    Levels over the whole graph serialize survivors that skipped functions sit
    between. Measured on a stripped shared object: 112 functions worth
    improving landed in 112 whole-graph levels, so every LLM call ran alone.
    """
    from cpp_re_agent import callgraph

    # a -> lib1 -> b -> lib2 -> c : a, b, c are independent of one another
    # once the library functions between them are filtered out.
    graph = {
        "a": {"lib1"}, "lib1": {"b"},
        "b": {"lib2"}, "lib2": {"c"},
        "c": set(),
    }
    whole = callgraph.topological_levels(graph)
    assert max(len(lv) for lv in whole) == 1, "whole-graph levels are a chain"

    # But a, b, c DO reach each other transitively, so ordering is preserved.
    subset = callgraph.levels_over_subset(graph, {"a", "b", "c"})
    flat = [n for lv in subset for n in lv]
    assert flat.index("c") < flat.index("b") < flat.index("a")

    # Genuinely independent survivors share a level.
    graph2 = {"x": {"libx"}, "libx": set(), "y": {"liby"}, "liby": set()}
    levels2 = callgraph.levels_over_subset(graph2, {"x", "y"})
    assert levels2 == [["x", "y"]], levels2


# --- Stage 2 parallelism ----------------------------------------------------
#
# Unlike stage 1, the refine pass has no ordering constraint: project.h and the
# stage-1 signature map are both fixed before it starts, and no refinement
# feeds another. So it runs at full width rather than in callgraph levels.

def _workspace_with_improved(tmp_path, count=4):
    ws = tmp_path / "ws"
    improved = ws / "improved"
    improved.mkdir(parents=True)
    for i in range(count):
        (improved / f"fn_{i}.cpp").write_text(
            f"int fn_{i}(int handle)\n{{\n    return handle + {i};\n}}\n",
            encoding="utf-8")
    (ws / "project.h").write_text("struct Order { int id; };\n", encoding="utf-8")
    return ws


def test_run_contextual_refines_concurrently(tmp_path):
    """Deadlocks (and fails) unless four refinements are in flight at once."""
    import threading
    from cpp_re_agent import pipeline

    ws = _workspace_with_improved(tmp_path, count=4)
    barrier = threading.Barrier(4, timeout=10)

    def fake(code, header, callees, **kwargs):
        barrier.wait()
        return "int refined(int handle)\n{\n    return handle;\n}\n"

    with patch("cpp_re_agent.contextual_improver.refine_function", side_effect=fake):
        refined = pipeline.run_contextual(ws, status=lambda m: None, max_workers=4)

    assert sorted(refined) == ["fn_0", "fn_1", "fn_2", "fn_3"]
    for i in range(4):
        assert "refined" in (ws / "improved" / f"fn_{i}.cpp").read_text()


def test_run_contextual_max_workers_1_stays_sequential(tmp_path):
    import threading
    import time as _time
    from cpp_re_agent import pipeline

    ws = _workspace_with_improved(tmp_path, count=3)
    concurrent, peak = 0, 0
    lock = threading.Lock()

    def fake(code, header, callees, **kwargs):
        nonlocal concurrent, peak
        with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        _time.sleep(0.01)
        with lock:
            concurrent -= 1
        return "int refined(int handle)\n{\n    return handle;\n}\n"

    with patch("cpp_re_agent.contextual_improver.refine_function", side_effect=fake):
        pipeline.run_contextual(ws, status=lambda m: None, max_workers=1)

    assert peak == 1, f"max_workers=1 must not overlap calls (peak={peak})"


def test_run_contextual_isolates_failures_and_keeps_prior_output(tmp_path):
    """A failed or unparseable refinement must leave the stage-1 version alone."""
    from cpp_re_agent import pipeline

    ws = _workspace_with_improved(tmp_path, count=4)
    original = (ws / "improved" / "fn_1.cpp").read_text()

    def fake(code, header, callees, **kwargs):
        if "fn_1" in code:
            raise RuntimeError("429 rate limit")
        if "fn_2" in code:
            return "this is not valid C++ {{{"
        return "int refined(int handle)\n{\n    return handle;\n}\n"

    with patch("cpp_re_agent.contextual_improver.refine_function", side_effect=fake):
        refined = pipeline.run_contextual(ws, status=lambda m: None, max_workers=4)

    assert sorted(refined) == ["fn_0", "fn_3"]
    assert (ws / "improved" / "fn_1.cpp").read_text() == original
    assert "not valid C++" not in (ws / "improved" / "fn_2.cpp").read_text()


def test_run_contextual_result_order_is_deterministic(tmp_path):
    """Completion order varies with concurrency; the reported list must not."""
    from cpp_re_agent import pipeline

    with patch("cpp_re_agent.contextual_improver.refine_function",
               return_value="int refined(int h)\n{\n    return h;\n}\n"):
        first = pipeline.run_contextual(_workspace_with_improved(tmp_path / "a", 6),
                                        status=lambda m: None, max_workers=6)
        second = pipeline.run_contextual(_workspace_with_improved(tmp_path / "b", 6),
                                         status=lambda m: None, max_workers=6)
    assert first == second == sorted(first)


# --- Shared streaming helpers ----------------------------------------------
#
# Every LLM call streams. A blocking `invoke` puts no bytes on the wire for the
# whole generation, which trips idle-read timeouts on proxies in front of remote
# endpoints — and a timed-out call that gets retried is genuinely slower.

def test_stream_text_concatenates_chunks_and_reports_progress():
    from cpp_re_agent.llm_factory import stream_text

    def chunk(text):
        c = MagicMock()
        c.content = text
        return c

    llm = MagicMock()
    llm.stream.return_value = iter([chunk("int "), chunk("f()"), chunk(" {}")])

    seen = []
    assert stream_text(llm, "prompt", on_chunk=seen.append) == "int f() {}"
    # The callback sees the running text, so a caller can show partial output.
    assert seen == ["int ", "int f()", "int f() {}"]
    llm.invoke.assert_not_called()


def test_stream_text_tolerates_chunks_without_content():
    """Some clients emit metadata-only chunks; they must not break the join."""
    from cpp_re_agent.llm_factory import stream_text

    good, empty, bare = MagicMock(), MagicMock(), object()
    good.content, empty.content = "code", None
    llm = MagicMock()
    llm.stream.return_value = iter([empty, good, bare])
    assert stream_text(llm, "prompt") == "code"


def test_stream_text_falls_back_to_invoke_without_streaming():
    from cpp_re_agent.llm_factory import stream_text

    llm = MagicMock()
    llm.stream.side_effect = NotImplementedError("no streaming")
    llm.invoke.return_value = MagicMock(content="blocking result")

    seen = []
    assert stream_text(llm, "prompt", on_chunk=seen.append) == "blocking result"
    assert seen == ["blocking result"]


def test_require_llm_names_the_missing_credential():
    """
    The regression: get_llm returning None produced
    `AttributeError: 'NoneType' has no attribute 'stream'` several frames from
    the real problem, which is an unset API key.
    """
    from cpp_re_agent.llm_factory import ImprovementError, require_llm

    with patch("cpp_re_agent.llm_factory.get_llm", return_value=None):
        with pytest.raises(ImprovementError, match="GEMINI_API_KEY"):
            require_llm("gemini", "gemini-2.5-flash")
        with pytest.raises(ImprovementError, match="LOCAL_LLM_URL"):
            require_llm("local", "openai/gpt-oss-20b")


def test_consolidate_definitions_checks_the_llm_and_streams():
    """Header synthesis is the longest single call, so it must stream too."""
    from cpp_re_agent import knowledge_graph
    from cpp_re_agent.llm_factory import ImprovementError

    with patch("cpp_re_agent.llm_factory.get_llm", return_value=None):
        with pytest.raises(ImprovementError, match="could not be initialized"):
            knowledge_graph.consolidate_definitions("struct A { int x; };")

    def chunk(text):
        c = MagicMock()
        c.content = text
        return c

    llm = MagicMock()
    llm.stream.return_value = iter([chunk("```cpp\nstruct A"), chunk(" { int x; };\n```")])
    with patch("cpp_re_agent.llm_factory.get_llm", return_value=llm):
        header = knowledge_graph.consolidate_definitions("struct A { int x; };")

    # Chunks are joined and the markdown fences removed. The surrounding
    # newlines survive because this stripper strips *then* replaces — see
    # plan item T6, which unifies these on ai_improver.extract_code's regex.
    assert header.strip() == "struct A { int x; };"
    assert "```" not in header
    llm.invoke.assert_not_called()
