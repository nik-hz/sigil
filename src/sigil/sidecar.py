"""Sidecar index: JSON store at .sigil/index.json."""

from __future__ import annotations

import json
import os
from pathlib import Path

SIDECAR_DIR = ".sigil"
SIDECAR_FILE = "index.json"


class Sidecar:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / SIDECAR_DIR / SIDECAR_FILE
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data.setdefault("modules", {})
            return data
        return {"version": "0.1", "project_root": str(self.root), "last_full_scan": None, "symbols": {}, "modules": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def upsert(self, symbol_id: str, record: dict) -> None:
        self.data["symbols"][symbol_id] = record


def drift_status(record: dict) -> str:
    if not record.get("sigil_present"):
        return "unmanaged"
    if record.get("body_hash") is None:
        return "orphaned"
    return "synced" if record.get("sigil_hash") == record.get("body_hash") else "drifted"


# Standard layer vocabulary for auto-inference from file paths.
_LAYER_PATTERNS: list[tuple[str, list[str]]] = [
    ("api",        ["api/", "routes/", "endpoints/", "views/", "handlers/", "controllers/"]),
    ("cli",        ["cli/", "cli.py", "commands/", "cmd/"]),
    ("storage",    ["store/", "storage/", "db/", "database/", "models/", "repo/", "repository/"]),
    ("validation", ["validation/", "validators/", "schemas/", "schema/"]),
    ("config",     ["config/", "settings/", "conf/"]),
    ("test",       ["test/", "tests/", "test_", "_test.", "spec/", ".test.", ".spec."]),
    ("hook",       ["hook/", "hooks/", "hook.py", "hooks.py"]),
    ("format",     ["format/", "format.py", "formatting/", "serialization/"]),
    ("language",   ["languages/", "lang/", "parsers/", "adapters/"]),
]


def infer_tags(file_path: str) -> list[str]:
    """Infer semantic layer tags from a file's path."""
    lower = file_path.lower()
    tags = []
    for layer, patterns in _LAYER_PATTERNS:
        for pat in patterns:
            if pat in lower:
                tags.append(layer)
                break
    return sorted(set(tags))


def compute_called_by(symbols: dict) -> dict[str, list[str]]:
    """Build a reverse index: symbol_id → list of symbol_ids that call it."""
    # Build a map of short name → list of full symbol_ids for resolution.
    name_to_sids: dict[str, list[str]] = {}
    for sid in symbols:
        # "file.py::Class.method" → "method", "Class.method"
        _, _, qual = sid.partition("::")
        parts = qual.split(".")
        for i in range(len(parts)):
            short = ".".join(parts[i:])
            name_to_sids.setdefault(short, []).append(sid)

    called_by: dict[str, list[str]] = {}
    for sid, rec in symbols.items():
        for call_name in rec.get("calls", []):
            # Try to resolve call_name to known symbol_ids.
            targets = name_to_sids.get(call_name, [])
            for target in targets:
                if target != sid:
                    called_by.setdefault(target, []).append(sid)

    # Deduplicate and sort.
    return {k: sorted(set(v)) for k, v in called_by.items()}


def refresh_sidecar(root: Path, sidecar: Sidecar) -> None:
    """Re-read files referenced in the sidecar and update each symbol's current body_hash."""
    from sigil.languages import get_adapter

    by_file: dict[str, list[str]] = {}
    for sid, rec in sidecar.data["symbols"].items():
        by_file.setdefault(rec["file"], []).append(sid)

    for rel, sids in by_file.items():
        path = root / rel
        if not path.exists():
            for sid in sids:
                sidecar.data["symbols"][sid]["body_hash"] = None
            continue

        adapter = get_adapter(path.suffix)
        if adapter is None:
            continue

        try:
            records = adapter.parse(path, rel)
        except Exception:
            continue

        seen = {r.symbol_id: r for r in records}
        for sid in sids:
            r = seen.get(sid)
            sidecar.data["symbols"][sid]["body_hash"] = r.body_hash if r else None
