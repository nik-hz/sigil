"""Go language adapter using tree-sitter."""

from __future__ import annotations

from pathlib import Path

from sigil.format import compute_hash, parse_sigil_line
from sigil.languages.base import FunctionRecord, ModuleRecord, treesitter_parse_module, treesitter_write_sigils

_parser = None


def _get_parser():
    global _parser
    if _parser is None:
        import tree_sitter_go as tsgo
        from tree_sitter import Language, Parser
        _parser = Parser(Language(tsgo.language()))
    return _parser


def _walk_functions(node, rel_path: str, source_bytes: bytes, source_lines: list[str],
                    records: list[FunctionRecord]) -> None:
    """Walk the Go AST for function and method declarations."""
    for child in node.children:
        if child.type == "function_declaration":
            name_node = child.child_by_field_name("name")
            if not name_node:
                continue
            func_name = name_node.text.decode()
            _record_function(child, func_name, rel_path, source_lines, records)

        elif child.type == "method_declaration":
            name_node = child.child_by_field_name("name")
            if not name_node:
                continue
            method_name = name_node.text.decode()
            # Extract receiver type.
            receiver_node = child.child_by_field_name("receiver")
            receiver_type = ""
            if receiver_node:
                # Walk receiver parameter list to find the type.
                for param in receiver_node.children:
                    if param.type == "parameter_declaration":
                        type_node = param.child_by_field_name("type")
                        if type_node:
                            # Handle pointer receivers: *Type → Type
                            rt = type_node.text.decode()
                            rt = rt.lstrip("*")
                            receiver_type = rt
                            break
            if receiver_type:
                func_name = f"({receiver_type}).{method_name}"
            else:
                func_name = method_name
            _record_function(child, func_name, rel_path, source_lines, records)


def _collect_calls(node) -> list[str]:
    """Collect function call names from a Go tree-sitter node."""
    calls: list[str] = []
    _walk_calls(node, calls)
    return sorted(set(calls))


def _walk_calls(node, calls: list[str]) -> None:
    if node.type == "call_expression":
        func_node = node.child_by_field_name("function")
        if func_node:
            name = _go_call_name(func_node)
            if name:
                calls.append(name)
    for child in node.children:
        _walk_calls(child, calls)


def _go_call_name(node) -> str | None:
    if node.type == "identifier":
        return node.text.decode()
    if node.type == "selector_expression":
        field = node.child_by_field_name("field")
        operand = node.child_by_field_name("operand")
        if field and operand:
            parent = _go_call_name(operand)
            if parent:
                return f"{parent}.{field.text.decode()}"
            return field.text.decode()
    return None


def _record_function(node, func_name: str, rel_path: str,
                     source_lines: list[str], records: list[FunctionRecord]) -> None:
    symbol_id = f"{rel_path}::{func_name}"
    body_text = node.text.decode()
    h = compute_hash(body_text)

    start_line = node.start_point[0]  # 0-based
    existing = None
    if start_line > 0:
        existing = parse_sigil_line(source_lines[start_line - 1])

    line_range = (start_line + 1, node.end_point[0] + 1)
    calls = _collect_calls(node)
    records.append(FunctionRecord(symbol_id, h, line_range, existing, calls=calls))


class GoAdapter:
    comment_prefix: str = "//"
    extensions: tuple[str, ...] = (".go",)

    def parse(self, path: Path, rel_path: str) -> list[FunctionRecord]:
        source = path.read_text(encoding="utf-8")
        source_bytes = source.encode("utf-8")
        source_lines = source.split("\n")
        parser = _get_parser()
        tree = parser.parse(source_bytes)
        records: list[FunctionRecord] = []
        _walk_functions(tree.root_node, rel_path, source_bytes, source_lines, records)
        return records

    def parse_module(self, path: Path, rel_path: str) -> ModuleRecord:
        parser = _get_parser()
        return treesitter_parse_module(
            path, rel_path, "go", parser,
            export_node_types=["function_declaration", "method_declaration", "type_declaration"],
            import_node_types=["import_declaration"],
        )

    def write_sigils(self, path: Path, rel_path: str, sigils: dict[str, str]) -> None:
        treesitter_write_sigils(path, sigils, self.comment_prefix, self.parse, rel_path)
