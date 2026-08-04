from pathlib import Path

from eval.run_eval import collect_candidates, evaluate, find_cases, infer_case, load_candidate
from eval.similarity import compare

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"

# hello_world's ground truth lives in examples/ and is reached via the corpus
# manifest, so there is exactly one copy of it in the repo.
ORIGINAL = find_cases(EVAL_DIR / "corpus")["hello_world"]
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


CASES = ["checksum", "hello_world", "inventory"]


def test_infer_case_strips_build_suffixes():
    assert infer_case("inventory-O2", CASES) == "inventory"
    assert infer_case("inventory-clang-O0", CASES) == "inventory"
    assert infer_case("hello_world.exe", CASES) == "hello_world"
    assert infer_case("inventory-O2-852ebd", CASES) == "inventory"
    assert infer_case("sample_app", CASES) is None


def _write_workspace(tmp_path):
    """The app's layout: workspace/<binary>/{raw,improved}/<function>.cpp."""
    improved = tmp_path / "inventory-O2" / "improved"
    improved.mkdir(parents=True)
    (improved / "main-00101060.cpp").write_text("int main() { return 0; }")
    (improved / "total_value-00101240.cpp").write_text("double total_value() { return 0; }")
    return improved


def test_collect_candidates_accepts_a_binary_output_dir(tmp_path):
    improved = _write_workspace(tmp_path)

    # Pointed at improved/, at the binary dir, and at the workspace root.
    for target in (improved, improved.parent, tmp_path):
        found = collect_candidates(target, CASES)
        assert set(found) == {"inventory"}, target
        assert "total_value" in found["inventory"]


def test_collect_candidates_case_override(tmp_path):
    improved = _write_workspace(tmp_path)
    renamed = improved.parent.rename(tmp_path / "unrecognizable") / "improved"

    assert collect_candidates(renamed, CASES) == {}
    assert set(collect_candidates(renamed, CASES, case="inventory")) == {"inventory"}


def test_collect_candidates_prefers_classic_layout(tmp_path):
    (tmp_path / "checksum.cpp").write_text("int crc() { return 0; }")
    assert set(collect_candidates(tmp_path, CASES)) == {"checksum"}


def test_evaluate_end_to_end_on_examples():
    raw = evaluate(EVAL_DIR / "corpus", EVAL_DIR / "examples" / "raw")
    improved = evaluate(EVAL_DIR / "corpus", EVAL_DIR / "examples" / "improved")

    # Every corpus case appears in the results, scored or not.
    assert {"checksum", "hello_world", "inventory"} <= set(raw)
    # The merged corpus also exposes the tiered lab programs, including the
    # multi-file one (a directory, not a .cpp).
    assert {"tier3_store", "tier4_taskflow"} <= set(raw)
    # Only hello_world has example candidates checked in.
    assert raw["hello_world"] is not None
    assert raw["checksum"] is None
    assert improved["hello_world"].overall > raw["hello_world"].overall
