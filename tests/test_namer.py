"""
Tests for the final naming pass.

On a stripped binary the per-function improve step usually declines to rename
`FUN_00401820` — with one body in view there is nothing to name it after.
Measured on the tier4 corpus binary, 55 of 72 functions were still synthetic at
the end of stage 2, and 414 call sites still read `FUN_...`. This pass runs last,
when project.h and a call graph over recovered code exist.
"""
from unittest.mock import MagicMock, patch

import pytest

from cpp_re_agent import namer, pipeline


def test_synthetic_names_are_recognised():
    assert namer.is_synthetic("FUN_00401820") is True
    assert namer.is_synthetic("FUN_00401820-00401820") is True
    assert namer.is_synthetic("LAB_004018a0") is True
    assert namer.is_synthetic("find_order") is False
    assert namer.is_synthetic("FUNCTION_KEY") is False, "not a placeholder"


def test_proposal_parsing_tolerates_a_chatty_model():
    """We ask for a bare identifier; models rarely comply exactly."""
    assert namer.parse_proposal("find_task_by_id") == "find_task_by_id"
    assert namer.parse_proposal("`reset_metrics`") == "reset_metrics"
    assert namer.parse_proposal("reset_metrics()") == "reset_metrics"
    assert namer.parse_proposal("void reset_metrics(int)") == "reset_metrics"
    assert namer.parse_proposal("reset_metrics\nBecause it clears...") == "reset_metrics"

    # Declining, and outright junk, must both yield nothing.
    assert namer.parse_proposal("UNKNOWN") is None
    assert namer.parse_proposal("") is None
    assert namer.parse_proposal("I'm not sure what this does!") is None


def test_proposals_never_collide_or_produce_illegal_names():
    taken = {"find_order", "int", "class"}

    assert namer.unique_name("compute_total", taken) == "compute_total"
    # An in-use name is numbered rather than silently duplicated: two functions
    # sharing an identifier would not compile.
    assert namer.unique_name("find_order", taken) == "find_order_2"
    # Keywords and placeholders are refused outright.
    assert namer.unique_name("class", taken) is None
    assert namer.unique_name("FUN_00401820", taken) is None
    assert namer.unique_name("9lives", taken) is None


def test_renames_are_applied_to_call_sites_not_just_definitions(tmp_path):
    """
    The whole point: a name applied only to the definition leaves the body of
    every caller reading `FUN_0010a4b2(container)`.
    """
    improved = tmp_path / "improved"
    improved.mkdir()
    (improved / "FUN_0010ca7a.cpp").write_text(
        "void* FUN_0010ca7a(void* c, int t) {\n"
        "    size_t n = FUN_0010a4b2(c);\n"
        "    return FUN_0010a8e2(c, n);\n}\n", encoding="utf-8")
    (improved / "FUN_0010a4b2.cpp").write_text(
        "size_t FUN_0010a4b2(void* c) { return 0; }\n", encoding="utf-8")

    changed = namer.apply_renames(improved, {
        "FUN_0010ca7a": "search_container",
        "FUN_0010a4b2": "container_size",
    })

    assert changed == 2
    caller = (improved / "FUN_0010ca7a.cpp").read_text()
    assert "void* search_container(" in caller
    assert "container_size(c)" in caller, "call site must be rewritten too"
    assert "FUN_0010a4b2" not in caller
    # Untouched names are left alone.
    assert "FUN_0010a8e2" in caller


def test_renames_are_whole_word_only(tmp_path):
    improved = tmp_path / "improved"
    improved.mkdir()
    (improved / "a.cpp").write_text(
        "int run(int x) { return run_helper(x) + rerun(x); }\n", encoding="utf-8")

    namer.apply_renames(improved, {"run": "execute"})
    text = (improved / "a.cpp").read_text()

    assert "int execute(int x)" in text
    assert "run_helper(x)" in text, "must not corrupt a longer identifier"
    assert "rerun(x)" in text


# --- the pipeline stage -----------------------------------------------------

def _workspace(tmp_path):
    ws = tmp_path / "ws"
    improved = ws / "improved"
    improved.mkdir(parents=True)
    (improved / "FUN_00001000.cpp").write_text(
        "int FUN_00001000(int h) {\n    return FUN_00002000(h) + 1;\n}\n",
        encoding="utf-8")
    (improved / "FUN_00002000.cpp").write_text(
        "int FUN_00002000(int h) {\n    return h * 2;\n}\n", encoding="utf-8")
    (improved / "already_named.cpp").write_text(
        "int already_named(int h) {\n    return h;\n}\n", encoding="utf-8")
    (ws / "project.h").write_text("struct Task { int id; };\n", encoding="utf-8")
    return ws


def test_run_naming_names_only_the_unnamed(tmp_path):
    ws = _workspace(tmp_path)
    proposals = iter(["double_value", "add_one"])

    with patch("cpp_re_agent.namer.propose_name",
               side_effect=lambda *a, **k: next(proposals)):
        renames = pipeline.run_naming(ws, status=lambda m: None, max_workers=1)

    assert set(renames) == {"FUN_00001000", "FUN_00002000"}
    assert "already_named" not in renames.values()

    # The file is renamed too, so read it under its recovered name.
    caller = (ws / "improved" / "add_one.cpp").read_text()
    assert "FUN_" not in caller, caller
    assert "double_value(h)" in caller, "the call site was rewritten"
    # Recorded so a name can be traced back to its address.
    assert namer.load_names(ws) == renames


def test_run_naming_is_leaves_first(tmp_path):
    """
    A caller is far easier to name once its callees have real names, so the
    callee must be proposed first.
    """
    ws = _workspace(tmp_path)
    seen_context = {}

    def fake(code, header="", callees=(), callers=(), **kw):
        name = "callee_fn" if "h * 2" in code else "caller_fn"
        seen_context[name] = list(callees)
        return name

    with patch("cpp_re_agent.namer.propose_name", side_effect=fake):
        pipeline.run_naming(ws, status=lambda m: None, max_workers=1)

    assert seen_context["caller_fn"] == ["callee_fn"], \
        "the caller should see its callee's recovered name, not FUN_00002000"


def test_run_naming_survives_a_model_that_declines(tmp_path):
    ws = _workspace(tmp_path)

    with patch("cpp_re_agent.namer.propose_name", return_value=None):
        renames = pipeline.run_naming(ws, status=lambda m: None, max_workers=1)

    assert renames == {}
    assert "FUN_00001000" in (ws / "improved" / "FUN_00001000.cpp").read_text()


def test_run_naming_isolates_a_failed_call(tmp_path):
    ws = _workspace(tmp_path)
    calls = []

    def fake(code, header="", **kw):
        calls.append(code)
        if "h * 2" in code:
            raise RuntimeError("429 rate limit")
        return "add_one"

    with patch("cpp_re_agent.namer.propose_name", side_effect=fake):
        renames = pipeline.run_naming(ws, status=lambda m: None, max_workers=1)

    assert len(calls) == 2, "one failure must not abandon the pass"
    assert renames == {"FUN_00001000": "add_one"}


def test_run_naming_is_a_no_op_when_everything_is_named(tmp_path):
    ws = tmp_path / "ws"
    (ws / "improved").mkdir(parents=True)
    (ws / "improved" / "compute.cpp").write_text(
        "int compute(int h) { return h; }\n", encoding="utf-8")

    with patch("cpp_re_agent.namer.propose_name") as propose:
        assert pipeline.run_naming(ws, status=lambda m: None) == {}
    propose.assert_not_called()


def test_run_naming_resolves_collisions_across_the_pass(tmp_path):
    """Two functions proposing the same identifier must not both take it."""
    ws = _workspace(tmp_path)

    with patch("cpp_re_agent.namer.propose_name", return_value="handle_task"):
        renames = pipeline.run_naming(ws, status=lambda m: None, max_workers=1)

    assert sorted(renames.values()) == ["handle_task", "handle_task_2"]


# --- file renaming ----------------------------------------------------------

def test_files_are_renamed_alongside_the_functions(tmp_path):
    """A workspace of FUN_0010991a-0010991a.cpp is unreadable either way."""
    ws = _workspace(tmp_path)
    proposals = iter(["double_value", "add_one"])

    with patch("cpp_re_agent.namer.propose_name",
               side_effect=lambda *a, **k: next(proposals)):
        pipeline.run_naming(ws, status=lambda m: None, max_workers=1)

    names = {p.name for p in (ws / "improved").glob("*.cpp")}
    assert names == {"double_value.cpp", "add_one.cpp", "already_named.cpp"}, names


def test_rename_files_never_clobbers_an_existing_file(tmp_path):
    improved = tmp_path / "improved"
    improved.mkdir()
    (improved / "FUN_00001000.cpp").write_text("a", encoding="utf-8")
    (improved / "taken.cpp").write_text("someone else", encoding="utf-8")

    moved = namer.rename_files(improved, {"FUN_00001000": "taken"})

    assert moved == 0
    assert (improved / "taken.cpp").read_text() == "someone else"
    assert (improved / "FUN_00001000.cpp").exists(), "left in place, not lost"


def test_resume_finds_renamed_files_and_does_not_duplicate_work(tmp_path):
    """
    The trap: stage 1 resumes by checking `improved/<raw name>.cpp`. Once the
    naming pass renames that file, a re-run would improve the function again
    and write a second copy under the old name.
    """
    ws = tmp_path / "ws"
    raw_name = "FUN_00001000-00001000"
    functions = {
        raw_name: ("undefined4 FUN_00001000(int param_1)\n{\n"
                   + "\n".join(f"    int iVar{j} = param_1 + {j};" for j in range(12))
                   + "\n    return iVar1;\n}\n")
    }

    # First run improves it.
    with patch("cpp_re_agent.ai_improver.improve_function",
               return_value="int FUN_00001000(int h)\n{\n    return h;\n}\n"):
        pipeline.batch_improve(functions, ws, None, max_workers=1)
    assert (ws / "improved" / f"{raw_name}.cpp").exists()

    # Naming renames the file and records the mapping.
    with patch("cpp_re_agent.namer.propose_name", return_value="scale_handle"):
        pipeline.run_naming(ws, status=lambda m: None, max_workers=1)
    assert (ws / "improved" / "scale_handle.cpp").exists()
    assert not (ws / "improved" / f"{raw_name}.cpp").exists()

    # Re-running stage 1 must recognise the renamed file as already done.
    calls = []
    with patch("cpp_re_agent.ai_improver.improve_function",
               side_effect=lambda *a, **k: calls.append(1) or "int x(){return 0;}"):
        result = pipeline.batch_improve(functions, ws, None, max_workers=1)

    assert calls == [], "must not re-improve a function whose file was renamed"
    assert result.skipped == [raw_name]
    assert {p.name for p in (ws / "improved").glob("*.cpp")} == {"scale_handle.cpp"}


def test_names_json_records_both_maps(tmp_path):
    ws = _workspace(tmp_path)
    with patch("cpp_re_agent.namer.propose_name", return_value="compute_total"):
        pipeline.run_naming(ws, status=lambda m: None, max_workers=1)

    functions = namer.load_names(ws)
    files = namer.load_file_map(ws)
    assert "FUN_00001000" in functions
    # Keyed by file stem, which is what the resume check looks up.
    assert "FUN_00001000.cpp" not in files
    assert any(k.startswith("FUN_") for k in files)
    assert set(files.values()) <= set(functions.values())


def test_files_are_aligned_even_when_nothing_new_is_named(tmp_path):
    """
    Stage 1 renames the function inside the file but not the file itself. On the
    real tier4 workspace that left 17 files reading `FUN_0010ca7a.cpp` while the
    code inside was already `SearchContainerForValue`.
    """
    ws = tmp_path / "ws"
    improved = ws / "improved"
    improved.mkdir(parents=True)
    (improved / "FUN_0010ca7a-0010ca7a.cpp").write_text(
        "void* SearchContainerForValue(void* c) { return c; }\n", encoding="utf-8")
    (ws / "symbols.json").write_text(
        '{"FUN_0010ca7a-0010ca7a": {"new_name": "SearchContainerForValue",'
        ' "signature": "void* SearchContainerForValue(void* c)"}}',
        encoding="utf-8")

    with patch("cpp_re_agent.namer.propose_name") as propose:
        renames = pipeline.run_naming(ws, status=lambda m: None, max_workers=1)

    propose.assert_not_called(), "nothing was unnamed, so no LLM call"
    assert renames == {}
    assert (improved / "SearchContainerForValue.cpp").exists()
    assert not (improved / "FUN_0010ca7a-0010ca7a.cpp").exists()
    # And the resume map still points stage 1 at the moved file.
    assert namer.load_file_map(ws) == {
        "FUN_0010ca7a-0010ca7a": "SearchContainerForValue"}
