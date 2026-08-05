from typing import Dict, List, Optional, Set

from . import scanner


def build_callgraph(functions: Dict[str, str]) -> Dict[str, Set[str]]:
    """
    Builds a callgraph from a {func_name: code} mapping.

    Each node maps to the set of *project* functions it calls. Calls to
    functions not present in `functions` (stdlib, imports, thunks we don't have)
    are dropped so the graph only contains edges we can actually act on.

    `functions` is keyed by ghidrecomp's `<symbol>-<address>` file stem, which
    call sites never use — and the stem drops the class, so one binary can hold
    eight unrelated functions all stemmed `size`. Matching therefore goes
    through `scanner.NameIndex`, which resolves on the demangled
    qualifier/arity and returns nothing when a name is ambiguous. An edge we
    can't pin down is left out rather than guessed: a wrong edge reorders the
    batch and feeds the wrong callee signature into a prompt.
    """
    index = scanner.NameIndex(functions)
    graph: Dict[str, Set[str]] = {}

    for name, code in functions.items():
        deps: Set[str] = set()
        try:
            for item in scanner.scan_code(code):
                if item.kind != "function":
                    continue
                for dep in item.dependencies:
                    target = index.resolve(dep)
                    if target and target != name:
                        deps.add(target)
        except Exception as e:
            print(f"Callgraph scan warning for {name}: {e}")
        graph[name] = deps

    return graph


def topological_order(graph: Dict[str, Set[str]], priority=None) -> List[str]:
    """
    Returns function names in leaves-first order: a function appears only
    after all of its (project) callees. This lets callers be improved with
    their callees' improved signatures already available.

    Implemented as an iterative post-order DFS so it survives deep graphs
    (no Python recursion limit). Cycles are broken by skipping nodes that are
    already on the current DFS stack.

    `priority`, if given, is a callable name -> number. It only breaks ties:
    among functions with no dependency constraint between them, higher-priority
    ones are emitted earlier. The leaves-first guarantee is never violated.
    """
    if priority is None:
        def order_key(names):
            return list(names)
    else:
        def order_key(names):
            # Highest priority first; name as a stable secondary key.
            return sorted(names, key=lambda n: (-priority(n), n))

    visited: Set[str] = set()
    on_stack: Set[str] = set()
    order: List[str] = []

    for start in order_key(graph.keys()):
        if start in visited:
            continue
        stack = [(start, iter(order_key(graph.get(start, ()))))]
        on_stack.add(start)

        while stack:
            node, children = stack[-1]
            advanced = False
            for dep in children:
                if dep in visited or dep in on_stack or dep not in graph:
                    continue
                on_stack.add(dep)
                stack.append((dep, iter(order_key(graph.get(dep, ())))))
                advanced = True
                break
            if not advanced:
                stack.pop()
                on_stack.discard(node)
                if node not in visited:
                    visited.add(node)
                    order.append(node)

    return order


def callers_of(graph: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    """Reverse edges: function -> the functions that call it."""
    callers: Dict[str, Set[str]] = {n: set() for n in graph}
    for name, deps in graph.items():
        for dep in deps:
            if dep in callers:
                callers[dep].add(name)
    return callers


def reachable_from(graph: Dict[str, Set[str]], start: str) -> Set[str]:
    """Every function reachable from `start`, including itself."""
    seen, frontier = {start}, [start]
    while frontier:
        node = frontier.pop()
        for dep in graph.get(node, ()):
            if dep not in seen:
                seen.add(dep)
                frontier.append(dep)
    return seen


def roots_of(graph: Dict[str, Set[str]]) -> List[str]:
    """Functions nothing else calls."""
    incoming = callers_of(graph)
    return [n for n in graph if not incoming.get(n)]


def primary_entry(graph: Dict[str, Set[str]]) -> Optional[str]:
    """
    Best guess at an executable's entry function.

    In a stripped executable there is no `main` symbol to look for, but the
    entry still behaves like one: nothing calls it, and it reaches most of the
    program. Picking the uncalled function with the largest reachable set finds
    it without needing any symbol at all.

    Meaningless for a shared object, which has no single entry — use
    `entry_points` instead, which handles both.
    """
    roots = roots_of(graph)
    if not roots:
        return None
    return max(roots, key=lambda n: (len(reachable_from(graph, n)), n))


def entry_points(graph: Dict[str, Set[str]],
                 exported: Optional[Set[str]] = None,
                 dominance: float = 0.5) -> List[str]:
    """
    Where the program is entered from the outside — the sources for depth.

    An executable has one: `main`, reachable from nothing and reaching almost
    everything. A shared object has many, one per exported function, and no
    single root dominates. Measuring depth from only the biggest root would
    then treat most of the library's own public API as unreachable, i.e. as
    deep library noise, and skip exactly the functions a caller cares about.

    `exported` (from the binary's dynamic symbol table, which survives
    stripping) is used when given; otherwise every uncalled function is an
    entry, which is the right default for a library.
    """
    roots = roots_of(graph)
    if not roots:
        return []

    if exported:
        named = [n for n in graph if n in exported]
        if named:
            return sorted(named)

    best = max(roots, key=lambda n: (len(reachable_from(graph, n)), n))
    if len(reachable_from(graph, best)) >= dominance * len(graph):
        return [best]          # one root reaches the program: an executable
    return sorted(roots)       # no dominant root: treat every root as an entry


def call_depths(graph: Dict[str, Set[str]], start) -> Dict[str, int]:
    """
    Hops from `start` to each reachable function (sources themselves are 0).

    `start` is one name or an iterable of them; several sources are a
    multi-source BFS, which is what a shared object needs.

    Depth separates a program's own code from the library it was linked
    against far better than any naming or address heuristic once symbols are
    gone: the user's functions sit near the entry points, while the statically
    linked standard library is reached through them and piles up deeper.
    """
    starts = [start] if isinstance(start, str) else list(start)
    depths = {s: 0 for s in starts}
    frontier = list(depths)
    while frontier:
        nxt = []
        for node in frontier:
            for dep in graph.get(node, ()):
                if dep not in depths:
                    depths[dep] = depths[node] + 1
                    nxt.append(dep)
        frontier = nxt
    return depths


def levels_over_subset(graph: Dict[str, Set[str]], subset: Set[str],
                       priority=None) -> List[List[str]]:
    """
    Dependency levels over `subset` only, with edges closed transitively.

    Levels computed over the *whole* graph serialize far more than necessary
    once most functions are filtered out: the survivors land in different
    levels because skipped library functions occupy the ones between them, and
    a level of one function means no parallelism at all. Real example — a
    stripped shared object had 112 functions worth improving spread across 112
    whole-graph levels, so every LLM call ran alone.

    Two functions here are ordered only if one can actually reach the other,
    so the leaves-first guarantee is preserved while independent work is free
    to run concurrently.
    """
    members = {n for n in subset if n in graph}
    if not members:
        return []

    condensed: Dict[str, Set[str]] = {}
    # Insert in sorted order: `topological_levels` falls back to insertion
    # order when two functions are unordered, and iterating a set here would
    # make the batch order vary between runs on the same input.
    for name in sorted(members):
        # Callees inside the subset, including those reached only through
        # functions that were filtered out.
        reach = reachable_from(graph, name) - {name}
        condensed[name] = reach & members

    return topological_levels(condensed, priority)


def topological_levels(graph: Dict[str, Set[str]], priority=None) -> List[List[str]]:
    """
    Groups `graph` into dependency levels. Level 0 holds functions with no
    project callees; every function in level N calls only functions in levels
    below N. Functions *within* a level have no dependency on one another, so
    they can be improved concurrently without breaking the leaves-first
    guarantee that `topological_order` provides.

    Flattening the result yields a valid leaves-first order. Levels are derived
    from `topological_order`, so cycles are broken the same way (a back-edge to
    a not-yet-levelled node is ignored) and `priority` has the same tie-breaking
    effect, here on the order of functions inside each level.
    """
    order = topological_order(graph, priority)
    depth: Dict[str, int] = {}
    levels: List[List[str]] = []

    for node in order:
        # Callees always precede their callers in `order`, so any dep missing
        # from `depth` is a cycle back-edge that topological_order already cut.
        d = max((depth[dep] + 1 for dep in graph.get(node, ()) if dep in depth),
                default=0)
        depth[node] = d
        while len(levels) <= d:
            levels.append([])
        levels[d].append(node)

    return levels
