# Decompilation Similarity Eval

Scaffolding for measuring how close the pipeline's output (raw ghidrecomp
decompilation, or the AI-improved version) is to the *original* source it was
compiled from. Use it to A/B test improvement ideas — prompt changes, models,
post-processing — with a number instead of eyeballs.

## How it works

`corpus/` holds small C++ programs whose source we control (the ground truth).
A *candidate* directory holds the pipeline's output for those same programs.
`run_eval.py` scores each candidate against its original with four metrics
(see `similarity.py` for definitions):

| metric | measures | mostly moved by |
|---|---|---|
| `structure` | token-stream shape, names anonymized | decompiler + AI restructuring |
| `ast_shape` | AST node-type histogram overlap | decompiler + AI restructuring |
| `identifier_recovery` | original names reappearing | **the AI improvement step** |
| `literal_recovery` | strings/numbers preserved | decompiler fidelity, hallucination |

All metrics ∈ [0, 1]; `overall` is a weighted mean. Absolute values matter
less than **deltas between runs** on the same corpus.

## Quick start (offline, no Ghidra needed)

`examples/` contains hand-written mock outputs for `hello_world` — one
Ghidra-style raw decompilation, one AI-improved version:

```bash
cd fable-review/eval
python run_eval.py --candidates examples/improved --compare examples/raw
```

Run the tests (they pin the key invariant: improved must outscore raw):

```bash
pytest fable-review/eval/tests -v
```

## Full loop (with Ghidra)

1. `./build_binaries.sh` — compiles the corpus at `-O0` and `-O2` into `bin/`.
2. Run each binary through the pipeline (app UI, or `ghidrecomp` + batch
   improve). Collect output as `<outdir>/<case>.cpp` or `<outdir>/<case>/`
   (a directory of per-function files is concatenated automatically — the
   app's `workspace/<binary>/improved/` layout works as-is).
3. Score and compare against your previous run:

```bash
python run_eval.py --candidates runs/new-prompt --compare runs/baseline --json results.json
```

## Extending

- **Add a corpus case**: drop a self-contained `.cpp` into `corpus/`. Favor
  small programs that exercise one thing (virtual dispatch, templates,
  std::map, error handling...).
- **Add a metric**: implement it in `similarity.py`, add it to `WEIGHTS`, and
  pin its expected ordering in `tests/test_similarity.py`.
- **Ideas not yet built**: compile-check the improved output (`g++ -fsyntax-only`
  as a hard gate), call-graph similarity, LLM-as-judge for comment quality,
  CodeBLEU.
