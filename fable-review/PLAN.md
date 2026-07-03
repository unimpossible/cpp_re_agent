# Fable Review: Findings & Improvement Plan

A holistic review of `cpp_re_agent` (Streamlit UI + ghidrecomp wrapper +
LLM improver + tree-sitter scanner + ChromaDB header synthesis), plus a plan
for landing the improvements. The measurement scaffolding that makes these
changes testable lives in [`eval/`](eval/README.md).

## 1. Correctness & robustness findings

Ordered roughly by impact.

1. **Failed LLM calls poison the improved-code cache.**
   `ai_improver.py:41,53` returns error *strings* (`"// Error: ..."`) as if
   they were code. `pages/0_Decompiler.py:114` skips any function whose
   improved file already exists, so one transient failure (rate limit, server
   down) writes a junk file that is never retried. Fix: raise an exception
   from `improve_function` instead of returning error text; only write the
   file on success; surface the error in the UI.

2. **Mock data silently masquerades as real results.**
   `decompiler.py:76-77` returns `MOCK_FUNCTIONS` whenever the output dir is
   missing or empty — which is exactly what happens when decompilation
   *fails*. A user with a broken Ghidra setup sees fake functions with no
   indication anything went wrong. Fix: gate mock data behind an explicit
   `demo_mode` flag / env var, and return an error (or empty + warning)
   otherwise.

3. **Decompilation failures are invisible.**
   `decompiler.py:62-66` swallows all exceptions with `print`, discards
   stderr (`capture_output=True`), and returns nothing; the UI then always
   reports "Decompilation Complete!" (`pages/0_Decompiler.py:85`). Fix:
   return a success/failure result (or raise) with captured stderr, and show
   it with `st.error`.

4. **Config path logic in `pages/1_Header_Synthesis.py:21-23` is wrong.**
   Streamlit runs with cwd at the project root, so
   `os.path.join(os.getcwd(), "..", "config.json")` looks *outside* the
   project and can pick up an unrelated file. Both pages also duplicate
   `load_config` with bare `except:` clauses. Fix: one shared `config.py`
   with `load_config`/`save_config`, `except (OSError, json.JSONDecodeError)`.

5. **`knowledge_graph.py:10-15` does heavy work at import time.**
   The ChromaDB client and collection are created when the module is
   imported, which creates a `chroma_db/` directory wherever cwd happens to
   be, slows every import, and makes the module untestable without a real
   DB. Fix: lazy `get_collection()` with `functools.lru_cache`, DB path
   anchored to the project root (or configurable).

6. **`knowledge_graph.consolidate_types` doesn't check `get_llm` for `None`**
   (`knowledge_graph.py:73-89`, unlike `ai_improver.py:40`) — a missing API
   key crashes with `AttributeError`. It also concatenates *every* stored
   type into a single prompt, which will blow the context window on any real
   binary. Fix: null-check + chunked/clustered consolidation (the code
   already notes this as future work).

7. **`scanner.extract_types_from_node` double-counts nested definitions.**
   The recursion at `scanner.py:54-55` descends into nodes it already
   captured, so a method inside a class is stored twice (once inside the
   class body, once as its own `function_definition`), bloating the vector
   DB and the consolidation prompt. Fix: don't recurse into children of a
   captured `struct/class_specifier`.

8. **Dead/vestigial code.** `ai_improver.py:55` (`return code` after
   `try/except` both return) is unreachable; `scanner.scan_code`'s ignored
   `provider` parameter (`scanner.py:59`) and `knowledge_graph.clear_db`'s
   commented-out call invite confusion. Remove.

9. **Docs/packaging drift.** README says `pip install -r requirements.txt`
   but only `pyproject.toml` exists (`pip install -e .`). `pyproject.toml`
   declares `google-generativeai`, `openai`, `langchain`, and
   `langchain-community`, none of which are imported (the langchain provider
   packages pull what they need). The author email has a trailing `.` and the
   homepage is a placeholder. Trim to actual imports and fix metadata; add
   `pytest` as a dev extra.

## 2. Performance findings

1. **Every Streamlit rerun re-reads every decompiled file.**
   `decompiler.get_functions` (`decompiler.py:79-83`) does a full
   `rglob("*.c")` + read on each widget interaction (every keystroke or
   click reruns the page). For a real binary that's thousands of files. Fix:
   `@st.cache_data` keyed on the output dir (or a small mtime-based cache in
   `decompiler.py`), and lazy-load file *contents* only for the selected
   function — the sidebar table only needs names and sizes.

2. **Batch improvement is strictly sequential** (`pages/0_Decompiler.py:108`).
   LLM latency dominates; a `ThreadPoolExecutor` with 4-8 workers (and a
   provider-aware rate limit) makes batch runs several times faster. This
   also isolates per-function failures.

3. **`scanner.get_parser` rebuilds the tree-sitter `Language`/`Parser` on
   every call** (`scanner.py:14-17`). Build once at module level or cache —
   it's called per file in the scan loop.

4. **`decompiler.get_binary_md5` reads the whole binary into memory**
   (`decompiler.py:7-9`). Hash in chunks (`hashlib.file_digest` on 3.11+).

5. **`knowledge_graph.get_all_types` relies on Chroma's default `.get()`
   limit** — silently truncates on large type sets. Paginate or pass `limit`.

## 3. Simplifications

1. **One shared `config.py`** replaces the duplicated `load_config`/
   `save_config` in both pages and kills the `sys.path.append` hacks
   (make the project an installable package or use a flat module layout —
   Streamlit pages can import from the root when run normally).
2. **Centralize model selection.** The
   `"gemini-1.5-flash" if provider == "gemini" else "openai/gpt-oss-20b"`
   expression appears twice in `0_Decompiler.py` (lines 120, 192) and the
   defaults differ between `llm_factory.get_llm`, `improve_function`, and the
   pages. Put a `DEFAULT_MODELS = {provider: model}` map in `llm_factory.py`
   and let callers pass only the provider. Also update the deprecated
   `gemini-1.5-flash` default to a current model.
3. **Unify markdown-fence stripping.** `.replace("```cpp", "").replace("```", "")`
   appears in both `ai_improver.py:50` and `knowledge_graph.py:90`; a shared
   `strip_code_fences()` (regex, handles leading language tags generally)
   belongs in `llm_factory.py`.
4. **`should_improve` magic number** (`ai_improver.py:12`): name it
   (`MIN_LINES_TO_IMPROVE = 10`) and make it a parameter so the eval harness
   can sweep it.
5. **Improved-file handling in `0_Decompiler.py`** duplicates dir-creation
   and path logic in three places (lines 97-98, 111, 175-177); extract a
   `workspace_paths(binary_path)` helper returning raw/improved dirs.

## 4. Measurement scaffolding (built in this review)

`eval/` contains a working similarity harness — see
[`eval/README.md`](eval/README.md):

- `similarity.py` — four deterministic, offline metrics (token-stream
  structure, AST-shape cosine, identifier recovery, literal recovery) with a
  weighted `overall` score.
- `run_eval.py` — scores a candidate output directory against `corpus/`
  originals, with `--compare` for baseline-vs-experiment deltas and `--json`
  for tracking runs over time.
- `corpus/` — three compilable ground-truth programs;
  `build_binaries.sh` produces `-O0`/`-O2` binaries for real Ghidra runs.
- `examples/` — checked-in mock raw/improved outputs so the demo and tests
  run with no Ghidra or LLM. Current demo scores: raw 0.459, improved 0.929.
- `tests/` — 8 pytest cases pinning the invariant that improved output must
  outscore raw decompilation.

## 5. Suggested landing order

| Phase | Work | Why first |
|---|---|---|
| 1 | Eval scaffolding (**done**, this branch) | Everything else becomes measurable |
| 2 | Fixes 1.1-1.4 (error handling, mock gating, config) | Correctness; cheap; unblocks trusting batch runs |
| 3 | Perf 2.1-2.4 + simplifications 3.1-3.5 | Makes iterating on real binaries tolerable |
| 4 | Scanner dedup (1.7) + consolidation chunking (1.6) | Header synthesis quality on real binaries |
| 5 | Experiments via the eval loop: prompt variants, models, passing `project.h` context into `improve_function`, compile-check gate (`g++ -fsyntax-only`) on improved output | The actual quality wins, now with numbers |

Phase 5 is where the harness pays off: each idea is a branch, a
`run_eval.py --compare` against the previous baseline, and a kept-or-reverted
decision based on the delta.
