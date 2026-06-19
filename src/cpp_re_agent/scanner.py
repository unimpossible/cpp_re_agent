import os
from typing import List
from pydantic import BaseModel, Field
from tree_sitter import Language, Parser
import tree_sitter_cpp

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
