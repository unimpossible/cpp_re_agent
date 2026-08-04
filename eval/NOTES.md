# Lab journal — prompt optimization for decompiled-C++ recovery

Running record of hypotheses, setups, and results. Written to be liftable into a
paper's method/results. Newest entries at the bottom. Each experiment dir under
`experiments/<id>/` is self-contained and re-executable; this file is the
narrative over them.

## Framework design (the standing setup)

**Objective.** Given a binary, recover source as close to the original as
possible. Decompilation is lossy (comments, exact names, template/STL sugar are
gone), so we optimize a *blend*, gated on "it parses": identifier recovery +
LLM-judge now; semantic re-decompile proxy + CodeBLEU next (P2).

**Round-trip** (`eval/roundtrip.py`): compile (WSL g++ 14.2, `-O2`, **symbols
kept for now**) → decompile (ghidrecomp/Ghidra) → improve each user function
with the prompt under test. Compile+decompile are cached on disk by source hash,
so only the LLM step and metrics re-run per experiment. Decompiled functions are
matched back to the original by name (`gcd-001012e0` → `gcd`); CRT glue is
dropped.

**Why symbols are kept (not stripped) in P1.** Keeping symbols lets us map each
decompiled function to its original by name, giving clean *per-function* signal.
Stripping (the real RE target) destroys names and is a planned difficulty axis,
not the first variable. Even unstripped, Ghidra still destroys all *local*
variable and parameter names (`param_1`, `iVar2`), so genuine recovery is still
being measured.

**Metrics** (`eval/metrics.py`, `eval/judge.py`):
- *Parse gate* — `scanner.is_valid_cpp`; score 0 if the reconstruction doesn't
  parse.
- *Identifier recovery* — recall of original identifiers that were **lost in
  decompilation** (present in original, absent from raw) and recovered in the
  output. Measures real reconstruction, not names that survived. (Single-char
  locals are excluded as noise, so this signal is thin at tier 0 and meatier
  from tier 1 up.)
- *LLM judge* — rubric over correctness/readability/naming/types + a critique;
  the critique is the feedback channel GEPA will consume in P2.
- *Composite* — parse-gated weighted blend (`identifier` 0.4, `judge` 0.6).

**Models / creds.** Task + judge models resolved via `llm_factory.get_llm`;
credentials come only from `.env` (a remote OpenAI-compatible endpoint +
Gemini). Langfuse tracing is already wired and captures every call.

**Tracking** (`eval/tracking.py`): `experiments/<ts>_<algo>/` with `config.yaml`
(+ env/version/git snapshot for reproducibility), `candidates/`, append-only
`results.jsonl`, `summary.json`; plus cross-run `INDEX.md` and `leaderboard.csv`.

---

## P1 — harness + control baseline

**Hypothesis.** The current hand-written `DEFAULT_SYSTEM_PROMPT` produces a
measurable but clearly improvable baseline on the composite metric, establishing
the floor GEPA must beat in P2.

**Setup.** `uv run python -m eval.run_experiment --algo control --ids
tier0_numbers tier1_stats`. Task + judge model `openai/gpt-oss-20b` via the
`.env` endpoint. Composite weights identifier 0.4 / judge 0.6.

**Result** (run `20260618_091832_control`, mean **0.51**):

| program | composite | identifier | judge | judge correctness |
|---|---|---|---|---|
| tier0_numbers | 0.73 | 1.0 (1/1) | 0.55 | 0.6 |
| tier1_stats | 0.29 | 0.5 (3/6) | 0.15 | 0.1 |

Two failure modes recur in the judge critiques, both optimizable:
1. **Duplicate / conflicting definitions.** Each function is improved
   independently (P1 uses no shared context), so the model re-declares
   `struct Sample` / functions and re-emits `#include`s; the concatenated
   reconstruction has conflicting definitions "which would break compilation."
2. **Behavioral drift.** Despite "Do NOT change functional behavior," the model
   mis-implements logic (tier1: wrong mean/argmax, *hard-codes* the mean output;
   tier0: widens `int`→64-bit in Collatz). The instruction is not binding enough.

**Methodology notes (for the write-up).**
- *Variance:* `tier1_stats` scored 0.09 in an earlier single run vs 0.29 here.
  The `--seed` controls corpus/python RNG but **not** endpoint sampling, so
  composite is noisy per run. → report mean ± std over N≥3 runs before claiming
  a GEPA win; add a `--repeats` flag.
- *Cost/latency:* improve step is slow on this endpoint — 137 s (tier0, 3 fns)
  and 555 s (tier1, 3 fns). GEPA does many rollouts, so P2 needs minibatching,
  the existing compile/decompile cache (already saving the slow stage), and
  likely a smaller/faster judge model.
- *Tier-0 identifier signal is thin* (1 recoverable id) — as predicted; lean on
  tiers 1+ and the judge for naming signal.

**Interpretation / next (P2 GEPA hypotheses).** The baseline leaves large
headroom (0.51, and only 0.29 once structs appear). Concrete prompt-evolution
targets GEPA should discover from the critique feedback: (a) "emit each type /
include exactly once; assume callees/types are declared elsewhere" to curb the
conflicting-definitions failure; (b) a stronger behavior-preservation constraint.

**On "whole-program" (corrected framing).** The improver is per-function and
stays that way — we can't (and shouldn't) feed an entire program into one
improve call. The genuinely whole-program prompt already exists elsewhere:
`knowledge_graph.consolidate_types` (header synthesis), which merges all
extracted types into one `project.h`. That is a *separate* optimization target
with its own metric and gets its **own experiment** (see TODO: "Header-synthesis
prompt experiment"), not folded into the improver experiment. So P2 GEPA
optimizes only the per-function improver prompt.

**Two failure modes, two different fixes.**
- *Identical-function duplication* → fixed in the harness: `roundtrip` now
  de-dups by body fingerprint (`_dedup_targets`), so true duplicates are
  improved and emitted once. (For our distinct-function corpus this is a no-op,
  but it cuts cost and duplicate output on real binaries.)
- *Struct re-declaration across distinct functions* → NOT a dedup case; it is a
  prompt/context concern. Addressed by the GEPA "declare once" hypothesis and,
  ultimately, by injecting a synthesized `project.h` as shared context (the
  header-synthesis experiment).

---

## P2 — GEPA-style reflective prompt optimization

**Model: the local endpoint, not Gemini.** The tool runs against the `.env`
OpenAI-compatible endpoint (`openai/gpt-oss-20b`), so the prompt must be
optimized *for that model* — a Gemini-tuned prompt may not transfer. (Gemini
Flash was trialed for speed, ~1 s/call vs ~45–185 s/call, but dropped per this
requirement.) Task, judge, and reflection models are all `gpt-oss-20b`.

**Optimizer** (`eval/optimizers/reflective.py`): GEPA's core ideas implemented
directly over our round-trip metric — evaluate a candidate per function (score +
critique) → reflect on the worst cases with an LLM to mutate the *instruction*
(steered by rotating focus angles so deterministic temp-0 reflection still
diversifies) → score mutations on held-out val → keep a Pareto front, sample the
next parent weighted by per-example wins. Budget knobs: `iterations`,
`candidates`, `minibatch` (train examples used for reflection feedback — keeps
the expensive eval small on the slow endpoint). Results are cached by
(prompt, example). The `gepa`/DSPy libraries are a clean future swap-in.

**Unbiased comparison.** 3-way split: optimize on train+val (val = selection),
report seed-vs-best on a **test** set never used for selection. Metric is
per-function `score_pair` (parse gate + identifier recovery + judge), the same
scorer `composite` uses whole-program.

**Corpus expansion + a finding.** Grew to 10 programs (tiers 0–2). Tier-2 class
programs decompiled to ~1 target each under `-O2`: **the optimizer inlines small
class methods into their callers, erasing method boundaries** — a real RE
observation, and why per-function signal concentrates in tiers 0–1.

**Header experiment wired** (`eval/header.py`, `--algo header-control`): improve
functions → collect the messy struct fragments → synthesize `project.h` via
`knowledge_graph.consolidate_definitions` → score struct/field recovery vs the
original. (Also required making `knowledge_graph` import lazily — chromadb's
grpc DLL is blocked by this machine's Application Control policy.)

**Results & the methodology arc (the interesting part).**

1. *First local run* (9 fns, val 2 / test 2): val improved +0.10 but **test
   collapsed −0.33**. The held-out test set did its job — exposed pure val
   overfitting at tiny split sizes.
2. *Powered run* (15 fns, val 5 / test 5, + 2 hand-written seed variants):
   same story — best-on-val (a seed variant) scored −0.19 on test. Crucially,
   the *same* seed prompt scored **0.48 on val but 0.737 on test** — a 0.26 gap
   for one prompt. Diagnosis: with ~5 functions per split and high per-function
   score variance, a split's mean is dominated by *which functions land in it*,
   so val cannot predict test. Train/val/test is the wrong design for a corpus
   this small.
3. *Fix — paired bakeoff* (`--algo bakeoff`): score every prompt on the **same**
   full 15 functions and compare **per function** (paired), removing the
   difficulty confound. Result, honestly: **the default prompt wins** (0.589)
   vs seedvar1 0.467 and seedvar2 0.540. The hand-written "principled" variants
   were *worse*. The default is a strong, well-tuned baseline; the earlier
   "improvements" were entirely split noise.

**Where the default actually fails (per-function judge critiques).** This is the
real signal for beating it:
- *Integer-type widening* — popcount / reverse_bits / is_power_of_two (~0.51–
  0.57) all penalized for `unsigned int → uint32_t`. Consistent, fixable.
- *Over-elaboration vs the minimal original* — gcd / is_prime / distance /
  word_count lose points for added namespaces, extra comments, `std::hypot` for
  `std::sqrt`, `std::abs` for a ternary, defensive checks. The default's own
  "add comments" / "create structs where appropriate" instructions backfire
  against a fidelity metric.
- *Invented wrong struct layouts* — mean_value / argmax_value / find_by_id guess
  struct fields/types that don't match.
- *Catastrophic on float/struct math* — polygon_perimeter (0.09): wrong return
  type, ignores point data.

**Next.** Evidence-based variants (V3 faithfulness-first: keep original types,
minimal style, don't invent structs; V4 a terse version) in a paired bakeoff vs
the default. _(result filling after run `…_bakeoff` completes)_

**Methodology lesson for the write-up.** On small eval corpora, *paired*
per-item comparison on a shared set beats train/val/test splits, which are
dominated by item-assignment variance. And blind reflective search wastes budget
versus reading the metric's own per-item failure critiques and writing a
targeted prompt — the judge's feedback channel is more useful read directly than
fed to an automatic mutator at this scale.

**Harness coverage — all three prompts now have experiments.** The tool has
exactly three LLM prompts, each now optimizable via an injectable base prompt +
a `--algo` runner over the same metric/tracking:
1. per-function improver (`ai_improver.DEFAULT_SYSTEM_PROMPT`) — `control` / `gepa` / `bakeoff`;
2. header / type-merging (`knowledge_graph.DEFAULT_CONSOLIDATE_PROMPT`) — `header-control`;
3. contextual "function-merging" (`contextual_improver.DEFAULT_CONTEXTUAL_PROMPT`)
   — `contextual-control`: first-pass improve → refine with project types +
   callee signatures (extracted from the original program), scored refined-vs-base.

**Corpus expansion (offline, harness-iteration step).** Added two tier-1
programs to deepen per-function signal without touching the endpoint:
`tier1_running` (int-array reductions: sum, Kadane max-subarray, longest run)
and `tier1_fraction` (a `Fraction` struct whose `add`/`reduce` share a `gcd`
callee). Both compile (WSL g++) and decompile cleanly with **all** functions
surviving as name-matchable targets — `tier1_fraction` notably keeps
`add → reduce → gcd` un-inlined, so it exercises the callgraph/symbol-map
propagation path rather than only isolated functions. Net **+7 function
examples** for the dataset; decompile cache pre-warmed so a future bakeoff
re-runs only the LLM step. Corpus is now 12 programs.

**Reliability fixes (both bit long local-endpoint runs).** (a) Langfuse tracing
disabled for batch runs (`DISABLE_LANGFUSE=1`) — its OTLP exporter timed out
against an unreachable server and killed a run mid-way. (b) Added a request
timeout + retries to `get_llm` (`LLM_TIMEOUT`, default 300s) — a single hung
streaming call had stalled a run for 12 min with no progress. Both were caught
as "is it stuck?" symptoms; now runs fail fast and resume from the disk cache.

---

## Context retrieval (TODO #5) — decision gate before any vector DB

**Framing.** "Retrieve relevant types instead of dumping `project.h`" bundles
three claims with different evidence bars: (A) cost — fewer tokens (trivially
true); (B) quality via relevance — trimming distractors *improves* output (the
uncertain one); (C) `consolidate_types` scaling (a capability threshold, its own
track). Only B needs an experiment, and **B is unmeasurable on a small corpus**:
when `project.h` is a handful of structs there is nothing to retrieve and nothing
to distract, so retrieval can at best tie dump-all.

**Reality check.** ChromaDB's semantic `.query()` is used *nowhere* in the repo
(grep: zero hits) — `knowledge_graph` is exact `get(where=...)` + dump-all. And
its grpc DLL is blocked here (same reason the harness is DB-free). So #5 is
*introducing* retrieval; the backend should be endpoint embeddings + numpy cosine
behind a `Retriever` seam, not chromadb.

**The cheap decisive design — static-deps as the ORACLE.** The static call graph
is perfect-knowledge relevance: the upper bound on what *any* retriever (always
noisier) could buy. It needs no embeddings, no DB, no blocked DLL. So the gate
compares, prompt held fixed, only context varied (`eval/context_retrieval.py`,
`--algo rag-context`): `c0_none` (floor) / `c1_full_dump` (current behavior,
padded with K synthetic distractor types) / `c2_static` (oracle: only referenced
types + actual callee sigs from the original). Paired per function. The
distractor sweep (`--distractors 0,20,80`) is the lever that gives `c1` room to
fail: as K grows `c1`'s `project.h` balloons while `c2` stays flat. **Logic of
the gate:** if `c2` doesn't beat `c1` even under heavy distraction, semantic
retrieval isn't worth building; build embeddings/`c3_semantic` only if the gate
shows headroom AND a regime where the call graph is unavailable (indirect/virtual
calls, stripped). Either outcome is publishable — a flat `c1` is the honest "the
model just ignores irrelevant types" result, in the spirit of "the default prompt
wins."

**Harness change.** `ai_improver.improve_function` gained `extra_context=` — a
pre-formatted block injected verbatim, bypassing the binary_path/symbol-map
gathering, so the experiment controls exactly what the model sees while reusing
the *same* prompt assembly (section headers identical). Output cached by
(model, prompt, context, code) so the K-independent `c0`/`c2` are sent once and
sweeps re-run cheaply.

**Corpus — `tier3_store` (the missing size axis).** Tiers 0-2 are too small to
distract. Added a tier-3 point-of-sale program: six structs (Money / Product /
Customer / OrderLine / Order / Catalog) and a multi-level call graph
(`order_total -> order_subtotal -> line_subtotal -> {find_product, money_scale}`).
Compiles + runs (WSL g++ `-std=c++17 -O2`; `total=1228 points=552 tier=2`).
`find_product` is called from two sites so it should survive inlining as a target.
The point: each function references only a *subset* of the six types, so the
oracle context is far smaller than the dump even at K=0 — smoke-measured
`find_product` static **102c** vs full-dump **1131c** (~11×); at K=80 distractors
full-dump is **~10.5KB** while the oracle is unchanged. That gap is the
experiment's discriminating power.

**Status.** Scaffolded and smoke-tested (strategy construction + CLI wiring +
corpus compile). Not yet run against the endpoint — that produces the first
`c1`-vs-`c2` numbers and the degradation curve.

**Metric bug found while interpreting the first rag-context run (FIXED).** The
first bakeoff looked like "distractors *improve* recovery +0.27" — mechanistically
backwards and non-monotone (k20 > k80 > k0), i.e. the signature of endpoint
sampling noise at n≈12 with no repeats. Chasing it surfaced a real, silent bug:
`metrics.extract_identifiers` (and the two `header.py` struct extractors) sliced a
Python **str with tree-sitter byte offsets** — correct only for pure ASCII; any
non-ASCII char shifts every span after it. `tier3_store.cpp` had an em dash in a
comment, which corrupted its identifier component across all conditions. Fixed all
three extractors to use `node.text` (always offset-correct), cleaned the corpus to
ASCII, added a non-ASCII regression check. (`scanner.py` was already correct — it
slices bytes then decodes.) Lesson for the write-up: the identifier metric was an
ASCII-only landmine; the rag-context table that exposed it is not yet
interpretable and must be re-run on the fixed metric with `--repeats`.

---

## Whole-chain model sweep — which task model is best end-to-end

**Objective.** Pick the best *single* task model for the entire chain, not per
prompt. One model drives all three LLM stages — stage-1 improve → synthesize
`project.h` from its own improved fragments → stage-2 contextual refine — and we
score the FINAL refined output against the original. A FIXED judge is the only
constant, so the task model is the sole variable.

**Design** (`eval/chain.py` + `--algo model-sweep`). Reuses the production
orchestration: `run_chain` calls the real `ai_improver.improve_function`,
`knowledge_graph.consolidate_definitions`, `contextual_improver.refine_function`,
and the production `pipeline._callee_context` — so the experiment measures the
*actual* chain, not a re-implementation. Decompile is the cached, model-
independent step; dedup (`roundtrip._dedup_targets`) means identical bodies are
improved/refined once. A "model" is a `provider:model` pair (`get_llm` takes
both), e.g. `local:openai/gpt-oss-20b,gemini:gemini-2.5-flash`; bare ids use
`--provider`.

**What it reports per model:** stage-1 composite vs chain (refined) composite
(so the `Δs2` column shows whether stage 2 *helped* for that model — the same
question as `contextual-control`, but per model and against the model's own
synthesized header), header struct recovery, and real cost (uncached LLM calls +
wall-seconds). `--repeats N` gives mean ± std (the local endpoint is noisy;
Gemini at temp 0 is deterministic so its std is ~0). Ranked by chain mean.

**Methodology cautions baked in.**
- *Fixed judge, self-bias.* The judge is held constant across all task models. If
  the judge model is also a candidate, it tends to prefer its own outputs — the
  runner prints a warning on that row. Prefer a judge *outside* the candidate set;
  with only two reachable models, read the judge's own row skeptically.
- *Cross-stage coupling is the point.* Stage 2 uses the model's OWN synthesized
  header, so a model that writes a bad header pays for it downstream — a fair
  whole-chain test, unlike scoring each stage with oracle context.
- *Cost.* ~(2·fns + 1) task calls + judge per program per model per repeat — large
  on the slow endpoint. Use `--ids`/`--limit`; task outputs are cached by
  (provider, model, role, repeat, input) so crashes resume and re-runs are cheap.
  Local endpoints (LM Studio) usually serve one model at a time, so a multi-local
  sweep may mean swapping the loaded model between invocations; Gemini + one local
  runs in a single pass.

**Status.** Built; orchestration smoke-tested with stubbed decompile + stubbed
stages (dedup collapses duplicates: s1 calls = unique bodies, header synth once,
scoring matches functions to originals). Not yet run against live endpoints — that
produces the first cross-model leaderboard.
