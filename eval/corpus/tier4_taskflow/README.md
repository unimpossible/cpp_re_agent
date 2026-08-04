# tier4_taskflow — multi-file corpus program

A deterministic task scheduler split across 5 headers and 6 translation units.
It exists because every other corpus program is a single `.cpp`, which lets the
harness get away with things a real binary does not.

## Why this shape

| Property | Where it comes from | What it exercises |
|---|---|---|
| Cross-TU calls | `main.cpp` → `workload` → `graph` → `task` | The decompiled callgraph spans translation units, so callee-signature propagation has to work across files that no longer exist as files. |
| Shared headers | `task.h` types used by all 4 other TUs | `project.h` synthesis is load-bearing: the same struct is referenced from several functions, and dedup actually matters. |
| 7-level dependency graph | `build_workload()` | Stage 1's leaves-first ordering and level-parallel batching have real depth to work with, not a flat list. |
| Namespaced classes with private members | `taskflow::Scheduler`, `DependencyGraph` | `this`-pointer recovery, member offsets, and methods that Ghidra emits as free functions taking a struct pointer. |
| `enum class` | `Priority`, `State` | Recovering named constants from integer literals. |
| STL (`map`, `vector`, `sort`, `ostringstream`) | throughout | Template instantiations dominate the function count — exactly the library noise `should_improve` must filter. |
| Recursive DFS | `DependencyGraph::visit` | A genuine self-call, which must not be mistaken for a PLT forwarding thunk. |
| **Same name, different class** | `Scheduler::reset` vs `MetricsCollector::reset`; `DependencyGraph::size` vs `MetricsCollector::size` | ghidrecomp's file stem is just `reset` / `size`, so anything matching on the bare name conflates them. The `-O0` build has **8** functions stemmed `size` once STL instantiations are counted. |
| **Overload sets** | `describe(int)`, `describe(const RunRecord&)`, `describe(const Summary&)`; `MetricsCollector::reset()` vs `reset(int)` | Three functions, one name. Arity separates one; the other two differ only by parameter type. |

## Determinism

No clock, no RNG seed from the environment, no input. Timing is simulated by a
seeded LCG (`Lcg`, seed `20240517`), so the binary prints identical output on
every run and the eval is reproducible. Expected output starts:

```
tasks=12 cost=84 roots=2 depth=7
level 0: 1 2
```

## Function identity

Because this program deliberately reuses names, anything pairing a decompiled
function with its original must key on `scanner.FunctionKey` — enclosing class
+ name + arity + parameter types — not the bare name. `FunctionKey.match_key()`
is namespace-insensitive on purpose: the decompiled side says
`taskflow::MetricsCollector::reset` while the original, written inside
`namespace taskflow {}`, says `MetricsCollector::reset`.

Where two functions still cannot be told apart, the harness **drops the pair**
rather than guessing. A missing sample is a coverage gap; a wrong pair scores
recovered code against source it was never compiled from.

## Building

Compiles clean under `g++ -std=c++17 -Wall -Wextra` at both `-O0` and `-O2`.

```bash
g++ -std=c++17 -O2 -g0 -o taskflow *.cpp   # by hand
./eval/build_binaries.sh                   # from the repo root; both opt levels
```

The manifest entry points `path` at this *directory*; `corpus.collect_sources`
picks up every `.h`/`.cpp` under it. `Program.source` concatenates them (headers
first) so struct and function recovery are scored against the whole program.
`Program.translation_units` is what gets handed to the compiler, and
`run_eval.find_cases` treats the directory as a single case.
