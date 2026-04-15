"""Tree-sitter based C/C++ parser.

Extracts function definitions, structs, globals, macros, typedefs, and includes
from C source files. Provides the raw data for call graph building and module detection.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_c as tsc
from tree_sitter import Language, Parser, Node


C_LANGUAGE = Language(tsc.language())


class CParser:
    """Parse C source files and extract structural information."""

    def __init__(self):
        self._parser = Parser(C_LANGUAGE)

    def parse_file(self, path: Path) -> dict:
        """Parse a single C file and return extracted information.

        Returns dict with keys: functions, structs, globals, macros, typedefs,
        includes, line_count, path.
        """
        source = path.read_bytes()
        tree = self._parser.parse(source)
        root = tree.root_node

        return {
            "path": str(path),
            "line_count": source.count(b"\n") + 1,
            "functions": self._extract_functions(root, source, str(path)),
            "structs": self._extract_structs(root, source, str(path)),
            "globals": self._extract_globals(root, source, str(path)),
            "macros": self._extract_macros(root, source, str(path)),
            "typedefs": self._extract_typedefs(root, source),
            "includes": self._extract_includes(root, source),
        }

    def parse_source(self, source: bytes, filename: str = "<string>") -> dict:
        """Parse C source from bytes."""
        tree = self._parser.parse(source)
        root = tree.root_node
        return {
            "path": filename,
            "line_count": source.count(b"\n") + 1,
            "functions": self._extract_functions(root, source, filename),
            "structs": self._extract_structs(root, source, filename),
            "globals": self._extract_globals(root, source, filename),
            "macros": self._extract_macros(root, source, filename),
            "typedefs": self._extract_typedefs(root, source),
            "includes": self._extract_includes(root, source),
        }

    # ── Function extraction ──────────────────────────────────────────────

    def _extract_functions(self, root: Node, source: bytes, filepath: str) -> list[dict]:
        """Extract all function definitions, including those inside #ifdef blocks."""
        functions = []
        self._find_functions_recursive(root, source, filepath, functions)
        return functions

    def _find_functions_recursive(
        self, node: Node, source: bytes, filepath: str, out: list[dict]
    ):
        """Walk tree finding function_definition nodes at any depth."""
        for child in node.children:
            if child.type == "function_definition":
                func = self._parse_function_def(child, source, filepath)
                if func:
                    out.append(func)
            elif child.type in (
                "preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else",
                "declaration_list", "linkage_specification",
            ):
                # Recurse into preprocessor blocks and extern "C" { } blocks
                self._find_functions_recursive(child, source, filepath, out)

    def _parse_function_def(self, node: Node, source: bytes, filepath: str) -> dict | None:
        name = self._get_function_name(node)
        if not name:
            return None

        return_type = self._get_function_return_type(node, source)
        params = self._get_function_params(node, source)
        body_node = node.child_by_field_name("body")
        calls = self._extract_calls(body_node, source) if body_node else []
        local_vars = self._extract_local_vars(body_node, source) if body_node else []

        # Check for static/inline qualifiers
        is_static = False
        is_inline = False
        for child in node.children:
            if child.type == "storage_class_specifier":
                text = self._node_text(child, source)
                if text == "static":
                    is_static = True
            elif child.type == "type_qualifier" or child.type == "function_specifier":
                text = self._node_text(child, source)
                if text == "inline":
                    is_inline = True

        return {
            "name": name,
            "return_type": return_type,
            "params": params,
            "calls": calls,
            "local_vars": local_vars,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "file": filepath,
            "is_static": is_static,
            "is_inline": is_inline,
            "line_count": node.end_point[0] - node.start_point[0] + 1,
        }

    def _get_function_name(self, node: Node) -> str | None:
        """Extract function name, handling nested declarators."""
        declarator = node.child_by_field_name("declarator")
        if not declarator:
            return None
        return self._unwrap_declarator_name(declarator)

    def _unwrap_declarator_name(self, node: Node) -> str | None:
        """Recursively unwrap declarator to get the identifier name."""
        if node.type == "identifier":
            return node.text.decode("utf-8") if node.text else None
        # function_declarator → declarator field holds the name or a pointer_declarator
        decl = node.child_by_field_name("declarator")
        if decl:
            return self._unwrap_declarator_name(decl)
        # pointer_declarator wraps the actual declarator
        for child in node.children:
            if child.type in ("identifier", "function_declarator", "pointer_declarator",
                              "parenthesized_declarator"):
                result = self._unwrap_declarator_name(child)
                if result:
                    return result
        return None

    def _get_function_return_type(self, node: Node, source: bytes) -> str:
        type_node = node.child_by_field_name("type")
        if type_node:
            return self._node_text(type_node, source)
        return "unknown"

    def _get_function_params(self, node: Node, source: bytes) -> list[dict]:
        """Extract parameter list from a function definition."""
        declarator = node.child_by_field_name("declarator")
        if not declarator:
            return []

        # Find the parameter_list node
        params_node = None
        if declarator.type == "function_declarator":
            params_node = declarator.child_by_field_name("parameters")
        else:
            # Nested declarator (e.g., pointer to function)
            for child in self._walk_tree(declarator):
                if child.type == "parameter_list":
                    params_node = child
                    break

        if not params_node:
            return []

        params = []
        for child in params_node.children:
            if child.type == "parameter_declaration":
                ptype = child.child_by_field_name("type")
                pdecl = child.child_by_field_name("declarator")
                params.append({
                    "type": self._node_text(ptype, source) if ptype else "unknown",
                    "name": self._unwrap_declarator_name(pdecl) if pdecl else None,
                })
            elif child.type == "variadic_parameter":
                params.append({"type": "...", "name": None})
        return params

    # ── Call extraction ───────────────────────────────────────────────────

    def _extract_calls(self, body: Node, source: bytes) -> list[str]:
        """Extract all function calls within a function body."""
        calls = set()
        for node in self._walk_tree(body):
            if node.type == "call_expression":
                func_node = node.child_by_field_name("function")
                if func_node:
                    call_name = self._resolve_call_target(func_node, source)
                    if call_name:
                        calls.add(call_name)
        return sorted(calls)

    def _resolve_call_target(self, node: Node, source: bytes) -> str | None:
        """Resolve the target of a call expression."""
        if node.type == "identifier":
            return self._node_text(node, source)
        elif node.type == "field_expression":
            # struct->method or struct.method — return the field name
            field = node.child_by_field_name("field")
            if field:
                return self._node_text(field, source)
        elif node.type == "parenthesized_expression":
            # (*(func_ptr))(args) — try to resolve inner
            for child in node.children:
                if child.type not in ("(", ")"):
                    return self._resolve_call_target(child, source)
        elif node.type == "pointer_expression":
            for child in node.children:
                if child.type != "*":
                    return self._resolve_call_target(child, source)
        return self._node_text(node, source)

    def _extract_local_vars(self, body: Node, source: bytes) -> list[str]:
        """Extract local variable names declared in a function body (shallow)."""
        vars_ = []
        for child in body.children:
            if child.type == "declaration":
                decl = child.child_by_field_name("declarator")
                if decl:
                    name = self._unwrap_declarator_name(decl)
                    if name:
                        vars_.append(name)
        return vars_

    # ── Struct extraction ─────────────────────────────────────────────────

    def _extract_structs(self, root: Node, source: bytes, filepath: str) -> list[dict]:
        structs = []
        self._find_structs_recursive(root, source, filepath, structs)
        return structs

    def _find_structs_recursive(
        self, node: Node, source: bytes, filepath: str, out: list[dict]
    ):
        for child in node.children:
            if child.type in ("struct_specifier", "union_specifier"):
                name_node = child.child_by_field_name("name")
                body_node = child.child_by_field_name("body")
                if body_node:
                    name = self._node_text(name_node, source) if name_node else "<anonymous>"
                    fields = self._extract_struct_fields(body_node, source)
                    out.append({
                        "name": name,
                        "kind": "struct" if child.type == "struct_specifier" else "union",
                        "fields": fields,
                        "start_line": child.start_point[0] + 1,
                        "file": filepath,
                    })
            elif child.type in ("type_definition", "declaration"):
                # Check inside typedefs/declarations for struct definitions
                found = self._find_struct_in_node(child, source, filepath)
                out.extend(found)
            elif child.type in (
                "preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else",
                "declaration_list", "linkage_specification",
            ):
                self._find_structs_recursive(child, source, filepath, out)

    def _find_struct_in_node(self, node: Node, source: bytes, filepath: str) -> list[dict]:
        results = []
        for child in self._walk_tree(node):
            if child.type in ("struct_specifier", "union_specifier"):
                name_node = child.child_by_field_name("name")
                body_node = child.child_by_field_name("body")
                if body_node:
                    name = self._node_text(name_node, source) if name_node else "<anonymous>"
                    fields = self._extract_struct_fields(body_node, source)
                    results.append({
                        "name": name,
                        "kind": "struct" if child.type == "struct_specifier" else "union",
                        "fields": fields,
                        "start_line": child.start_point[0] + 1,
                        "file": filepath,
                    })
        return results

    def _extract_struct_fields(self, body: Node, source: bytes) -> list[dict]:
        fields = []
        for child in body.children:
            if child.type == "field_declaration":
                ftype = child.child_by_field_name("type")
                fdecl = child.child_by_field_name("declarator")
                fields.append({
                    "type": self._node_text(ftype, source) if ftype else "unknown",
                    "name": self._unwrap_declarator_name(fdecl) if fdecl else None,
                })
        return fields

    # ── Global variable extraction ────────────────────────────────────────

    def _extract_globals(self, root: Node, source: bytes, filepath: str) -> list[dict]:
        globals_ = []
        self._find_globals_recursive(root, source, filepath, globals_)
        return globals_

    def _find_globals_recursive(
        self, node: Node, source: bytes, filepath: str, out: list[dict]
    ):
        for node in node.children:
            if node.type in (
                "preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else",
                "declaration_list", "linkage_specification",
            ):
                self._find_globals_recursive(node, source, filepath, out)
            elif node.type == "declaration":
                self._process_global_declaration(node, source, filepath, out)

    def _process_global_declaration(
        self, node: Node, source: bytes, filepath: str, out: list[dict]
    ):
        # Skip if this is a function declaration (has parameter_list)
        if self._is_function_declaration(node):
            return
        # Skip typedefs
        has_typedef = any(
            c.type == "storage_class_specifier" and self._node_text(c, source) == "typedef"
            for c in node.children
        )
        if has_typedef:
            return

        type_node = node.child_by_field_name("type")
        type_text = self._node_text(type_node, source) if type_node else "unknown"

        is_static = any(
            c.type == "storage_class_specifier" and self._node_text(c, source) == "static"
            for c in node.children
        )
        is_const = any(
            c.type == "type_qualifier" and self._node_text(c, source) == "const"
            for c in node.children
        )
        is_extern = any(
            c.type == "storage_class_specifier" and self._node_text(c, source) == "extern"
            for c in node.children
        )

        decl = node.child_by_field_name("declarator")
        name = self._unwrap_declarator_name(decl) if decl else None
        if name:
            out.append({
                "name": name,
                "type": type_text,
                "is_static": is_static,
                "is_const": is_const,
                "is_extern": is_extern,
                "start_line": node.start_point[0] + 1,
                "file": filepath,
            })

    def _is_function_declaration(self, node: Node) -> bool:
        """Check if a declaration is a function prototype (not a variable)."""
        for child in self._walk_tree(node):
            if child.type == "function_declarator":
                return True
        return False

    # ── Macro extraction ──────────────────────────────────────────────────

    def _extract_macros(self, root: Node, source: bytes, filepath: str) -> list[dict]:
        macros = []
        self._find_macros_recursive(root, source, filepath, macros)
        return macros

    def _find_macros_recursive(
        self, node: Node, source: bytes, filepath: str, out: list[dict]
    ):
        for child in node.children:
            if child.type == "preproc_def":
                name_node = child.child_by_field_name("name")
                value_node = child.child_by_field_name("value")
                if name_node:
                    out.append({
                        "name": self._node_text(name_node, source),
                        "kind": "object",
                        "params": None,
                        "body": self._node_text(value_node, source) if value_node else "",
                        "start_line": child.start_point[0] + 1,
                        "file": filepath,
                    })
            elif child.type == "preproc_function_def":
                name_node = child.child_by_field_name("name")
                params_node = child.child_by_field_name("parameters")
                value_node = child.child_by_field_name("value")
                if name_node:
                    params = []
                    if params_node:
                        for pc in params_node.children:
                            if pc.type == "identifier":
                                params.append(self._node_text(pc, source))
                    out.append({
                        "name": self._node_text(name_node, source),
                        "kind": "function",
                        "params": params,
                        "body": self._node_text(value_node, source) if value_node else "",
                        "start_line": child.start_point[0] + 1,
                        "file": filepath,
                    })
            elif child.type in (
                "preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else",
                "declaration_list", "linkage_specification",
            ):
                self._find_macros_recursive(child, source, filepath, out)

    # ── Typedef extraction ────────────────────────────────────────────────

    def _extract_typedefs(self, root: Node, source: bytes) -> dict[str, str]:
        """Extract typedefs as a name -> underlying_type mapping."""
        typedefs = {}
        self._find_typedefs_recursive(root, source, typedefs)
        return typedefs

    def _find_typedefs_recursive(self, node: Node, source: bytes, out: dict):
        for child in node.children:
            if child.type == "type_definition":
                type_node = child.child_by_field_name("type")
                decl_node = child.child_by_field_name("declarator")
                if type_node and decl_node:
                    name = self._unwrap_declarator_name(decl_node)
                    if name:
                        out[name] = self._node_text(type_node, source)
            elif child.type in (
                "preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else",
                "declaration_list", "linkage_specification",
            ):
                self._find_typedefs_recursive(child, source, out)

    # ── Include extraction ────────────────────────────────────────────────

    def _extract_includes(self, root: Node, source: bytes) -> list[str]:
        includes = []
        for node in root.children:
            if node.type == "preproc_include":
                path_node = node.child_by_field_name("path")
                if path_node:
                    includes.append(self._node_text(path_node, source))
        return includes

    # ── Utilities ─────────────────────────────────────────────────────────

    @staticmethod
    def _node_text(node: Node, source: bytes) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _walk_tree(node: Node):
        """Yield all descendant nodes (depth-first)."""
        cursor = node.walk()
        visited = False
        while True:
            if not visited:
                yield cursor.node
                if cursor.goto_first_child():
                    continue
            if cursor.goto_next_sibling():
                visited = False
                continue
            if not cursor.goto_parent():
                break
            visited = True
