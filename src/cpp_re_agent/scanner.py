import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional
from pydantic import BaseModel, Field
from tree_sitter import Language, Parser
import tree_sitter_cpp

# ghidrecomp writes one file per function named "<symbol>-<address>.c", so the
# keys we index functions by carry an address suffix that call sites never do.
_ADDR_SUFFIX_RE = re.compile(r"-[0-9a-fA-F]{4,}$")


def strip_address_suffix(name: str) -> str:
    """`visit-00104000` -> `visit`. Leaves names without a suffix untouched."""
    return _ADDR_SUFFIX_RE.sub("", name)


def normalize_name(name: str) -> str:
    """
    Reduce a decompiled symbol to a comparable identifier so a call site can be
    matched to the function that defines it.

    Call sites appear as bare (or namespace-qualified) names, while the
    functions themselves are keyed by ghidrecomp's `<symbol>-<address>` file
    stem — without this, no call ever matches a known function and the callgraph
    comes out edgeless.
    """
    name = name.split("(")[0]              # drop "(int, int)" from a demangled name
    name = strip_address_suffix(name)
    name = name.split("::")[-1]            # Class::method -> method
    return re.sub(r"[^A-Za-z0-9_]", "", name).strip()


# Ghidra prefixes each decompiled function with its demangled signature in a
# comment, e.g.
#     /* taskflow::DependencyGraph::size() const */
# possibly wrapped across lines. This is the only place the class qualifier
# survives — the file stem is just "size", which 8 different functions in one
# binary can share (DependencyGraph::size plus seven STL instantiations).
_DEMANGLED_COMMENT_RE = re.compile(r"/\*(.*?)\*/", re.DOTALL)


def _split_top_level(text: str, sep: str) -> List[str]:
    """Split on `sep` only outside <>, () and [] — template args are full of commas."""
    parts, depth, buf, i = [], 0, [], 0
    while i < len(text):
        ch = text[i]
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if depth == 0 and text.startswith(sep, i):
            parts.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _param_split(params: str) -> List[str]:
    """The individual parameter declarations in a signature's argument text."""
    inner = params.strip()
    if not inner or inner == "void":
        return []
    return [p.strip() for p in _split_top_level(inner, ",") if p.strip()]


# Words that decorate a parameter type without identifying it. Dropped so the
# demangled form (`taskflow::RunRecord const&`) and the source form
# (`const RunRecord &record`) reduce to the same token.
_TYPE_NOISE = {"const", "volatile", "struct", "class", "enum", "typename",
               "unsigned", "signed", "static", "inline"}


def _param_fingerprint(param: str) -> str:
    """
    Reduce one parameter declaration to its bare type name.

    Overloads that share an arity — `describe(const RunRecord&)` versus
    `describe(const Summary&)` — can only be told apart by type, and the two
    sides of the eval spell the same type differently.
    """
    text = re.sub(r"[*&\[\]]", " ", param)
    tokens = [t for t in text.split() if t and t not in _TYPE_NOISE]
    if not tokens:
        return ""
    head = tokens[0]                     # type first, trailing param name ignored
    head = head.split("<")[0]            # vector<int> -> vector
    return head.split("::")[-1]          # taskflow::RunRecord -> RunRecord


@dataclass(frozen=True)
class FunctionKey:
    """
    Identity of a function beyond its bare name.

    `qualifier` is the namespace/class chain ("taskflow::DependencyGraph"),
    `base` the method name ("size"), `arity` the parameter count. Bare names
    collide constantly in decompiled C++ — across classes (Scheduler::reset vs
    MetricsCollector::reset) and across overloads (describe(int) vs
    describe(const Summary&)) — so matching on `base` alone silently pairs
    unrelated functions.
    """
    qualifier: str = ""
    base: str = ""
    arity: int = -1          # -1 when the signature was unavailable
    params: tuple = ()       # bare type names, for same-arity overloads

    @property
    def qualified(self) -> str:
        return f"{self.qualifier}::{self.base}" if self.qualifier else self.base

    @property
    def owner(self) -> str:
        """The immediately-enclosing class, ignoring namespaces.

        `taskflow::MetricsCollector::reset` and `MetricsCollector::reset` are
        the same method — the decompiled side carries the namespace and the
        original source (written inside `namespace taskflow {}`) does not.

        Split depth-aware: a templated qualifier like
        `std::vector<int,std::allocator<int>>` contains `::` inside its
        argument list, and a plain split would cut it into nonsense.
        """
        if not self.qualifier:
            return ""
        return _split_top_level(self.qualifier, "::")[-1]

    def match_key(self, with_params: bool = True) -> str:
        """
        Namespace-insensitive identity for pairing the same function across the
        original source and its decompilation.
        """
        sig = ",".join(self.params) if (with_params and self.params) else ""
        return f"{self.owner}::{self.base}({self.arity}:{sig})"

    @property
    def is_stdlib(self) -> bool:
        head = self.qualifier.split("::")[0] if self.qualifier else ""
        return head in ("std", "__gnu_cxx", "__cxxabiv1")

    def __str__(self) -> str:
        return f"{self.qualified}/{self.arity}" if self.arity >= 0 else self.qualified


def compact_templates(text: str) -> str:
    """
    Remove whitespace inside template argument lists.

    Ghidra's demangled comment writes `std::vector<int, std::allocator<int> >`
    while call sites in the decompiled body write
    `std::vector<int,std::allocator<int>>`. Without this they are different
    strings, so no std:: method ever matches its own call sites — and the
    internal spaces also defeat any "the name is the last space-separated
    token" return-type stripping.
    """
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch.isspace() and depth > 0:
            continue
        out.append(ch)
    return "".join(out)


def parse_signature(sig: str) -> Optional[FunctionKey]:
    """Parse a demangled signature like `ns::Cls::meth(int, char*) const`."""
    sig = compact_templates(" ".join(sig.split()))
    if not sig:
        return None

    # Find the '(' that opens the parameter list, ignoring template brackets.
    depth, open_at = 0, -1
    for i, ch in enumerate(sig):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "(" and depth == 0:
            open_at = i
            break
    params: tuple = ()
    if open_at == -1:
        name_part, arity = sig, -1
    else:
        close_at = sig.rfind(")")
        name_part = sig[:open_at]
        if close_at > open_at:
            decls = _param_split(sig[open_at + 1:close_at])
            arity = len(decls)
            params = tuple(_param_fingerprint(d) for d in decls)
        else:
            arity = -1

    # Drop a leading return type: the name is the last whitespace-separated run.
    name_part = name_part.strip().split(" ")[-1].strip()
    if not name_part:
        return None

    segments = [s for s in _split_top_level(name_part, "::") if s]
    if not segments:
        return None
    base = segments[-1]
    # Strip template args from the method itself, keep them on the qualifier so
    # vector<int>::size and vector<Task>::size stay distinct.
    base = base.split("<")[0].strip()
    qualifier = "::".join(segments[:-1]).strip()
    if not base:
        return None
    return FunctionKey(qualifier=qualifier, base=base, arity=arity, params=params)


def demangled_signature(code: str) -> Optional[str]:
    """The leading demangled-signature comment Ghidra emits, if present."""
    for match in _DEMANGLED_COMMENT_RE.finditer(code[:4000]):
        body = " ".join(match.group(1).split())
        # Ghidra also emits "WARNING: ..." comments; a signature has a '(' and
        # either a qualifier or a plain call shape, and never reads as prose.
        if body.startswith("WARNING") or "(" not in body:
            continue
        return body
    return None


def function_key(name: str, code: str = "") -> FunctionKey:
    """
    Best available identity for a decompiled function.

    Prefers the demangled comment (only source of the class qualifier and the
    parameter list), then the definition text, then the bare file-stem name.
    """
    if code:
        # Best source: Ghidra's demangled comment carries namespace, class and
        # real parameter types.
        sig = demangled_signature(code)
        if sig:
            key = parse_signature(sig)
            if key and key.base:
                return key

        # Next: the definition itself. Qualified for methods
        # (`undefined8 __thiscall taskflow::Graph::visit(...)`) and still the
        # only place a free function's parameter types appear
        # (`std::string describe(const RunRecord &record)`).
        fallback = None
        for line in _definition_lines(code):
            key = parse_signature(line)
            if not key or not key.base:
                continue
            if key.qualifier:
                return key
            if fallback is None and key.arity >= 0:
                fallback = key
        if fallback is not None:
            return fallback

    stem = normalize_name(name)
    return FunctionKey(qualifier="", base=stem, arity=-1)


def _definition_lines(code: str) -> List[str]:
    """
    Candidate signature lines from a function body: everything up to the first
    `{`, with comments and preprocessor lines dropped. Ghidra sometimes wraps a
    long signature over several lines, so they are rejoined.
    """
    stripped = re.sub(r"/\*.*?\*/", " ", code[:4000], flags=re.DOTALL)
    header = stripped.split("{", 1)[0]
    lines = []
    for raw in header.splitlines():
        line = raw.strip()
        if not line or line.startswith(("//", "#")):
            continue
        lines.append(line)
    if not lines:
        return []
    # Both the joined form (wrapped signatures) and each line on its own.
    return [" ".join(lines)] + lines


class NameIndex:
    """
    Resolves a call-site name to exactly one known function, or to nothing.

    Ambiguity is reported rather than guessed: if a bare `size` could be any of
    eight functions, resolving it to the first one inserts a wrong callgraph
    edge and feeds the wrong callee signature into a prompt. Callers get None
    and can fall back to no context, which is merely unhelpful instead of wrong.
    """

    def __init__(self, functions: Dict[str, str] | Iterable[str]):
        if isinstance(functions, dict):
            items = list(functions.items())
        else:
            items = [(n, "") for n in functions]

        self.keys: Dict[str, FunctionKey] = {}
        self._by_qualified: Dict[str, List[str]] = {}
        self._by_base: Dict[str, List[str]] = {}
        self._by_exact: Dict[str, str] = {}

        for name, code in items:
            key = function_key(name, code)
            self.keys[name] = key
            # Only the literal key is an exact match. An address-stripped alias
            # here would be first-wins, which is the guess this class exists to
            # avoid — bare names go through _by_base, which counts candidates.
            self._by_exact.setdefault(name, name)
            self._by_qualified.setdefault(key.qualified, []).append(name)
            self._by_base.setdefault(key.base, []).append(name)

    def candidates(self, callsite: str) -> List[str]:
        """Every known function a call site could refer to, most precise first."""
        exact = self._by_exact.get(callsite)
        if exact:
            return [exact]

        probe = parse_signature(callsite) or FunctionKey(base=normalize_name(callsite))

        # A qualified call site must match on its qualifier. Falling back to the
        # bare name here is what maps DependencyGraph::size onto std::vector::size.
        if probe.qualifier:
            hits = self._by_qualified.get(probe.qualified, [])
            if hits and probe.arity >= 0:
                narrowed = [n for n in hits if self.keys[n].arity in (probe.arity, -1)]
                if narrowed:
                    return narrowed
            return list(hits)

        hits = self._by_base.get(probe.base, [])
        if len(hits) > 1 and probe.arity >= 0:
            narrowed = [n for n in hits if self.keys[n].arity == probe.arity]
            if narrowed:
                return narrowed
        return list(hits)

    def resolve(self, callsite: str) -> Optional[str]:
        """The single function `callsite` names, or None if unknown/ambiguous."""
        hits = self.candidates(callsite)
        return hits[0] if len(hits) == 1 else None

    def ambiguous_names(self) -> Dict[str, List[str]]:
        """Bare names shared by more than one function — diagnostics only."""
        return {b: names for b, names in self._by_base.items() if len(names) > 1}

# Data Model for Structured Output
class CppType(BaseModel):
    name: str = Field(description="Name of the struct or class")
    kind: str = Field(description="Either 'struct', 'class', 'function', or 'declaration'")
    body: str = Field(description="The full body of the struct/class definition including fields and methods")
    dependencies: List[str] = Field(description="List of other types this type depends on", default=[])

def get_parser():
    CPP_LANGUAGE = Language(tree_sitter_cpp.language())
    parser = Parser(CPP_LANGUAGE)
    return parser

def extract_calls(node, source_code: bytes) -> List[str]:
    calls = []
    if node.type == "call_expression":
        func_node = node.child_by_field_name("function")
        if func_node:
            name = source_code[func_node.start_byte:func_node.end_byte].decode("utf-8")
            calls.append(name)
    
    for child in node.children:
        calls.extend(extract_calls(child, source_code))
    return calls

def extract_types_from_node(node, source_code: bytes) -> List[CppType]:
    types = []
    
    # Traverse errors gracefully 
    # (Tree-sitter produces ERROR nodes but keeps the rest of the tree intact)
    if node.type in ["struct_specifier", "class_specifier", "function_definition", "declaration"]:
        try:
            name_node = node.child_by_field_name("name")
            # Handling name extraction for declarations which might be nested in declarators
            if not name_node and node.type == "declaration":
                 declarator = node.child_by_field_name("declarator")
                 if declarator:
                     if declarator.type == "function_declarator":
                         name_node = declarator.child_by_field_name("declarator")
                     else:
                         name_node = declarator
            
            # For function definitions, simple name extraction might fail if it's a pointer/ref/qualified
            # naive fallback for function_definition: find the 'function_declarator' -> 'identifier'
            if not name_node and node.type == "function_definition":
                declarator = node.child_by_field_name("declarator")
                if declarator:
                    # Often nested: function_declarator -> identifier
                    while declarator and declarator.type in ["function_declarator", "pointer_declarator", "reference_declarator"]:
                        child = declarator.child_by_field_name("declarator")
                        if not child: 
                            # If no nested declarator, check for identifier directly in children
                            for c in declarator.children:
                                if c.type in ["identifier", "field_identifier", "qualified_identifier", "destructor_name"]:
                                    name_node = c
                                    break
                            break
                        declarator = child
                    if not name_node:
                        name_node = declarator

            if name_node:
                name = source_code[name_node.start_byte:name_node.end_byte].decode("utf-8")
                calls = []

                if "struct" in node.type:
                    kind = "struct"
                elif "class" in node.type:
                    kind = "class"
                elif "function" in node.type:
                    kind = "function"
                    # NEW: Extract calls
                    calls = list(set(extract_calls(node, source_code)))
                else:
                    kind = "declaration"
                
                body = source_code[node.start_byte:node.end_byte].decode("utf-8")
                
                types.append(CppType(name=name, kind=kind, body=body, dependencies=calls))
        except Exception as e:
            print(f"Error parsing node {node.type}: {e}")

    for child in node.children:
        types.extend(extract_types_from_node(child, source_code))
        
    return types

def is_valid_cpp(code: str) -> bool:
    """
    Returns True if `code` parses as C++ without tree-sitter ERROR nodes.

    Used as a cheap validation gate on LLM output: it won't catch semantic
    bugs, but it reliably rejects truncated functions, unbalanced braces, and
    prose that leaked out of a code fence.
    """
    if not code or not code.strip():
        return False

    parser = get_parser()
    root = parser.parse(code.encode("utf-8")).root_node

    has_error = getattr(root, "has_error", None)
    if callable(has_error):
        has_error = has_error()
    return not bool(has_error)


# Node types that introduce a branch, i.e. add to cyclomatic complexity.
# `&&`/`||` are the anonymous operator tokens inside a binary_expression and
# show up as their own children when walking node.children.
_BRANCH_NODES = {
    "if_statement", "for_statement", "while_statement", "do_statement",
    "case_statement", "conditional_expression", "&&", "||",
}


def complexity_metrics(code: str) -> dict:
    """
    Cheap structural metrics from the tree-sitter AST. Used to estimate how
    much an LLM improvement pass is worth (see `ai_improver.score_function`).

    Returns a dict with:
      cyclomatic: 1 + number of branch points (if/for/while/case/&&/||).
      calls:      number of call expressions.
      max_depth:  maximum AST nesting depth (proxy for control-flow nesting).
    """
    parser = get_parser()
    root = parser.parse(code.encode("utf-8")).root_node

    metrics = {"cyclomatic": 1, "calls": 0, "max_depth": 0}

    # Iterative walk to avoid Python recursion limits on deep decompiled trees.
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if node.type in _BRANCH_NODES:
            metrics["cyclomatic"] += 1
        elif node.type == "call_expression":
            metrics["calls"] += 1
        if depth > metrics["max_depth"]:
            metrics["max_depth"] = depth
        for child in node.children:
            stack.append((child, depth + 1))

    return metrics


def scan_code(code: str, provider="ignored") -> List[CppType]:
    """
    Extracts struct/class definitions from C++ code using Tree-Sitter.
    Ignores 'provider' argument to maintain signature compatibility.
    """
    parser = get_parser()
    source_bytes = code.encode("utf-8")
    tree = parser.parse(source_bytes)
    
    return extract_types_from_node(tree.root_node, source_bytes)
