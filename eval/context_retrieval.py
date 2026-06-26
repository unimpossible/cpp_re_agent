"""
Context-retrieval experiment (TODO #5 decision gate).

The question: does pulling only *relevant* types/callees into the improver prompt
beat dumping the whole project.h every time? This module holds the improver
prompt FIXED and varies ONLY the context-construction strategy, so any score
difference is attributable to context, not the prompt.

Strategies (no embeddings yet — this phase establishes the oracle):
  - c0_none     : empty context (floor).
  - c1_full_dump: every project type + every sibling signature (current
                  "dump project.h" behavior), padded with K synthetic distractor
                  types to simulate a large binary's project.h.
  - c2_static   : ORACLE relevance — only the types the function references and
                  the signatures of the functions it actually calls (perfect
                  static knowledge; distractor-immune by construction).

The headline is the distractor sweep: as K grows, does c1 degrade (distraction)
while c2 stays flat? c2 is the upper bound on what *any* retriever could buy, so
if c2 does not beat c1, semantic retrieval (strictly noisier) is not worth
building. If c2 shows headroom, the next phase adds a c3_semantic strategy whose
job is to recover most of the c2 gain without the call graph.

Context blocks mirror ai_improver's production section headers exactly, so the
prompt the model sees here is the same shape it sees in the real pipeline.
"""
import random
import re
from dataclasses import dataclass
from typing import Dict, List

from cpp_re_agent import scanner, symbol_map

from . import roundtrip
from .corpus import Program
from .dataset import FunctionExample


@dataclass
class ProgramTypes:
    """A program's reusable context universe, extracted from its ORIGINAL source."""
    types: Dict[str, str]   # type name -> full struct/class definition
    sigs: Dict[str, str]    # normalized function name -> signature (no body)


def program_types(program: Program) -> ProgramTypes:
    types: Dict[str, str] = {}
    sigs: Dict[str, str] = {}
    for item in scanner.scan_code(program.source):
        if item.kind in ("struct", "class"):
            types[item.name] = item.body
        elif item.kind == "function":
            res = symbol_map.extract_signature(item.body)
            if res:
                name, sig = res
                sigs[roundtrip._normalize_name(name)] = sig
    return ProgramTypes(types, sigs)


def function_deps(body: str) -> List[str]:
    """Normalized names of the project functions this body calls."""
    for item in scanner.scan_code(body):
        if item.kind == "function":
            return [roundtrip._normalize_name(d) for d in (item.dependencies or [])]
    return []


def _referenced(type_names, body: str) -> List[str]:
    """Type names that appear as a whole-word token in `body`."""
    return [n for n in type_names if re.search(rf"\b{re.escape(n)}\b", body)]


# --- context block formatting (mirrors ai_improver's section headers) ----------

def _types_block(defs: List[str]) -> str:
    if not defs:
        return ""
    return "### Project Types (project.h)\n```cpp\n" + "\n\n".join(defs) + "\n```\n\n"


def _sigs_block(sigs: List[str]) -> str:
    if not sigs:
        return ""
    lines = "\n".join(s if s.rstrip().endswith(";") else f"{s};" for s in sigs)
    return ("### Improved Callee Signatures (use these names/types)\n```cpp\n"
            + lines + "\n```\n\n")


# --- the three strategies ------------------------------------------------------

def ctx_none(pt: ProgramTypes, ex: FunctionExample) -> str:
    return ""


def ctx_full_dump(pt: ProgramTypes, ex: FunctionExample,
                  distractor_types: List[str], distractor_sigs: List[str]) -> str:
    """Every project type + every sibling signature, padded with distractors."""
    self_norm = roundtrip._normalize_name(ex.name)
    type_defs = list(pt.types.values()) + list(distractor_types)
    sigs = [s for n, s in pt.sigs.items() if n != self_norm] + list(distractor_sigs)
    return _types_block(type_defs) + _sigs_block(sigs)


def ctx_static_deps(pt: ProgramTypes, ex: FunctionExample) -> str:
    """ORACLE: only types referenced by, and signatures of functions called by,
    this function (derived from its ORIGINAL body = perfect relevance)."""
    self_norm = roundtrip._normalize_name(ex.name)
    type_defs = [pt.types[n] for n in _referenced(pt.types.keys(), ex.original)]
    deps = function_deps(ex.original)
    callee_sigs = [pt.sigs[d] for d in deps if d in pt.sigs and d != self_norm]
    return _types_block(type_defs) + _sigs_block(callee_sigs)


# --- synthetic distractors (deterministic by seed) -----------------------------

_NOUNS = [
    "Buffer", "Session", "Packet", "Node", "Cache", "Handle", "Record", "Frame",
    "Token", "Region", "Channel", "Worker", "Slot", "Entry", "Header", "Cursor",
    "Pool", "Stream", "Vertex", "Pixel", "Segment", "Bucket", "Descriptor",
    "Context", "Tracker", "Sample", "Window", "Block", "Chunk", "Mapping",
    "Registry", "Queue", "Vector3", "Matrix", "Glyph", "Span", "Arena", "Ledger",
]
_FIELDS = [
    "count", "offset", "length", "flags", "id", "capacity", "status", "timestamp",
    "width", "height", "weight", "value", "next_index", "prev_index", "hash",
    "version", "kind", "mask", "size", "limit", "head", "tail", "rate", "seed",
]
_PRIMS = ["int", "unsigned int", "long", "double", "float", "short", "bool", "char"]
_VERBS = ["compute", "update", "reset", "merge", "scan", "encode", "decode",
          "flush", "rotate", "expand", "shrink", "lookup", "advance", "commit"]


def synth_distractor_types(n: int, seed: int = 0) -> List[str]:
    """`n` unique, plausible-but-irrelevant struct definitions."""
    rng = random.Random(seed * 131 + 7)
    out: List[str] = []
    for i in range(n):
        name = f"{_NOUNS[i % len(_NOUNS)]}{i}"
        fields, used = [], set()
        for _ in range(rng.randint(3, 6)):
            fname = rng.choice(_FIELDS)
            while fname in used:
                fname = f"{rng.choice(_FIELDS)}{rng.randint(0, 9)}"
            used.add(fname)
            t = rng.choice(_PRIMS)
            r = rng.random()
            if r < 0.15:
                fields.append(f"    {t}* {fname};")
            elif r < 0.25:
                fields.append(f"    {t} {fname}[{rng.choice([4, 8, 16, 32])}];")
            else:
                fields.append(f"    {t} {fname};")
        out.append(f"struct {name} {{\n" + "\n".join(fields) + "\n};")
    return out


def synth_distractor_sigs(n: int, seed: int = 0) -> List[str]:
    """`n` unique, irrelevant free-function signatures."""
    rng = random.Random(seed * 977 + 13)
    out: List[str] = []
    for i in range(n):
        name = f"{rng.choice(_VERBS)}_{_NOUNS[i % len(_NOUNS)].lower()}{i}"
        params = ", ".join(f"{rng.choice(_PRIMS)} a{j}"
                           for j in range(rng.randint(1, 3)))
        out.append(f"{rng.choice(_PRIMS)} {name}({params});")
    return out
