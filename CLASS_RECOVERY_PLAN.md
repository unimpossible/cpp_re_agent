# Class recovery for project.h synthesis: plan

> Branch: `feature/thisptr`. Written 2026-08-04/05. Execute against this
> file — it's the source of truth for the work, not the chat that produced
> it.

## Context

`get_config_value_at_percentage.cpp` in `tier4_taskflow-O0-stripped` is
really `taskflow::MetricsCollector::percentile(int) const`, but nothing in
the pipeline currently groups it with its sibling methods. Investigating
whether vtables could have told us that led to a concrete finding (via
`pyelftools` against the unstripped ground-truth binary): `MetricsCollector`
has **no vtable and no RTTI** — it's non-polymorphic, so vtable-walking is a
dead end for *this* class. That raised the real question this plan answers:
under what compilation conditions does vtable/RTTI-based recovery actually
work, and what else has to carry the weight when it doesn't?

Today, `pipeline.py` runs Stage 1 (`batch_improve`, leaves-first per-function
LLM improvement) then Stage 2 (`synthesize_header` → `run_contextual`) then
Stage 3 (`run_naming`). `synthesize_header` only ever feeds
`knowledge_graph.consolidate_definitions` loose struct/function fragments —
nothing groups functions into probable classes first. Goal: give
`synthesize_header` that grouping, computed from signals that actually
survive stripping, validated across compiler/flag combinations we don't
currently build — rather than assumed from one GCC `-O0` binary.

## How vtables actually get compiled (why this matters for the rig)

A class gets a vtable only when it (or a base it inherits from) declares at
least one `virtual` member — including just a virtual destructor. The
compiler then:

- adds a hidden vptr as (usually) the class's first word,
- emits one vtable per class in the hierarchy that needs its own dispatch
  (a static array of function pointers, one per virtual method — pure
  virtuals point at `__cxa_pure_virtual`/similar),
- and, if RTTI is enabled (**the default** — GCC/Clang need `-fno-rtti` to
  turn it off, MSVC needs `/GR-`), also emits a `type_info` object per
  polymorphic class, referenced from slot 0 of its vtable.

That `type_info` object holds a pointer to the class's **name as a raw
string constant** (`"16MetricsCollector"`-style on Itanium; a decorated
`".?AVMetricsCollector@@"`-style string in MSVC's `TypeDescriptor`). This is
the detail worth building the rig around: `strip --strip-all` deletes the
*symbol table*, not arbitrary `.rodata`/`.rdata` content — so on a
polymorphic class built with RTTI on (the common case), **the class name
survives stripping as plain string data**, findable by walking vtable-shaped
pointer arrays to their first slot's `type_info` and reading the string,
with no symbol table involved at all. `MetricsCollector` gave a false
negative on this only because it isn't polymorphic; a class with one virtual
method would not have.

Multiple inheritance adds a second vtable-with-thunks per additional base;
virtual inheritance (diamonds) adds VTT/virtual-base-table machinery. Those
are progressively rarer in real code but change the byte pattern the
structural scanner has to recognize, so the rig should exercise them at
least once each rather than assuming single inheritance generalizes.

None of this exists for a non-polymorphic class (`MetricsCollector`'s
actual case) — that's the regime the non-vtable heuristics have to cover
instead.

## Step 1: build the corpus-generation rig (do this first)

Everything downstream is heuristics tuned against a corpus, and the current
one (`eval/build_binaries.sh`) only produces **g++, Linux ELF, x86-64,
`-O0`/`-O2`, static exe + `.so`** — it has no polymorphic classes at all, so
there is nothing in the repo today that would even exercise a vtable/RTTI
classifier. Fix the corpus before writing classifiers against it.

**New corpus sources** (alongside the existing tiered corpus), each small
and self-contained like the existing tiers, covering the shapes above:
- non-polymorphic class with sibling methods sharing private state (the
  `MetricsCollector` shape — already covered by `tier4_taskflow`, keep as
  the baseline case).
- single-inheritance polymorphic class: base with virtual methods + virtual
  destructor, one or two overriding derived classes.
- abstract interface (pure virtuals only) + concrete implementations.
- multiple inheritance across two polymorphic bases (exercises thunks).
- one class compiled both with and without RTTI, to measure what the
  structural scanner loses when `-fno-rtti`/`/GR-` is set.

**Build matrix** (extend `eval/build_binaries.sh`'s `build()` rather than
duplicate it — it's already parameterized by case/opt/sources):
- optimization: `-O0` (default/baseline) and `-Os` (explicitly requested —
  size-optimized code reorders/merges functions differently than either O0
  or O2 and is common in real shipped binaries) as the two required levels;
  keep `-O2` since it's already there and useful for stress-testing.
- RTTI: default vs `-fno-rtti` (GCC/Clang), so the vtable/RTTI classifier's
  degradation is measured, not assumed.
- linkage: keep the existing static-exe + `.so` split (already proved
  `.dynsym` survives `strip --strip-all` on a `.so` — 801 exported mangled
  symbols on `libtier4_taskflow-O0.so-stripped`, confirmed).
- compiler: `g++` and `clang++` now (the script already takes `CXX=` as an
  override — just needs a driver that runs it twice and tags output
  accordingly, e.g. `bin/<case>-clang-O0`).
- **MSVC/PE**: once available, a second, Windows-native build script
  (PowerShell, invoked from a Developer Command Prompt / located via
  `vswhere`) producing the same case set through `cl.exe`, with `/GR` and
  `/GR-` variants and PDB stripped (release-mode default — MSVC binaries
  don't need an explicit strip step the way ELF does, since the COFF symbol
  table + debug info live in the separate PDB and just aren't shipped).
  Output lands in the same `bin/` naming scheme so the eval harness doesn't
  need to know which toolchain produced a given binary.
- every combination built as an **unstripped/stripped pair**, same as
  today, so ground truth stays available for scoring.

This is the actual first milestone — nothing below should be built against
assumptions the current single-binary corpus can't check.

## Signals / classifiers

Kept as independent, individually-gated detectors rather than one scoring
function — matches the codebase's existing "leave it out rather than guess"
posture (`callgraph.py`'s edge-resolution comment) and means a weak binary
regime degrades one detector instead of corrupting a combined score.

**Tier 1 — near-ground-truth, used whenever present:**
1. **Symbol-table reader.** ELF `.symtab`/`.dynsym` via `pyelftools`
   (already an available dependency), PE export table for the MSVC side.
   Demangles into the same `scanner.FunctionKey`/`parse_signature` shape
   Ghidra's own demangled-comment path already produces, so a symtab hit and
   a Ghidra-comment hit are handled identically downstream.
2. **Vtable/RTTI structural scan.** Walks candidate vtable-shaped pointer
   arrays in `.data.rel.ro`/`.rodata` (ELF) or `.rdata` (PE), follows slot 0
   to the `type_info`/`TypeDescriptor`, and reads the embedded name string —
   works with **zero symbol-table entries**, as explained above. Only fires
   for polymorphic classes with RTTI enabled; the rig's RTTI-off variant is
   exactly what measures how often that is.

**Tier 2 — weak, only used in combination, never alone:**
3. **Implicit-`this` shape.** AST check (tree-sitter, matches the existing
   `scanner`-based style): a function's first parameter is only ever
   dereferenced/passed opaquely, never treated as a plain integer, and is
   forwarded unchanged into other candidate functions.
4. **Constructor/destructor fingerprint.** Zero-init of a fixed-size block
   followed by calls into other project functions on that same block — the
   `MetricsCollector::MetricsCollector` shape (`memset` + a vector-ctor-shaped
   call). Its callers (via `callgraph.callers_of`) show what embeds the
   object.
5. **Call-graph density, address-adjacency as a tiebreak only.** Given
   *pushback on address adjacency as a signal* — it's demoted: raw
   proximity in the address space is not used to form a cluster by itself
   (too easily broken by `-O2`/`-Os` reordering, LTO, identical-code-folding
   merging unrelated same-body methods, and TU interleaving). It's kept only
   as a tiebreak between two already-call-graph-linked this-shaped
   candidates, never as the reason two functions get grouped.

A cluster is only formed when a Tier 1 signal resolves it directly, or when
**at least two** Tier 2 signals agree — no single weak signal clusters
anything on its own.

## Where this lives: inside `synthesize_header`, not a new stage

Per feedback, this does **not** become a new pipeline stage or CLI stage
(`STAGES` in `cli.py` stays as-is). `pipeline.synthesize_header` (currently:
read `improved/`, concatenate struct/class fragments via
`_collect_definitions`, call `knowledge_graph.consolidate_definitions`)
grows one step before the LLM call: run the classifiers over
`workspace/raw` + the callgraph `batch_improve` already built, and prepend a
"candidate class groupings" block (function names + which signal(s)
justified the grouping — no invented class names) to `all_defs` before it
goes to `consolidate_definitions`. `classes.json` is still written to the
workspace (same load/save pattern as `symbol_map.py`/`namer.py`) purely for
debuggability/eval scoring, not as a separate user-facing phase.

`contextual_improver.refine_function` gets the same treatment `synthesize_header`
already gives `project.h`: an optional `class_context: str = ""` parameter,
inserted as a `### Probable Class Membership` block before `### Target
Function`, populated from the same `classes.json` for that function's
cluster.

`namer.py` is out of scope for this change beyond noting the synergy: a
Tier-1-resolved anchor name could seed `current[stem]` before the LLM naming
loop runs at all, and cluster siblings could extend `namer.build_prompt`'s
callee/caller lists. Fast follow, not part of this change.

## Testing / eval plan

- `tests/test_class_recovery.py` (modeled on `tests/test_namer.py`): pure
  unit tests per classifier on synthetic inputs, no LLM. Vtable/RTTI scanner
  gets its own fixture binaries (small, built by the Step 1 rig) rather than
  synthetic bytes, since the byte-level structure is the whole point.
- Ground-truth scoring: new `eval/class_metrics.py` alongside
  `eval/similarity.py`, parsing each unstripped binary's `.symtab` `_ZN...`
  symbols into `{function: owning_class}` and scoring `classes.json`
  clusters against that partition (pairwise precision/recall — simplest
  thing that answers "did we get the right functions in the right group").
- Run the metric across the **full new build matrix** (compiler × O0/Os ×
  RTTI on/off × static/shared, plus MSVC/PE once available) to produce an
  actual per-regime reliability table, replacing assumption with
  measurement — this is the deliverable that tells us which classifiers to
  trust by default and which to gate behind "only if present."

## Milestones

1. Corpus rig: new polymorphic/multiple-inheritance/interface source files
   + extended `build_binaries.sh` matrix (compiler, `-O0`/`-Os`/`-O2`, RTTI
   on/off) + Windows/MSVC build script once the toolchain is available.
2. Tier 1 classifiers (symbol-table reader, vtable/RTTI structural scanner)
   + `eval/class_metrics.py`, validated against the new corpus.
3. Tier 2 classifiers (this-shape, ctor-fingerprint, call-density-with-
   adjacency-as-tiebreak-only), validated the same way — specifically
   checking how much they degrade at `-Os` vs `-O0`.
4. Wire into `synthesize_header` (prepend grouping block to `all_defs`) and
   `contextual_improver.refine_function` (`class_context` param). No CLI/
   `STAGES` changes.
5. Re-run the eval harness end-to-end, compare `project.h` quality and
   contextual-refine output before/after using existing similarity metrics
   plus the new class-attribution metric.

## Non-goals

- No new CLI stage / `STAGES` entry.
- Address-adjacency is never a standalone clustering signal.
- `run_naming` changes beyond the noted synergy — separate change later.
