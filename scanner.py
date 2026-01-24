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

            if name_node:
                name = source_code[name_node.start_byte:name_node.end_byte].decode("utf-8")
                if "struct" in node.type:
                    kind = "struct"
                elif "class" in node.type:
                    kind = "class"
                elif "function" in node.type:
                    kind = "function"
                else:
                    kind = "declaration"
                body = source_code[node.start_byte:node.end_byte].decode("utf-8")
                
                # Basic dependency extraction (very naive: regex matching or deeper traversal)
                # For now, we leave dependencies empty or could perform a secondary scan
                types.append(CppType(name=name, kind=kind, body=body))
        except Exception as e:
            print(f"Error parsing node: {e}")

    for child in node.children:
        types.extend(extract_types_from_node(child, source_code))
        
    return types

def scan_code(code: str, provider="ignored") -> List[CppType]:
    """
    Extracts struct/class definitions from C++ code using Tree-Sitter.
    Ignores 'provider' argument to maintain signature compatibility.
    """
    parser = get_parser()
    source_bytes = code.encode("utf-8")
    tree = parser.parse(source_bytes)
    
    return extract_types_from_node(tree.root_node, source_bytes)
