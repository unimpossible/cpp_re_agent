from typing import Dict, List, Set

from . import scanner


def build_callgraph(functions: Dict[str, str]) -> Dict[str, Set[str]]:
    """
    Builds a callgraph from a {func_name: code} mapping.

    Each node maps to the set of *project* functions it calls. Calls to
    functions not present in `functions` (stdlib, imports, thunks we don't have)
    are dropped so the graph only contains edges we can actually act on.
    """
    names = set(functions.keys())
    graph: Dict[str, Set[str]] = {}

    for name, code in functions.items():
        deps: Set[str] = set()
        try:
            for item in scanner.scan_code(code):
                if item.kind != "function":
                    continue
                for dep in item.dependencies:
                    if dep in names and dep != name:
                        deps.add(dep)
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
