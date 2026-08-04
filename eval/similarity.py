"""Similarity metrics between original C++ source and decompiled/improved output.

The goal is a cheap, deterministic, offline score that tracks "how close did
the pipeline get to the original source", so that improvement ideas (prompt
changes, models, post-processing) can be A/B tested without eyeballing diffs.

Four metrics, each in [0, 1]:

- structure:            difflib ratio over the token stream with identifiers
                        and literals anonymized. Measures control-flow /
                        statement-shape fidelity independent of naming.
- ast_shape:            cosine similarity of AST node-type histograms.
                        A coarse, order-insensitive structural signal.
- identifier_recovery:  fraction of the original's identifiers that reappear
                        in the candidate (case/underscore-insensitive).
                        This is the metric the AI-improvement step should move.
- literal_recovery:     fraction of the original's string/char/numeric
                        literals present in the candidate. Literals survive
                        compilation, so this mostly measures decompiler
                        fidelity and catches hallucinated rewrites.

`overall` is a weighted mean; metrics that are undefined for a given original
(e.g. no literals) are dropped and the weights renormalized.
"""

from __future__ import annotations

import difflib
import math
from collections import Counter
from dataclasses import dataclass, asdict
from typing import Iterator, Optional

from tree_sitter import Language, Node, Parser
import tree_sitter_cpp

WEIGHTS = {
    "structure": 0.35,
    "ast_shape": 0.15,
    "identifier_recovery": 0.30,
    "literal_recovery": 0.20,
}

IDENTIFIER_NODE_TYPES = {
    "identifier",
    "field_identifier",
    "type_identifier",
    "namespace_identifier",
    "statement_identifier",
}

# Runtime/library names that appear in virtually any C++ program (original and
# decompiled alike). Excluding them stops them from inflating recovery scores.
IDENTIFIER_STOPLIST = {
    "std", "cout", "cin", "cerr", "endl", "string", "vector", "basic_string",
    "basic_ostream", "basic_istream", "operator", "printf", "scanf", "strlen",
    "strcmp", "strcpy", "memcpy", "memset", "malloc", "free", "main", "argc",
    "argv", "size_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t", "int8_t",
    "int16_t", "int32_t", "int64_t",
}

_parser: Optional[Parser] = None


def get_parser() -> Parser:
    global _parser
    if _parser is None:
        _parser = Parser(Language(tree_sitter_cpp.language()))
    return _parser


def _walk(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        # Reverse keeps preorder left-to-right, which structure() relies on.
        stack.extend(reversed(node.children))


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _normalize_number(text: str) -> str:
    """Map 0x2a, 42U, 42l -> '42' so hex constants from the decompiler match."""
    stripped = text.rstrip("uUlLfF")
    try:
        return str(int(stripped, 0))
    except ValueError:
        try:
            return str(float(stripped))
        except ValueError:
            return text


def _normalize_identifier(text: str) -> str:
    """Case/underscore-insensitive form: unit_price ~ unitPrice ~ UnitPrice."""
    return text.replace("_", "").lower()


@dataclass
class ParsedCode:
    token_stream: list[str]          # anonymized leaf tokens, in order
    node_histogram: Counter          # named AST node types
    identifiers: set[str]            # normalized identifiers (stoplist removed)
    literals: set[str]               # normalized string/char/number literals


def parse_code(code: str) -> ParsedCode:
    source = code.encode("utf-8")
    tree = get_parser().parse(source)

    tokens: list[str] = []
    histogram: Counter = Counter()
    identifiers: set[str] = set()
    literals: set[str] = set()

    for node in _walk(tree.root_node):
        if node.type == "comment":
            continue
        if node.is_named:
            histogram[node.type] += 1

        if node.child_count == 0 and node.start_byte < node.end_byte:
            if node.type in IDENTIFIER_NODE_TYPES:
                tokens.append("ID")
                name = _text(node, source)
                if name not in IDENTIFIER_STOPLIST:
                    identifiers.add(_normalize_identifier(name))
            elif node.type == "number_literal":
                tokens.append("NUM")
                literals.add(_normalize_number(_text(node, source)))
            elif node.type in ("string_content", "character"):
                tokens.append("STR")
                literals.add(_text(node, source))
            elif node.type == "escape_sequence":
                tokens.append("STR")
            else:
                tokens.append(node.type)

    return ParsedCode(tokens, histogram, identifiers, literals)


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b[k] for k in a.keys() & b.keys())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(
        sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def _recall(original: set[str], candidate: set[str]) -> Optional[float]:
    if not original:
        return None
    return len(original & candidate) / len(original)


@dataclass
class SimilarityReport:
    structure: float
    ast_shape: float
    identifier_recovery: Optional[float]
    literal_recovery: Optional[float]
    overall: float
    identifiers_total: int = 0
    identifiers_recovered: int = 0
    literals_total: int = 0
    literals_recovered: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def compare(original: str, candidate: str) -> SimilarityReport:
    """Score `candidate` (decompiled or AI-improved code) against `original`."""
    if not original.strip() or not candidate.strip():
        return SimilarityReport(0.0, 0.0, None, None, 0.0)

    orig = parse_code(original)
    cand = parse_code(candidate)

    scores = {
        "structure": difflib.SequenceMatcher(
            None, orig.token_stream, cand.token_stream, autojunk=False).ratio(),
        "ast_shape": _cosine(orig.node_histogram, cand.node_histogram),
        "identifier_recovery": _recall(orig.identifiers, cand.identifiers),
        "literal_recovery": _recall(orig.literals, cand.literals),
    }

    defined = {k: v for k, v in scores.items() if v is not None}
    total_weight = sum(WEIGHTS[k] for k in defined)
    overall = (sum(WEIGHTS[k] * v for k, v in defined.items()) / total_weight
               if total_weight else 0.0)

    return SimilarityReport(
        structure=scores["structure"],
        ast_shape=scores["ast_shape"],
        identifier_recovery=scores["identifier_recovery"],
        literal_recovery=scores["literal_recovery"],
        overall=overall,
        identifiers_total=len(orig.identifiers),
        identifiers_recovered=len(orig.identifiers & cand.identifiers),
        literals_total=len(orig.literals),
        literals_recovered=len(orig.literals & cand.literals),
    )
