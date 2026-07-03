import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVAL_DIR))

from run_eval import evaluate, load_candidate
from similarity import compare

ORIGINAL = (EVAL_DIR / "corpus" / "hello_world.cpp").read_text()
RAW = (EVAL_DIR / "examples" / "raw" / "hello_world.cpp").read_text()
IMPROVED = (EVAL_DIR / "examples" / "improved" / "hello_world.cpp").read_text()

UNRELATED = """
#include <cstdio>
static int fib(int n) {
    if (n < 2) return n;
    return fib(n - 1) + fib(n - 2);
}
int main() { printf("%d\\n", fib(30)); return 0; }
"""


def test_identical_code_scores_near_one():
    report = compare(ORIGINAL, ORIGINAL)
    assert report.structure > 0.99
    assert report.ast_shape > 0.99
    assert report.identifier_recovery == 1.0
    assert report.literal_recovery == 1.0
    assert report.overall > 0.99


def test_empty_candidate_scores_zero():
    assert compare(ORIGINAL, "").overall == 0.0
    assert compare("", ORIGINAL).overall == 0.0


def test_improved_scores_higher_than_raw():
    """The core property: the eval must reward recovering the original."""
    raw = compare(ORIGINAL, RAW)
    improved = compare(ORIGINAL, IMPROVED)

    assert improved.overall > raw.overall + 0.15
    assert improved.identifier_recovery > raw.identifier_recovery
    assert improved.structure > raw.structure
    # Ghidra preserves strings, so even raw output should recover most literals.
    assert raw.literal_recovery >= 0.5


def test_raw_decompilation_beats_unrelated_code():
    raw = compare(ORIGINAL, RAW)
    unrelated = compare(ORIGINAL, UNRELATED)
    assert raw.overall > unrelated.overall


def test_hex_literals_match_decimal():
    a = "int f() { return 42 + 101; }"
    b = "int f() { return 0x2a + 0x65; }"
    assert compare(a, b).literal_recovery == 1.0


def test_identifier_matching_ignores_case_and_underscores():
    a = "int unit_price; int itemCount;"
    b = "int unitPrice; int item_count;"
    assert compare(a, b).identifier_recovery == 1.0


def test_load_candidate_concatenates_directories(tmp_path):
    case_dir = tmp_path / "hello_world"
    case_dir.mkdir()
    (case_dir / "main.cpp").write_text("int main() { return 0; }")
    (case_dir / "helper.c").write_text("void helper() {}")

    blob = load_candidate(tmp_path, "hello_world")
    assert "int main" in blob
    assert "void helper" in blob
    assert load_candidate(tmp_path, "missing_case") is None


def test_evaluate_end_to_end_on_examples():
    raw = evaluate(EVAL_DIR / "corpus", EVAL_DIR / "examples" / "raw")
    improved = evaluate(EVAL_DIR / "corpus", EVAL_DIR / "examples" / "improved")

    # Every corpus case appears in the results, scored or not.
    assert set(raw) == {"checksum", "hello_world", "inventory"}
    # Only hello_world has example candidates checked in.
    assert raw["hello_world"] is not None
    assert raw["checksum"] is None
    assert improved["hello_world"].overall > raw["hello_world"].overall
