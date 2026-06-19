import pytest
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
from cpp_re_agent import decompiler
from cpp_re_agent import ai_improver
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

def test_decompiler_handles_missing_output(workspace_dir):
    """Test that get_functions returns mock data or empty dict if dir missing."""
    # When directory doesn't exist
    funcs = decompiler.get_functions(str(workspace_dir / "non_existent"))
    # Should fall back to mock data as per implementation
    assert "main" in funcs 
    assert "User::introduce" in funcs

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
    with patch("cpp_re_agent.ai_improver.get_llm") as mock_get_llm:
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
