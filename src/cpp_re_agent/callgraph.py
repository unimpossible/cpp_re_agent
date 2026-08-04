from typing import Dict, List, Set

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
