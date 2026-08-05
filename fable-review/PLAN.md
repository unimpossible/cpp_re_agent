# Fable Review: Findings & Improvement Plan

A holistic review of `cpp_re_agent` (ghidrecomp wrapper + LLM improver +
tree-sitter scanner + header synthesis), plus a plan for landing the
improvements. The measurement scaffolding that makes these changes testable
lives in [`eval/`](../eval/README.md).

The review was written when a Streamlit UI was the front end. **The UI has
since been deleted** and the CLI (`cpp-re`) is the only interface; findings
that existed only in the pages are marked MOOT below.

> **Status:** originally written against the tree that predates `c545203`
> ("Major refactor with opus"), `6760bc4` ("add cli runnable version") and
> `8b21fc9` ("prompt evaluation toolset. Standard library cleanup").
> Re-verified against branch `idea-callgraph` at HEAD `7130fac` on 2026-08-02;
> all `file:line` anchors below are current as of that commit. Fixed items
> have been moved to [§0](#0-already-landed) and dropped from the work list.
>
> **Related documents:** `TODO.md` (repo root, untracked) is the current
> *feature* roadmap and owns staleness detection, retrieval, and the prompt
> experiments. This document owns correctness/perf/simplification hygiene.
> Overlaps are cross-referenced inline.
>
> **2026-08-04:** the Streamlit UI (`streamlit_app.py`, `pages/`, `config.json`)
> was deleted and `cpp-re` is now the only interface. The two eval trees were
> merged into a single `eval/` package. Findings that lived only in the UI are
> struck through as MOOT rather than removed, so the review stays readable
> against the tree it was written for.

## 0. Already landed

No action needed; recorded so the list isn't re-derived.

| Finding | Landed in | Evidence |
|---|---|---|
| Mock data masquerading as real results | `8b21fc9` | `MOCK_FUNCTIONS` deleted; zero refs repo-wide |
| ChromaDB client created at import time | `c545203` | `knowledge_graph.py:13-26` lazy `_collections()`; `chromadb` import moved inside |
| Unreachable `return code` in `ai_improver` | `8b21fc9` | only `ai_improver.py:388` remains, and it is reachable |
| Commented-out `clear_db` call | — | moot: `clear_db`'s only caller was the deleted UI |
| README `pip install -r requirements.txt` | `c545203` | README now says `pip install -e ".[dev]"` |
| No pytest dev extra | `c545203` | `pyproject.toml:32` `dev = ["pytest"]` |
| Author email trailing `.` / placeholder homepage | `c545203` | both correct now |
| `sys.path.append` hacks in pages | `c545203` | src-layout package; the pages are gone entirely |
| Deprecated `gemini-1.5-flash` default | `8b21fc9` | now `gemini-2.5-flash` |

Also landed since the review, and worth knowing before planning further work:
**the callgraph feature is done** — `callgraph.py` (`build_callgraph`,
iterative `topological_order` with a `priority` tiebreaker) and `symbol_map.py`,
wired into `pipeline.batch_improve` (`pipeline.py:140-144`). The branch name
`idea-callgraph` is stale relative to the code.

## 1. Correctness & robustness findings

Ordered by impact.

1. ~~**Failed LLM calls poison the improved-code cache.**~~ **FIXED** — see
   [T1](#p0--correctness-on-the-primary-code-path). `improve_function` and
   `refine_function` raised error *strings* that `pipeline.batch_improve` wrote
   to disk as if they were code; because the batch skips on `target.exists()`,
   one transient failure was cached permanently and its bogus signature
   propagated to every later caller. Now `llm_factory.ImprovementError` is
   raised, and the file is written only when the call succeeds *and* the output
   parses.

2. ~~**Decompilation failures are partly invisible.**~~ **FIXED** — see
   [T2](#p0--correctness-on-the-primary-code-path). `DecompilationError` now
   carries ghidrecomp's captured `stderr` (a `CalledProcessError`'s `str()`
   reports only the exit status, so the real Ghidra error was being dropped),
   a missing binary raises instead of returning silently, and `get_functions`
   raises on a missing output dir instead of returning `{}`. The CLI prints a
   clean `error:` and exits non-zero.

3. ~~**Config path logic in `pages/1_Header_Synthesis.py` is wrong.**~~
   **MOOT** — the Streamlit UI was deleted. The broken `..` path, the three
   copies of `load_config` with bare `except:`, and `config.json` itself went
   with it; the CLI takes flags and never read a config file.

4. **`consolidate_definitions` doesn't check `get_llm` for `None`.**
   The function was split; `knowledge_graph.py:135-138` is the one the CLI
   actually uses and it calls `llm.invoke(prompt)` with no null-check (unlike
   `ai_improver.py:486`) — a missing API key crashes with `AttributeError`.
   The unbounded concatenation is still there too (`knowledge_graph.py:154`),
   and `pipeline._collect_definitions` (`pipeline.py:218-230`) has the
   identical shape, so the CLI inherits it. Fix the null-check here; the
   chunking/clustering half is owned by **`TODO.md` #5(C)**, which already has
   a test approach (sweep fragment count via `eval/header.py` until the single
   call fails) — don't duplicate that work.

5. **`scanner.extract_types_from_node` double-counts nested definitions.**
   Unchanged. The recursion at `scanner.py:88-89` descends into nodes it
   already captured, so a method inside a class is stored twice, bloating both
   the store and the consolidation prompt. Fix: don't recurse into children of
   a captured `struct`/`class_specifier`.

6. **Dead parameter.** `scanner.scan_code`'s ignored `provider` argument
   (`scanner.py:153`: `def scan_code(code: str, provider="ignored")`). Remove.

7. ~~**Packaging drift.**~~ **FIXED** — see
   [T8](#p1--cheap-consolidation-unblocks-everything-after). The four unused
   declarations are gone, `pyyaml` is declared, and `streamlit`/`pandas` left
   with the UI.

## 2. Performance findings

1. **`get_functions` reads every decompiled file eagerly.** *(largely
   defused)* `decompiler.get_functions` does a full `rglob("*.c")` plus an
   eager `f.read()`. This was severe under Streamlit, which re-ran it on every
   keystroke; with the UI gone it runs once per CLI invocation. What remains:
   `--dry-run` and the batch planner only need names and sizes, so reading
   every body is still wasted work on a large binary.

2. **Batch improvement is strictly sequential** (`pipeline.py:151`).
   LLM latency dominates. **New constraint the original finding didn't
   anticipate:** the loop is now deliberately leaves-first over a shared
   `symbols` map (`pipeline.py:140-144`), so naive parallelism breaks
   callee-signature propagation. Fix: parallelize *within each topological
   level* — a `ThreadPoolExecutor` per level, joined before the next level
   starts. This also isolates per-function failures.

3. **`scanner.get_parser` rebuilds the tree-sitter `Language`/`Parser` on every
   call** (`scanner.py:14-17`), and the cost has multiplied since the review:
   `score_function`/`should_improve` now call into `is_valid_cpp` and
   `complexity_metrics` per function. Build once at module level or cache.

4. **`decompiler.get_binary_md5` reads the whole binary into memory**
   (`decompiler.py:7-9`). Hash in chunks (`hashlib.file_digest` on 3.11+).

## 3. Simplifications

1. ~~**One shared `config.py`**~~ **MOOT** — the duplicated
   `load_config`/`save_config` existed only in the deleted pages (see 1.3).

2. **Centralize model selection.** *(mostly fixed)* The
   `"gemini-1.5-flash" if provider == "gemini" else ...` ternary is gone, and
   the three page copies of `DEFAULT_MODELS` went with the UI — one definition
   now remains, in `pipeline.py`. Still open: the *signature* defaults
   disagree, `llm_factory.get_llm(provider="local")` vs
   `ai_improver.improve_function(provider="gemini")` vs
   `contextual_improver.run_contextual_improvement(provider="gemini")`.
   Fix: promote `DEFAULT_MODELS` + `default_model_for` into `llm_factory.py`;
   `pipeline` and the pages import them; reconcile the defaults.

3. **Unify markdown-fence stripping.** Now in **3** places:
   `ai_improver.py:248`, `knowledge_graph.py:139`,
   `contextual_improver.py:77`. `ai_improver` already has the good
   implementation — a `_CODE_BLOCK_RE` (`ai_improver.py:9`) with the `.replace`
   chain only as a fallback inside `extract_code`. Fix: promote
   `ai_improver.extract_code` into `llm_factory.py` and have the other two call
   it — don't write a new `strip_code_fences`.

4. **`should_improve` knobs.** The line threshold is still an unnamed literal
   (`ai_improver.py:192-194`, value changed 10 → 5). The gate is now much
   richer than when this was written (library-namespace filter, stub/thunk
   detection, `_ARTIFACT_RE` density), so the fix is broader: name the
   constants and make the thresholds parameters so the eval harness can sweep
   them.

5. **Workspace path handling.** *(partially fixed: 3x → 2x in the page)* A
   helper already exists — **`pipeline.workspace_for` (`pipeline.py:41-45`)** —
   but it returns only the workspace root, and the page doesn't use it. Paths
   are still recomputed independently at `0_Decompiler.py:117-118` and
   `:181-183`, `ai_improver.improve_function:397-401`,
   `1_Header_Synthesis.py:46-48`, `2_Contextual_Improver.py:33-41`. Fix: extend
   `workspace_for` to return `(raw, improved, project_h)` and route every
   caller through it.

## 4. Knowledge store / retrieval — gated, do not fix yet

Two findings from the original review are ChromaDB-specific:

- `DB_DIR = os.path.join(os.getcwd(), "chroma_db")` (`knowledge_graph.py:8`) is
  still cwd-anchored, so the DB lands wherever the process happens to start.
- `get_all_types` relies on Chroma's default `.get()` limit
  (`knowledge_graph.py:93-95`) and silently truncates on large type sets.

**Do not action these yet, and do not read this as "retrieval is dead."**
Retrieval is open and worth investigating; ChromaDB *as the backend* is what is
leaning out. The evidence:

- Chroma's semantic `.query()` is called **nowhere** today — `knowledge_graph`
  only does exact `get(where=...)` and dump-all.
- `pipeline.py` is deliberately DB-free (docstring, `pipeline.py:14-19`); it
  scans improved files with `scanner`/`symbol_map` instead, because chromadb's
  grpc DLL is blocked on this machine.
- **Nothing calls it any more.** The Header Synthesis page was Chroma's last
  consumer, so `add_type`/`add_scan_results`/`get_all_types`/
  `get_function_by_name`/`clear_db`/`consolidate_types` (and
  `contextual_improver.run_contextual_improvement`) are now dead code.
- `TODO.md` #5 proposes the eventual backend be endpoint embeddings + numpy
  cosine behind a `Retriever` seam, *not* chromadb.

`TODO.md` #5 owns the decision gate (`--algo rag-context`; arms `c0_none` /
`c1_full_dump` / `c2_static` oracle). Resolve that gate first: if the static
oracle `c2` doesn't beat the full dump `c1` as distractors grow, no retriever
is worth building and both fixes above are deleted rather than made. If it
does, the store is rebuilt on the new seam and both fixes are moot anyway.

## 5. Measurement scaffolding

**The two eval trees have been merged** into a single `eval/` package at the
repo root (this review's `fable-review/eval/` no longer exists). It has two
halves serving different purposes — see [`eval/README.md`](../eval/README.md):

| | Similarity scoring | Prompt-optimization lab |
|---|---|---|
| Entry | `python -m eval.run_eval` | `python -m eval.run_experiment` |
| Metrics | token-stream structure, AST-shape cosine, identifier recovery, literal recovery → weighted `overall` (`similarity.py`) | parse / identifier / LLM-judge composite (`metrics.py`) |
| Needs an LLM? | No — fully offline | Yes |
| Cost | milliseconds | minutes–hours, plus tokens |
| In CI? | **Yes** — `.github/workflows/eval.yml` | No |

**Land** changes against `run_eval` (cheap, deterministic, catches
regressions); **choose** between designs with `run_experiment` (expensive,
judged). Anything landed from §1–§3 should be checked against the former;
anything from `TODO.md` against the latter.

One shared corpus (`eval/corpus/` + `manifest.yaml`, 16 cases): the tiered
lab programs, the merged `checksum`/`inventory` similarity cases, and the
multi-file `tier4_taskflow`. `examples/` holds checked-in mock raw/improved
output so the demo and tests run with no Ghidra or LLM — current scores raw
0.459, improved 0.929. Tests live in `tests/` with the project's own, so
`pytest` covers everything in one run.

## 6. Prioritized TODO

Ordered for execution. Each item is small enough to land and verify on its own.

### P0 — correctness on the primary code path

- [x] **T1. Stop caching failed LLM calls.** *(done)* New
      `llm_factory.ImprovementError`; `ai_improver.improve_function` and
      `contextual_improver.refine_function` raise it instead of returning
      `"// Error"` strings (and `refine_function` gained the missing `get_llm`
      null-check). `pipeline.batch_improve` writes the file and records the
      signature only when the call succeeds *and* the output parses — the same
      guard `run_contextual` already had — and reports failures in the new
      `StageResult.failed`, surfaced by the CLI as "failed (not written, will
      retry on re-run)". Regression tests in `tests/test_core.py`: 6 of the 7
      fail against the pre-fix tree. *(§1.1)*
- [x] **T2. Make decompile failures legible.** *(done)* New
      `decompiler.DecompilationError`, raised with ghidrecomp's captured
      `stderr` attached (`CalledProcessError.__str__` reports only the exit
      status, so the Ghidra error was being dropped), with a distinct message
      for a missing `ghidrecomp` on PATH. A missing binary now raises instead
      of returning silently, and `get_functions` raises on a non-existent
      output dir — `rglob` on a missing path yielded `{}`, indistinguishable
      from a binary with no functions. `cli.main` prints a clean `error:` and
      exits 1, and `run_stage1` warns when decompilation produced nothing.
      Ghidrecomp is now invoked as `python -m ghidrecomp` so a blocked or
      missing console-script shim can't stop a run. 5 regression
      tests, all failing against the pre-fix tree; the stale
      `test_decompiler_handles_missing_output` (which still asserted the
      deleted `MOCK_FUNCTIONS` fallback) is replaced. *(§1.2)*
- [x] **T3. Null-check the LLM in `consolidate_definitions`.** *(done)* Solved
      once for every call site rather than three times: new
      `llm_factory.require_llm` raises `ImprovementError` naming the missing
      credential (`GEMINI_API_KEY` / `LOCAL_LLM_URL`), and
      `ai_improver`/`contextual_improver`/`knowledge_graph` all use it. Landed
      with `llm_factory.stream_text`, which makes every LLM call stream — a
      blocking `invoke` puts no bytes on the wire for the whole generation and
      trips idle-read timeouts on proxies in front of remote endpoints. After
      this, `llm.stream`/`llm.invoke` appear only in `llm_factory`.
      *(§1.4 — the chunking half stays with `TODO.md` #5C.)*
- [x] **T4. ~~Shared `config.py`~~ — dropped with the UI.** *(moot)* The three
      duplicated `load_config` copies and the broken `..` path lived only in the
      Streamlit pages, which are deleted; `config.json` is gone too. The CLI is
      flag-driven. *(§1.3, §3.1)*

### P1 — cheap consolidation, unblocks everything after

- [ ] **T5. Centralize model selection** in `llm_factory.py`
      (`DEFAULT_MODELS` + `default_model_for`). The three page copies went with
      the UI, so only `pipeline.py:32-41` remains — but the *signature* defaults
      still disagree (`llm_factory.get_llm` defaults `local`,
      `ai_improver.improve_function` and `contextual_improver` default
      `gemini`), which is the half that actually bites. *(§3.2)*
- [ ] **T6. One fence-stripper.** Promote `ai_improver.extract_code` into
      `llm_factory.py`; call it from `knowledge_graph.py:139` and
      `contextual_improver.py:77`. *(§3.3)*
- [ ] **T7. Extend `pipeline.workspace_for`** to return
      `(raw, improved, project_h)`; route the five recompute sites through it.
      *(§3.5)*
- [x] **T8. Fix dependency declarations.** *(done)* Dropped the four unused
      (`google-generativeai`, `openai`, `langchain`, `langchain-community`),
      added `pandas` to runtime and `pyyaml` to the `dev` extra. The former
      `fable-review/eval/requirements.txt` folded in with the eval merge.
      *(§1.7)*
- [ ] **T9. Drop `scanner.scan_code`'s dead `provider` param**
      (`scanner.py:153`). *(§1.6)*

### P2 — performance, in cost order

- [ ] **T10. Cache the tree-sitter parser** (`scanner.py:14-17`) — one-line
      change, largest per-function win now that scoring calls it repeatedly.
      *(§2.3)*
- [ ] **T11. Chunked MD5** (`decompiler.py:7-9`). *(§2.4)*
- [ ] **T12. Lazy-load `get_functions`** (`decompiler.py`). Much less urgent
      now: the per-keystroke Streamlit rerun that made this a hot path is gone,
      so it runs once per CLI invocation. Still worth not reading every file's
      contents when only names and sizes are needed (`--dry-run`). *(§2.1)*
- [x] **T13. Parallelize the batch within topological levels.** *(done)* New
      `callgraph.topological_levels` groups functions so level N depends only on
      levels below it; `batch_improve` runs each level through a
      `ThreadPoolExecutor` and joins before the next, so callee signatures are
      always recorded before their callers run. Workers do the LLM call only —
      writing, symbol-map updates and `status`/progress stay on the calling
      thread (one writer, ordered status output), and each worker gets a
      private copy of the level-start symbol snapshot. `max_workers=1` restores
      the exact sequential path. `pipeline.DEFAULT_WORKERS = 4`, exposed as
      `cpp-re --workers N`. Results are re-sorted into
      planned order so runs stay comparable. 6 tests, including a barrier test
      that deadlocks unless calls genuinely overlap. *(§2.2)*

### Landed out of order (found by the tier4 corpus program)

- [x] **T17. Decompiled-name matching.** *(done)* `build_callgraph` compared
      call-site names (`visit`) against ghidrecomp's `<symbol>-<address>` file
      stems (`visit-00104000`), so nothing ever matched: **zero edges on real
      decompiler output**, which made the leaves-first ordering and the whole
      callee-signature propagation (`TODO.md` #1/#2, marked done) silently
      inert. `scanner.normalize_name`/`strip_address_suffix` fix the suffix;
      `ai_improver._dependencies` also had to exclude self-recursion, which
      only shows up once names are normalized. On the tier4 `-O2` binary:
      0 edges / 1 level → 185 edges / 4 levels. *(Not in the original review —
      it predates the callgraph feature.)*
- [x] **T18. Overloads and same-named methods.** *(done)* The bare stem is not
      an identity: one binary holds 8 functions stemmed `size`
      (`DependencyGraph::size` plus 7 STL instantiations), and overloads like
      `describe(int)` / `describe(const Summary&)` are indistinguishable by
      name *and* arity. New `scanner.FunctionKey` (qualifier, base, arity,
      param types) parsed from Ghidra's demangled-signature comment — the only
      place the class survives — plus `scanner.NameIndex`, which returns
      **nothing** for an ambiguous name instead of the first candidate.
      In the eval harness the same collision was worse than a missed match:
      `dataset._original_bodies` keyed originals by bare name, so
      `Scheduler::reset` and `MetricsCollector::reset` overwrote each other and
      every decompiled `reset` was scored against whichever original was
      scanned last. Now keyed by `match_key()` (namespace-insensitive, so the
      decompiled `taskflow::X::f` pairs with the source's `X::f`), with
      colliding keys dropped and warned about rather than mis-paired. Context
      maps (`contextual.program_context`, `context_retrieval.program_types`)
      instead keep *all* signatures per name — for context the whole overload
      set is the right answer, unlike an edge where guessing is harmful.

### P3 — output quality

- [ ] **T14. Fix scanner double-counting** (`scanner.py:88-89`) — don't recurse
      into children of a captured `struct`/`class_specifier`. Directly shrinks
      the consolidation prompt. *(§1.5)*
- [ ] **T15. Name and parameterize the `should_improve` thresholds**
      (`ai_improver.py:192-194` and the surrounding gate) so the harness can
      sweep them. *(§3.4)*
- [ ] **T16. Test the in-flight eval work** — `runs.json` provenance
      (`pipeline.py:62-114`) and its consumer `run_eval.read_provenance`, plus
      `sweep_models.py`, are landed and import cleanly but have no test
      coverage and have not been run end-to-end since the merge. Decide whether
      `inventory/` is a checked-in fixture or ignored. *(§5)*

### Gated / owned elsewhere — not scheduled here

- ChromaDB path anchoring and `get_all_types` pagination — blocked on the
  `TODO.md` #5 retrieval decision gate. *(§4)*
- Consolidation chunking/clustering — `TODO.md` #5(C).
- Staleness detection via source hash — `TODO.md` #6. Note this replaces the
  `target.exists()` skip that T1 works around, so sequence T1 first.
- Prompt optimization, model sweep, header-synthesis experiment — `TODO.md`
  P2 / whole-chain sweep.

Each P0–P3 item should land with a
`fable-review/eval/run_eval.py --compare` against the previous baseline where
it could plausibly move output quality (T14, T15 certainly; T1 and T3 by
removing junk from the corpus), and a plain test run otherwise.
