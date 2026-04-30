"""TypeScript/JavaScript language adapter using tree-sitter."""

from __future__ import annotations

from pathlib import Path

from sigil.format import compute_hash, parse_sigil_line
from sigil.languages.base import FunctionRecord, ModuleRecord, treesitter_parse_module, treesitter_write_sigils

# Lazy-loaded parsers.
_js_parser = None
_ts_parser = None
_tsx_parser = None


def _get_parser(suffix: str):
    """Return a tree-sitter parser for the given file extension."""
    global _js_parser, _ts_parser, _tsx_parser
    import tree_sitter_javascript as tsjs
    import tree_sitter_typescript as tsts
    from tree_sitter import Language, Parser

    if suffix in (".ts",):
        if _ts_parser is None:
            _ts_parser = Parser(Language(tsts.language_typescript()))
        return _ts_parser
    elif suffix in (".tsx",):
        if _tsx_parser is None:
            _tsx_parser = Parser(Language(tsts.language_tsx()))
        return _tsx_parser
    else:  # .js, .jsx
        if _js_parser is None:
            _js_parser = Parser(Language(tsjs.language()))
        return _js_parser


# @sig 40edfe12 | role: _walk_functions | by: claude-code-292be15c | at: 2026-04-29T19:49:38Z
def _walk_functions(node, rel_path: str, source_bytes: bytes, source_lines: list[str],
                    scope: list[str], records: list[FunctionRecord]) -> None:
    """Recursively walk the tree-sitter AST to find function definitions."""
    for child in node.children:
        if child.type == "class_declaration":
            # Get class name.
            name_node = child.child_by_field_name("name")
            class_name = name_node.text.decode() if name_node else "<anon>"
            scope.append(class_name)
            _walk_functions(child, rel_path, source_bytes, source_lines, scope, records)
            scope.pop()
            continue

        if child.type in ("function_declaration", "method_definition",
                          "function", "generator_function_declaration"):
            name_node = child.child_by_field_name("name")
            if not name_node:
                _walk_functions(child, rel_path, source_bytes, source_lines, scope, records)
                continue
            func_name = name_node.text.decode()
            _record_function(child, func_name, rel_path, source_bytes, source_lines, scope, records)
            continue

        # Handle: const foo = (...) => { ... }  or  const foo = function(...) { ... }
        if child.type in ("lexical_declaration", "variable_declaration"):
            for declarator in child.children:
                if declarator.type == "variable_declarator":
                    name_n = declarator.child_by_field_name("name")
                    value_n = declarator.child_by_field_name("value")
                    if name_n and value_n and value_n.type in ("arrow_function", "function"):
                        func_name = name_n.text.decode()
                        _record_function(child, func_name, rel_path, source_bytes,
                                         source_lines, scope, records)
            continue

        # Handle export statements wrapping function declarations.
        if child.type in ("export_statement",):
            _walk_functions(child, rel_path, source_bytes, source_lines, scope, records)
            continue

        # Recurse into class bodies, modules, namespaces, etc.
        if child.type in ("program", "statement_block", "module", "class_body"):
            _walk_functions(child, rel_path, source_bytes, source_lines, scope, records)


def _collect_calls(node) -> list[str]:
    """Recursively collect function call names from a tree-sitter node."""
    calls: list[str] = []
    _walk_calls(node, calls)
    return sorted(set(calls))


def _walk_calls(node, calls: list[str]) -> None:
    """Walk tree-sitter AST to find call_expression nodes."""
    if node.type == "call_expression":
        func_node = node.child_by_field_name("function")
        if func_node:
            name = _ts_call_name(func_node)
            if name:
                calls.append(name)
    for child in node.children:
        _walk_calls(child, calls)


def _ts_call_name(node) -> str | None:
    """Extract call name from a call_expression function node."""
    if node.type == "identifier":
        return node.text.decode()
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        obj = node.child_by_field_name("object")
        if prop and obj:
            parent = _ts_call_name(obj)
            if parent:
                return f"{parent}.{prop.text.decode()}"
            return prop.text.decode()
    return None


def _record_function(node, func_name: str, rel_path: str, source_bytes: bytes,
                     source_lines: list[str], scope: list[str],
                     records: list[FunctionRecord]) -> None:
    """Create a FunctionRecord for a function/method node."""
    symbol = ".".join([*scope, func_name])
    symbol_id = f"{rel_path}::{symbol}"

    body_text = node.text.decode()
    h = compute_hash(body_text)

    # Check line above for existing sigil.
    start_line = node.start_point[0]  # 0-based
    existing = None
    if start_line > 0:
        above = source_lines[start_line - 1]
        existing = parse_sigil_line(above)

    line_range = (start_line + 1, node.end_point[0] + 1)  # 1-based
    calls = _collect_calls(node)
    records.append(FunctionRecord(symbol_id, h, line_range, existing, calls=calls))


class TypeScriptAdapter:
    comment_prefix: str = "//"
    extensions: tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx")

    def parse(self, path: Path, rel_path: str) -> list[FunctionRecord]:
        source = path.read_text(encoding="utf-8")
        source_bytes = source.encode("utf-8")
        source_lines = source.split("\n")
        parser = _get_parser(path.suffix)
        tree = parser.parse(source_bytes)
        records: list[FunctionRecord] = []
        _walk_functions(tree.root_node, rel_path, source_bytes, source_lines, [], records)
        return records

    def parse_module(self, path: Path, rel_path: str) -> ModuleRecord:
        parser = _get_parser(path.suffix)
        lang = "typescript" if path.suffix in (".ts", ".tsx") else "javascript"
        return treesitter_parse_module(
            path, rel_path, lang, parser,
            export_node_types=["function_declaration", "class_declaration",
                               "lexical_declaration", "variable_declaration"],
            import_node_types=["import_statement"],
        )

    def write_sigils(self, path: Path, rel_path: str, sigils: dict[str, str]) -> None:
        treesitter_write_sigils(path, sigils, self.comment_prefix, self.parse, rel_path)
