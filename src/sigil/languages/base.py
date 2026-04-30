"""LanguageAdapter protocol and shared helpers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from sigil.format import format_sigil_line, parse_sigil_line


@dataclass
class ModuleRecord:
    """Per-file summary extracted at index time."""
    file: str
    language: str
    imports: list[str]       # imported module names
    exports: list[str]       # top-level symbol names defined in this file
    line_count: int


@dataclass
class FunctionRecord:
    symbol_id: str
    body_hash: str
    line_range: tuple[int, int]
    existing_sigil: Optional[dict]
    calls: list[str] | None = None  # list of called symbol names (unresolved)


@runtime_checkable
class LanguageAdapter(Protocol):
    comment_prefix: str
    extensions: tuple[str, ...]

    def parse(self, path: Path, rel_path: str) -> list[FunctionRecord]: ...
    def write_sigils(self, path: Path, rel_path: str, sigils: dict[str, str]) -> None: ...


def treesitter_parse_module(path: Path, rel_path: str, language: str,
                            parser, export_node_types: list[str],
                            import_node_types: list[str]) -> ModuleRecord:
    """Shared module-level info extraction for tree-sitter adapters."""
    source = path.read_text(encoding="utf-8")
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    imports: list[str] = []
    exports: list[str] = []

    def _walk_top_level(node):
        for child in node.children:
            # Imports
            if child.type in import_node_types:
                _extract_import(child, imports)
            # Exports
            if child.type in export_node_types:
                name_node = child.child_by_field_name("name")
                if name_node:
                    exports.append(name_node.text.decode())
            # Recurse into export statements
            if child.type == "export_statement":
                _walk_top_level(child)

    def _extract_import(node, out: list[str]):
        src = node.child_by_field_name("source") or node.child_by_field_name("path")
        if src:
            out.append(src.text.decode().strip("'\""))
        else:
            # For Go/Rust: import path is in the text
            for c in node.children:
                if c.type in ("interpreted_string_literal", "string_literal"):
                    out.append(c.text.decode().strip("'\""))
                elif c.type == "import_spec_list":
                    for spec in c.children:
                        if spec.type == "import_spec":
                            path_node = spec.child_by_field_name("path")
                            if path_node:
                                out.append(path_node.text.decode().strip("'\""))

    _walk_top_level(root)
    line_count = source.count("\n") + 1
    return ModuleRecord(
        file=rel_path, language=language,
        imports=sorted(set(imports)), exports=sorted(set(exports)),
        line_count=line_count,
    )


def treesitter_write_sigils(path: Path, sigils: dict[str, str], comment_prefix: str,
                            parse_fn, rel_path: str) -> None:
    """Shared text-based sigil insertion for tree-sitter adapters.

    sigils maps symbol_id → formatted sigil line text.
    parse_fn is the adapter's parse method (used to locate function line numbers).
    """
    if not sigils:
        return

    source = path.read_text(encoding="utf-8")
    lines = source.split("\n")

    # Parse to get function locations.
    records = parse_fn(path, rel_path)
    sym_to_line: dict[str, int] = {}
    for rec in records:
        sym_to_line[rec.symbol_id] = rec.line_range[0]

    # Process from bottom to top so line insertions don't shift later targets.
    targets = []
    for sym_id, sigil_text in sigils.items():
        line_no = sym_to_line.get(sym_id)
        if line_no is not None:
            targets.append((line_no, sym_id, sigil_text))

    targets.sort(key=lambda t: t[0], reverse=True)

    sigil_pattern = re.compile(
        rf"^\s*{re.escape(comment_prefix)}\s*@sig\s+[0-9a-f]{{8}}\s*\|"
    )

    for line_no, sym_id, sigil_text in targets:
        idx = line_no - 1  # 1-based to 0-based
        if idx < 0 or idx >= len(lines):
            continue

        # Detect indentation of the function definition line.
        def_line = lines[idx]
        indent = def_line[: len(def_line) - len(def_line.lstrip())]

        # Check if the line above is already a sigil comment.
        if idx > 0 and sigil_pattern.match(lines[idx - 1]):
            lines[idx - 1] = indent + sigil_text
        else:
            lines.insert(idx, indent + sigil_text)

    new_source = "\n".join(lines)
    tmp = path.with_suffix(path.suffix + ".sig.tmp")
    tmp.write_text(new_source, encoding="utf-8")
    os.replace(tmp, path)
