"""Core operation: update_for_file and project-root helpers."""

from __future__ import annotations

from pathlib import Path

from sigil.format import format_sigil_line, now_iso
from sigil.languages import get_adapter
from sigil.languages.base import FunctionRecord
from sigil.sidecar import SIDECAR_DIR, Sidecar, infer_tags

DEFAULT_IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "dist", "build", ".sigil"}


def find_project_root(start: Path) -> Path:
    p = start.resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists() or (parent / SIDECAR_DIR).exists():
            return parent
    return p


def is_ignored(path: Path, root: Path) -> bool:
    rel = path.resolve().relative_to(root.resolve())
    return any(part in DEFAULT_IGNORE_DIRS for part in rel.parts)


def _record_from_existing(rel: str, rec: FunctionRecord, language: str) -> dict:
    bc = rec.existing_sigil
    result = {
        "file": rel,
        "language": language,
        "line_range": list(rec.line_range),
        "body_hash": rec.body_hash,
        "sigil_present": bc is not None,
        "sigil_hash": bc["hash"] if bc else None,
        "sigil_role": bc["role"] if bc else None,
        "sigil_agent": bc["agent_id"] if bc else None,
        "sigil_timestamp": bc["timestamp"] if bc else None,
    }
    if rec.calls is not None:
        result["calls"] = rec.calls
    result["tags"] = infer_tags(rel)
    return result


# Map extensions to language names for sidecar records.
_EXT_TO_LANG = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
}


# @sig c00c1cb6 | role: update_for_file | by: claude-code-292be15c | at: 2026-04-29T19:56:21Z
def update_for_file(root: Path, file_path: Path, agent_id: str, stamp_new: bool = False) -> dict:
    sidecar = Sidecar(root)
    rel = str(file_path.resolve().relative_to(root.resolve()))

    adapter = get_adapter(file_path.suffix)
    if adapter is None:
        return {"snapshotted": [], "stamped": [], "unchanged": []}

    language = _EXT_TO_LANG.get(file_path.suffix, file_path.suffix.lstrip("."))
    records = adapter.parse(file_path, rel)

    sigils_to_write: dict[str, str] = {}
    summary: dict[str, list[str]] = {"snapshotted": [], "stamped": [], "unchanged": []}
    ts = now_iso()

    for rec in records:
        prev = sidecar.data["symbols"].get(rec.symbol_id)

        if prev is None:
            if stamp_new:
                # Agent is creating this function — stamp it immediately.
                role = rec.symbol_id.split("::", 1)[1].split(".")[-1]
                line_text = format_sigil_line(rec.body_hash, role, agent_id, ts, prefix=adapter.comment_prefix)
                sigils_to_write[rec.symbol_id] = line_text
                entry = {
                    "file": rel,
                    "language": language,
                    "line_range": list(rec.line_range),
                    "body_hash": rec.body_hash,
                    "sigil_present": True,
                    "sigil_hash": rec.body_hash,
                    "sigil_role": role,
                    "sigil_agent": agent_id,
                    "sigil_timestamp": ts,
                }
                if rec.calls is not None:
                    entry["calls"] = rec.calls
                entry["tags"] = infer_tags(rel)
                sidecar.upsert(rec.symbol_id, entry)
                summary["stamped"].append(rec.symbol_id)
            else:
                sidecar.upsert(rec.symbol_id, _record_from_existing(rel, rec, language))
                summary["snapshotted"].append(rec.symbol_id)
            continue

        if prev["body_hash"] == rec.body_hash:
            summary["unchanged"].append(rec.symbol_id)
            continue

        # Body changed since last record → insert/update sigil.
        role = prev.get("sigil_role") or rec.symbol_id.split("::", 1)[1].split(".")[-1]
        line_text = format_sigil_line(rec.body_hash, role, agent_id, ts, prefix=adapter.comment_prefix)
        sigils_to_write[rec.symbol_id] = line_text

        entry = {
            "file": rel,
            "language": language,
            "line_range": list(rec.line_range),
            "body_hash": rec.body_hash,
            "sigil_present": True,
            "sigil_hash": rec.body_hash,
            "sigil_role": role,
            "sigil_agent": agent_id,
            "sigil_timestamp": ts,
        }
        if rec.calls is not None:
            entry["calls"] = rec.calls
        # Preserve user-set tags, merge with auto-inferred.
        existing_tags = prev.get("tags", [])
        auto_tags = infer_tags(rel)
        entry["tags"] = sorted(set(existing_tags + auto_tags))
        sidecar.upsert(rec.symbol_id, entry)
        summary["stamped"].append(rec.symbol_id)

    if sigils_to_write:
        adapter.write_sigils(file_path, rel, sigils_to_write)

    sidecar.save()
    return summary
