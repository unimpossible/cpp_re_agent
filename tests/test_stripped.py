"""
Tests for the stripped-binary selection harness (`eval.stripped`).

All synthetic — no Ghidra, no LLM. The harness's own arithmetic and the
address-pairing trick it rests on are what these pin down; the real numbers
come from running it against the corpus.
"""
from unittest.mock import patch

import pytest

from cpp_re_agent import pipeline
from eval import stripped as strip_eval


def _body(name, calls=(), lines=12):
    body = [f"    int iVar{j} = param_1 + {j};" for j in range(lines)]
    body += [f"    {c}(param_1);" for c in calls]
    return (f"undefined4 {name}(int param_1)\n{{\n"
            + "\n".join(body) + "\n    return iVar1;\n}\n")


def test_address_is_parsed_from_the_ghidrecomp_name():
    assert strip_eval.address_of("FUN_00108cdb-00108cdb") == 0x108CDB
    assert strip_eval.address_of("main-00102828") == 0x102828
    assert strip_eval.address_of("no_address_here") is None


def test_ground_truth_labels_program_code_from_the_unstripped_build():
    """
    The pairing trick: stripping does not move code, so the unstripped name at
    an address says what the stripped FUN_<addr> really is.
    """
    plain = {
        0x1000: ("main-00001000", _body("main")),
        0x2000: ("add_task-00002000", _body("add_task")),
        0x3000: ("_M_realloc_insert-00003000", _body("_M_realloc_insert")),
    }
    truth = strip_eval.ground_truth(plain, {"main", "add_task", "run"})

    assert truth[0x1000] is True
    assert truth[0x2000] is True
    assert truth[0x3000] is False, "libstdc++ is not the program's own code"


def test_score_selection_counts_against_the_paired_addresses():
    stripped = {
        0x1000: ("FUN_00001000", _body("FUN_00001000")),
        0x2000: ("FUN_00002000", _body("FUN_00002000")),
        0x3000: ("FUN_00003000", _body("FUN_00003000")),
        0x4000: ("FUN_00004000", _body("FUN_00004000")),
    }
    truth = {0x1000: True, 0x2000: True, 0x3000: False, 0x4000: False}

    # Pretend the pipeline selected three of them, two of which are real.
    chosen = ["FUN_00001000", "FUN_00002000", "FUN_00003000"]
    with patch("cpp_re_agent.pipeline.selected_functions", return_value=chosen):
        result = strip_eval.score_selection(stripped, truth, depth=2)

    assert result.selected == 3
    assert result.true_positives == 2 and result.false_positives == 1
    assert result.total_user == 2
    assert result.precision == pytest.approx(2 / 3)
    assert result.recall == pytest.approx(1.0)
    assert result.f1 == pytest.approx(2 * (2 / 3) * 1.0 / ((2 / 3) + 1.0))


def test_selection_metrics_are_zero_safe():
    empty = strip_eval.Selection(depth=1, selected=0, true_positives=0,
                                 false_positives=0, total_user=0)
    assert (empty.precision, empty.recall, empty.f1) == (0.0, 0.0, 0.0)


# --- the semantics the harness depends on -----------------------------------

def _stripped_chain():
    """main -> a -> b -> c, all FUN_-named, so it reads as stripped."""
    return {
        "FUN_00001000": _body("FUN_00001000", ["FUN_00002000"]),
        "FUN_00002000": _body("FUN_00002000", ["FUN_00003000"]),
        "FUN_00003000": _body("FUN_00003000", ["FUN_00004000"]),
        "FUN_00004000": _body("FUN_00004000"),
    }


def test_max_depth_zero_disables_the_filter_entirely():
    """
    Regression: `--max-depth 0` is documented as "disable", but was being
    mapped to None, which means *auto* — on a stripped binary that is the
    default depth, so the flag silently did the opposite of what it said.
    """
    functions = _stripped_chain()
    assert pipeline.looks_stripped(functions) is True

    auto = pipeline.selected_functions(functions)                  # None = auto
    off = pipeline.selected_functions(functions, max_depth=0)      # 0 = off

    assert len(off) > len(auto), "0 must widen the selection, not repeat auto"
    assert set(off) == set(functions)
    assert "FUN_00004000" not in auto, "auto stops at DEFAULT_MAX_DEPTH"


def test_selected_functions_matches_what_the_batch_would_schedule(tmp_path):
    """
    The harness scores `selected_functions`; if that drifted from the real
    batch loop the measurements would be about nothing.
    """
    functions = _stripped_chain()

    scheduled = []
    with patch("cpp_re_agent.ai_improver.improve_function",
               side_effect=lambda code, **kw: (
                   scheduled.append(next(n for n, c in functions.items() if c == code))
                   or "int r(int h)\n{\n    return h;\n}\n")):
        pipeline.batch_improve(functions, tmp_path / "ws", None, max_workers=1)

    assert set(scheduled) == set(pipeline.selected_functions(functions))
