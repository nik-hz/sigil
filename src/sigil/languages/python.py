"""Python language adapter using libcst."""

from __future__ import annotations

import os
from pathlib import Path

import libcst as cst

from sigil.format import compute_hash, parse_sigil_line
from sigil.languages.base import FunctionRecord, ModuleRecord


def _collect_calls_from_node(node: cst.CSTNode) -> list[str]:
    """Recursively collect function call names from a libcst node."""
    calls: list[str] = []
    _walk_cst_calls(node, calls)
    return sorted(set(calls))


def _walk_cst_calls(node: cst.CSTNode, calls: list[str]) -> None:
    """Walk libcst tree to find Call nodes."""
    if isinstance(node, cst.Call):
        name = _extract_call_name(node.func)
        if name:
            calls.append(name)
    for child in node.children:
        _walk_cst_calls(child, calls)


def _extract_call_name(node: cst.BaseExpression) -> str | None:
    """Extract a dotted call name from a Call func node."""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        parent = _extract_call_name(node.value)
        if parent:
            return f"{parent}.{node.attr.value}"
        return node.attr.value
    return None


class _Visitor(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (cst.metadata.PositionProvider,)

    def __init__(self, rel_path: str):
        self.rel_path = rel_path
        self.scope: list[str] = []
        self.records: list[FunctionRecord] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self.scope.append(node.name.value)

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self.scope.pop()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        symbol = ".".join([*self.scope, node.name.value])
        symbol_id = f"{self.rel_path}::{symbol}"

        stripped = node.with_changes(decorators=(), leading_lines=())
        body_code = cst.Module(body=[]).code_for_node(stripped)
        h = compute_hash(body_code)

        existing = None
        for line in node.leading_lines:
            if line.comment:
                parsed = parse_sigil_line(line.comment.value)
                if parsed:
                    existing = parsed
                    break

        pos = self.get_metadata(cst.metadata.PositionProvider, node)
        line_range = (pos.start.line, pos.end.line)

        # Collect calls within the function body.
        calls = _collect_calls_from_node(node.body)

        self.records.append(FunctionRecord(symbol_id, h, line_range, existing, calls=calls))
        self.scope.append(node.name.value)

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self.scope.pop()


class _Updater(cst.CSTTransformer):
    def __init__(self, rel_path: str, targets: dict[str, str]):
        self.rel_path = rel_path
        self.targets = targets
        self.scope: list[str] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self.scope.append(node.name.value)
        return True

    def leave_ClassDef(self, original_node, updated_node):
        self.scope.pop()
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self.scope.append(node.name.value)
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef):
        symbol = ".".join(self.scope)
        self.scope.pop()
        symbol_id = f"{self.rel_path}::{symbol}"
        new_text = self.targets.get(symbol_id)
        if new_text is None:
            return updated_node

        new_leading = []
        replaced = False
        for ll in updated_node.leading_lines:
            if ll.comment and parse_sigil_line(ll.comment.value):
                new_leading.append(ll.with_changes(comment=cst.Comment(value=new_text)))
                replaced = True
            else:
                new_leading.append(ll)
        if not replaced:
            new_leading.append(cst.EmptyLine(comment=cst.Comment(value=new_text)))
        return updated_node.with_changes(leading_lines=tuple(new_leading))


def _extract_python_module_info(module: cst.Module, source: str, rel_path: str) -> ModuleRecord:
    """Extract module-level info: imports and top-level exports."""
    imports: list[str] = []
    exports: list[str] = []

    for stmt in module.body:
        # Unwrap simple statements
        node = stmt
        if isinstance(node, cst.SimpleStatementLine):
            for item in node.body:
                if isinstance(item, cst.Import):
                    if isinstance(item.names, cst.ImportStar):
                        imports.append("*")
                    elif isinstance(item.names, (list, tuple)):
                        for alias in item.names:
                            imports.append(cst.Module(body=[]).code_for_node(alias.name).strip())
                elif isinstance(item, cst.ImportFrom):
                    mod = cst.Module(body=[]).code_for_node(item.module).strip() if item.module else ""
                    if mod:
                        imports.append(mod)
                elif isinstance(item, (cst.Assign, cst.AnnAssign)):
                    if isinstance(item, cst.Assign):
                        for target in item.targets:
                            if isinstance(target.target, cst.Name):
                                exports.append(target.target.value)
                    elif isinstance(item, cst.AnnAssign) and isinstance(item.target, cst.Name):
                        exports.append(item.target.value)
        elif isinstance(node, cst.FunctionDef):
            exports.append(node.name.value)
        elif isinstance(node, cst.ClassDef):
            exports.append(node.name.value)

    line_count = source.count("\n") + 1
    lang = "python"
    return ModuleRecord(
        file=rel_path, language=lang,
        imports=sorted(set(imports)), exports=sorted(set(exports)),
        line_count=line_count,
    )


class PythonAdapter:
    comment_prefix: str = "#"
    extensions: tuple[str, ...] = (".py",)

    def parse(self, path: Path, rel_path: str) -> list[FunctionRecord]:
        source = path.read_text(encoding="utf-8")
        module = cst.parse_module(source)
        wrapper = cst.metadata.MetadataWrapper(module)
        visitor = _Visitor(rel_path)
        wrapper.visit(visitor)
        return visitor.records

    def parse_module(self, path: Path, rel_path: str) -> ModuleRecord:
        source = path.read_text(encoding="utf-8")
        module = cst.parse_module(source)
        return _extract_python_module_info(module, source, rel_path)

    def write_sigils(self, path: Path, rel_path: str, sigils: dict[str, str]) -> None:
        if not sigils:
            return
        source = path.read_text(encoding="utf-8")
        module = cst.parse_module(source)
        new_module = module.visit(_Updater(rel_path, sigils))
        tmp = path.with_suffix(path.suffix + ".sig.tmp")
        tmp.write_text(new_module.code, encoding="utf-8")
        os.replace(tmp, path)
