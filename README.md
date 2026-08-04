# AI Reverse Engineering Agent

WORK IN PROGRESS - FOR TESTING PURPOSES ONLY

A command-line tool that decompiles a C++ binary with Ghidra and uses an LLM to
push the result back toward readable source: real names, real types, and a
synthesized `project.h`.

## How it works

```mermaid
flowchart TD
    BIN(["binary"]) --> GR["ghidrecomp — decompile"]
    GR --> RAW[("raw/ — one .c per function")]

    RAW --> CG["build call graph, order leaves-first,<br/>group into dependency levels"]
    CG --> GATE{"worth improving?"}
    GATE -->|"library / stub / already done"| SKIP["skip"]
    GATE -->|"yes"| CTX

    subgraph L1["LOOP A — per function, parallel within a level"]
        direction LR
        CTX["assemble prompt context:<br/>project.h + callee signatures<br/>+ the raw decompiled function"]
        CTX --> AI1{{"LLM #1 — improve<br/>rename, retype, de-artifact"}}
        AI1 --> V1{"parses as C++?"}
        V1 -->|"no — one retry, then reject"| CTX
        V1 -->|"yes"| W1["write improved/NAME.cpp<br/>record recovered signature"]
    end

    W1 --> SYM[("symbols.json<br/>original name to new signature")]
    SYM -.->|"callee signatures feed the next level up"| CTX
    W1 --> IMP[("improved/")]

    IMP --> COLL["scan improved files for<br/>struct / class / declaration fragments"]
    COLL --> AI2{{"LLM #2 — merge all fragments<br/>into one coherent header"}}
    AI2 --> PH[("project.h")]

    PH --> RCTX
    IMP --> RCTX

    subgraph L2["LOOP B — per function refine"]
        direction LR
        RCTX["project.h + callee signatures<br/>+ the improved function"]
        RCTX --> AI3{{"LLM #3 — refine against<br/>whole-program types"}}
        AI3 --> V2{"parses as C++?"}
        V2 -->|"no"| KEEP["keep the<br/>previous version"]
        V2 -->|"yes"| W2["overwrite<br/>improved/NAME.cpp"]
    end

    PH -.->|"on a re-run, seeds LOOP A"| CTX
```

### Where the decompiled code meets the model

There are exactly **three** LLM boundaries, and they see different things:

**LLM #1 — the improve loop (per function).** This is where raw decompiler
output first reaches the model. Each function is sent on its own, never the
whole binary, wrapped in whatever context has been recovered so far: the
`project.h` from a previous run if one exists, plus the *already-improved*
signatures of the functions it calls. That ordering is the point of the call
graph — a caller is only sent after its callees, so by the time
`process_order` is improved, the model has already been told that
`FUN_00401820` is really `Order *find_order(Catalog *, int)`. Output must parse
as C++ or it gets one corrective retry, then is rejected and **not written**, so
a failure is retried on the next run rather than cached forever.

**LLM #2 — header synthesis (once).** Stage 1 leaves struct and class fragments
scattered across dozens of improved files, often partial and contradictory.
Every fragment is scanned out and sent in a single call to be merged into one
`project.h`.

**LLM #3 — the refine loop (per function).** Now that a whole-program view of
the types exists, every improved function is sent back with `project.h`
attached and rewritten against it. This is what turns `void *param_1` into
`Catalog *catalog` consistently across files. Output that doesn't parse leaves
the stage-1 version in place.

Note the order: `project.h` is built **before** the refine loop, from stage 1's
output — and the refine loop is what consumes it. Because it is written to the
workspace, a second `cpp-re` run also feeds it into LOOP A (the dashed edge),
so each run starts with better type context than the last.

### Loop shapes

**LOOP A is parallel within a level, sequential across levels.** Functions in
one dependency level cannot call each other, so they are improved concurrently
(`--workers`); the next level starts only after every signature from the
previous one is recorded. That is why parallelism is per-level rather than a
flat thread pool — a flat pool would improve callers before their callees and
lose the signature propagation entirely.

**LOOP B is a single pass**, one call per improved function.

Runs are **resumable**. Output already written is skipped, and a failed function
is never written, so re-running retries exactly what failed.

## Prerequisites

1. **Ghidra** — installed, with `GHIDRA_INSTALL_DIR` set.
2. **Python 3.10+**
3. **An LLM** — either a Google API key, or a local OpenAI-compatible server
   (LM Studio, Ollama, llama.cpp, ...).

## Installation

```bash
pip install -e ".[dev]"
```

Configure via `.env` (or the environment):

```
GHIDRA_INSTALL_DIR=C:\Path\To\Ghidra
GEMINI_API_KEY=your_key_here
LOCAL_LLM_URL=http://localhost:1234/v1
```

## Usage

```bash
cpp-re path/to/binary                      # both stages
cpp-re path/to/binary --dry-run            # plan only: no LLM calls, no cost
cpp-re path/to/binary --stages improve     # stage 1 only
cpp-re path/to/binary --provider gemini --workers 8
cpp-re path/to/binary --limit 10 --json summary.json
```

`cpp-re --help` lists everything. Also runnable as `python -m cpp_re_agent`.

Start with `--dry-run`: it decompiles (cached afterward) and reports how many
functions would be improved, the call-graph shape, and how many sequential LLM
rounds that costs at your `--workers` setting — before spending anything.

| flag | what it does |
|---|---|
| `--stages all\|header\|improve` | how far to run |
| `--limit N` | improve at most N *real* functions (library/stub code is filtered out before counting) |
| `--workers N` | concurrent LLM calls per call-graph level. Use `1` for a local endpoint that serves requests serially |
| `--provider local\|gemini` | which LLM to use; `--model` overrides the per-provider default |
| `--output-dir DIR` | workspace location (default `./workspace/<binary>`) |
| `--json FILE` | write the run summary as JSON |
| `--dry-run`, `-q` | plan without calling an LLM; quiet output |

Output lands in `workspace/<binary>/`:

```
raw/          ghidrecomp's decompilation
improved/     one .cpp per improved function
project.h     synthesized types
symbols.json  original name -> recovered signature
runs.json     provenance: which model produced this
```

## Measuring changes

`eval/` scores output against known-good originals so prompt/model changes can
be judged by a number rather than by eye. See [eval/README.md](eval/README.md).

```bash
python -m eval.run_eval --candidates workspace          # score what you just ran
pytest                                                  # unit + similarity tests
```

## Project structure

* `src/cpp_re_agent/` — the package
  * `cli.py` — the command-line entry point
  * `pipeline.py` — stage orchestration (batch improve, synthesis, refine)
  * `decompiler.py` — `ghidrecomp` wrapper
  * `ai_improver.py` / `contextual_improver.py` — the two LLM passes
  * `callgraph.py` / `symbol_map.py` — ordering and signature propagation
  * `scanner.py` — Tree-sitter parsing, function identity and name resolution
  * `knowledge_graph.py` — type merging into `project.h`
* `eval/` — measurement harness and corpus
* `tests/` — pytest suite
* `examples/` — sample sources/binaries
* `workspace/` — pipeline output
