# `eval/` — measurement for the decompilation pipeline

One package, two halves. They answer different questions and cost wildly
different amounts, so keep them straight:

| | **Similarity scoring** | **Prompt-optimization lab** |
|---|---|---|
| Entry point | `python -m eval.run_eval` | `python -m eval.run_experiment` |
| Question | "did this change make output closer to the original?" | "which prompt/model/context wins?" |
| Method | 4 deterministic metrics (`similarity.py`) | LLM-as-judge composite (`metrics.py`, `judge.py`) |
| Needs an LLM? | **No** — fully offline | Yes |
| Needs Ghidra? | No (scores whatever output you point it at) | Yes (real compile → decompile round-trip) |
| Cost | milliseconds | minutes to hours, and tokens |
| In CI? | **Yes** — it's the regression gate | No |

Rule of thumb: **land** changes against `run_eval` (cheap, deterministic,
catches regressions), **choose** between designs with `run_experiment`
(expensive, judged, answers open questions).

Both halves share one corpus (`corpus/` + `corpus/manifest.yaml`) and one
`Program` loader (`corpus.py`).

## The corpus

`corpus/manifest.yaml` is the index. A program is either a single `.cpp`, a
*directory* of translation units (`tier4_taskflow/`), or a path pointing
outside `corpus/` (`hello_world` → `examples/hello_world.cpp`, which doubles as
the app's demo source — one copy, no duplicates).

Tiers: 0 free functions · 1 structs+arrays · 2 classes/methods · 3 STL ·
4 multi-file, namespaces, overloads. See
[`corpus/tier4_taskflow/README.md`](corpus/tier4_taskflow/README.md) for why
the multi-file case exists — it is the one that exercises cross-TU call graphs,
`project.h` synthesis, and name collisions.

Add a case by dropping a self-contained `.cpp` (or a directory) into `corpus/`
and adding a manifest entry. Favor small programs that exercise one thing.

## Similarity scoring

`run_eval.py` scores candidate output against the original it was compiled
from, with four metrics (defined in `similarity.py`):

| metric | measures | mostly moved by |
|---|---|---|
| `structure` | token-stream shape, names anonymized | decompiler + AI restructuring |
| `ast_shape` | AST node-type histogram overlap | decompiler + AI restructuring |
| `identifier_recovery` | original names reappearing | **the AI improvement step** |
| `literal_recovery` | strings/numbers preserved | decompiler fidelity, hallucination |

All ∈ [0, 1]; `overall` is a weighted mean. Absolute values matter less than
**deltas between runs** on the same corpus.

It reads the pipeline's output where the pipeline wrote it — no copying or
renaming:

```bash
python -m eval.run_eval                                       # all of workspace/
python -m eval.run_eval --candidates workspace/inventory-O2
python -m eval.run_eval --candidates workspace/inventory-O2/improved
```

Binary directory names map to corpus cases by stripping build suffixes, so
`inventory-O2`, `inventory-clang-O0` and `hello_world.exe` all score against
`inventory` / `hello_world`. If a name doesn't map, pass `--case`. Given a
binary's directory, `improved/` is scored; use `--stage raw` for the baseline.

### Offline quick start (no Ghidra, no LLM)

`examples/` holds hand-written mock output for `hello_world` — one Ghidra-style
raw decompilation, one AI-improved version:

```bash
python -m eval.run_eval --candidates eval/examples/improved --compare eval/examples/raw
```

### Full loop (with Ghidra)

1. `eval/build_binaries.sh` — compiles every corpus program at `-O0` and `-O2`
   into `eval/bin/` (multi-file programs are linked into one binary).
2. Run the binaries through the pipeline: `cpp-re eval/bin/inventory-O2`.
3. Score against your previous run:

```bash
python -m eval.run_eval --candidates workspace --compare runs/baseline --json results.json
```

### Model sweep

`sweep_models.py` runs the real pipeline over one binary with several models,
each into its own workspace, and scores them side by side. Ghidra output is
model-independent, so `raw/` is decompiled once and reused.

```bash
python -m eval.sweep_models --binary eval/bin/inventory-O2
python -m eval.sweep_models --binary eval/bin/inventory-O2 --models qwen/qwen3-8b openai/gpt-oss-20b
```

With no `--models`, the local endpoint (`LOCAL_LLM_URL`) is asked what it serves.

## Stripped-binary selection

`stripped.py` measures a different thing from the other two halves: not how
good the output is, but whether the pipeline **picks the right functions to
improve at all** when the binary has no symbols.

```bash
python -m eval.stripped --case tier4_taskflow
python -m eval.stripped --case tier4_taskflow --depths none,1,2,3,4 --json out.json
```

Ground truth comes from the fact that **stripping does not move code**: the
same program built with and without symbols has its functions at identical
addresses (100% correspondence on the corpus), so the unstripped decompilation
says what each stripped `FUN_<addr>` really is. A function counts as the
program's own when its unstripped name matches one defined in the corpus
source — no hand-maintained list.

It scores `pipeline.selected_functions`, the same call `batch_improve` plans
with, so the numbers cannot drift from what the tool actually does. Current
result on `tier4_taskflow-O0` (66 of 731 functions are the program's):

| `--max-depth` | selected | precision | recall | f1 |
|---|---|---|---|---|
| off | 518 | 11.4% | 89.4% | 20.2% |
| 1 | 29 | 72.4% | 31.8% | 44.2% |
| **2** (default) | **72** | **55.6%** | **60.6%** | **58.0%** |
| 3 | 132 | 35.6% | 71.2% | 47.5% |
| 4 | 210 | 23.3% | 74.2% | 35.5% |

Decompilations are cached under `experiments/stripped/`, since Ghidra takes
minutes per binary. Build the inputs first with `./eval/build_binaries.sh`,
which emits `<case>-{O0,O2}` and `<case>-{O0,O2}-stripped` for every program,
plus `lib<case>-{O0,O2}.so` for multi-file ones.

## Prompt-optimization lab

`run_experiment.py` drives the judged experiments; see
[`NOTES.md`](NOTES.md) for the lab journal and the standing design.

```bash
python -m eval.run_experiment --algo rag-context --ids tier3_store --distractors 0,20,80
python -m eval.run_experiment --algo model-sweep --models local:openai/gpt-oss-20b,gemini:gemini-2.5-flash
```

Supporting modules: `roundtrip.py` (compile → decompile → improve, cached by
source hash), `dataset.py` (per-function examples), `header.py` (struct
recovery), `context_retrieval.py` (the RAG decision gate), `chain.py`
(whole-chain runs), `tracking.py` (experiment records), `toolchain.py` (WSL
g++), `optimizers/` (GEPA-style prompt evolution).

## Tests

The eval's tests live with the project's, so one command covers everything:

```bash
pytest                      # tests/test_core.py + tests/test_similarity.py
```

`tests/test_similarity.py` pins the invariant that improved output must
outscore raw decompilation, plus the candidate-resolution rules.

## CI

`.github/workflows/eval.yml` runs three jobs, none needing Ghidra or an LLM:

- **tests** — the full `pytest` suite against the installed package, on Python
  3.10 (the oldest supported) and 3.14 (the development version). The version
  floor is deliberate: it catches imports that only exist on the newer
  interpreter.
- **build-binaries** — compiles the corpus with `g++` and `clang++` at
  `-O0`/`-O2` and smoke-tests that each binary runs.
- **eval** — installs only the five packages the offline half needs (proving it
  really is standalone), runs `tests/test_similarity.py`, and scores
  `examples/improved` against the `examples/raw` baseline, printing the table
  to the run summary and uploading `results.json`.

## Extending

- **Add a metric**: implement it in `similarity.py`, add it to `WEIGHTS`, and
  pin its expected ordering in `tests/test_similarity.py`.
- **Not yet built**: compile-check improved output (`g++ -fsyntax-only`) as a
  hard gate, call-graph similarity, CodeBLEU, execution-based I/O equivalence
  (stdin cases are already in the manifest for it).
