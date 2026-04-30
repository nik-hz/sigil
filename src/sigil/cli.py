"""Click CLI: sig init | list | drift | show | hook post-tool | hook session-start."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from sigil.format import now_iso
from sigil.languages import get_adapter, supported_extensions
from sigil.sidecar import Sidecar, compute_called_by, drift_status, refresh_sidecar
from sigil.store import _record_from_existing, find_project_root, is_ignored, _EXT_TO_LANG


@click.group()
def cli() -> None:
    """sig — sigil provenance tracker."""


@cli.command()
@click.option("--root", default=".", help="Project root (default: walk up from cwd).")
def init(root: str) -> None:
    """Snapshot all supported source files into the sidecar without inserting in-source comments."""
    root_path = find_project_root(Path(root))
    sidecar = Sidecar(root_path)
    exts = supported_extensions()
    n_new = 0
    for ext in exts:
        for f in root_path.rglob(f"*{ext}"):
            if is_ignored(f, root_path):
                continue
            rel = str(f.resolve().relative_to(root_path.resolve()))
            adapter = get_adapter(f.suffix)
            if adapter is None:
                continue
            language = _EXT_TO_LANG.get(f.suffix, f.suffix.lstrip("."))
            try:
                records = adapter.parse(f, rel)
            except Exception as e:
                click.echo(f"skip {rel}: {e}", err=True)
                continue
            for rec in records:
                if rec.symbol_id not in sidecar.data["symbols"]:
                    sidecar.upsert(rec.symbol_id, _record_from_existing(rel, rec, language))
                    n_new += 1
    # Also populate module-level summaries.
    n_modules = 0
    for ext in exts:
        for f in root_path.rglob(f"*{ext}"):
            if is_ignored(f, root_path):
                continue
            rel = str(f.resolve().relative_to(root_path.resolve()))
            adapter = get_adapter(f.suffix)
            if adapter is None or not hasattr(adapter, "parse_module"):
                continue
            try:
                mod_rec = adapter.parse_module(f, rel)
                sidecar.data["modules"][rel] = {
                    "language": mod_rec.language,
                    "imports": mod_rec.imports,
                    "exports": mod_rec.exports,
                    "line_count": mod_rec.line_count,
                }
                n_modules += 1
            except Exception:
                continue

    sidecar.data["last_full_scan"] = now_iso()
    sidecar.save()
    click.echo(f"snapshotted {n_new} symbols, {n_modules} modules → {sidecar.path}")


@cli.command(name="list")
@click.option("--drifted", is_flag=True)
@click.option("--tag", "filter_tag", default=None, help="Filter symbols by tag.")
def list_cmd(drifted: bool, filter_tag: str | None) -> None:
    """List tracked symbols and their drift status."""
    root = find_project_root(Path("."))
    sidecar = Sidecar(root)
    refresh_sidecar(root, sidecar)
    for sid, rec in sorted(sidecar.data["symbols"].items()):
        status = drift_status(rec)
        if drifted and status != "drifted":
            continue
        if filter_tag and filter_tag not in rec.get("tags", []):
            continue
        agent = rec.get("sigil_agent") or "-"
        tags = rec.get("tags", [])
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        click.echo(f"{status:10s} {sid}  ({agent}){tag_str}")


@cli.command()
def drift() -> None:
    """List drifted symbols. Exit 1 if any drift found, else 0."""
    root = find_project_root(Path("."))
    sidecar = Sidecar(root)
    refresh_sidecar(root, sidecar)
    drifted = [(sid, rec) for sid, rec in sidecar.data["symbols"].items() if drift_status(rec) == "drifted"]
    if not drifted:
        click.echo("no drift")
        sys.exit(0)
    for sid, rec in drifted:
        click.echo(sid)
        click.echo(f"  recorded:  {rec['sigil_hash']} by {rec['sigil_agent']} at {rec['sigil_timestamp']}")
        click.echo(f"  current:   {rec['body_hash']}")
    sys.exit(1)


@cli.command()
@click.argument("symbol_id")
def show(symbol_id: str) -> None:
    """Show the full sidecar record for one symbol."""
    root = find_project_root(Path("."))
    sidecar = Sidecar(root)
    rec = sidecar.data["symbols"].get(symbol_id)
    if not rec:
        click.echo(f"no record: {symbol_id}", err=True)
        sys.exit(2)
    # Attach called_by for display.
    called_by = compute_called_by(sidecar.data["symbols"])
    display = dict(rec)
    if symbol_id in called_by:
        display["called_by"] = called_by[symbol_id]
    click.echo(json.dumps(display, indent=2, sort_keys=True))


@cli.command()
@click.argument("symbol_id")
def graph(symbol_id: str) -> None:
    """Show call graph for a symbol: what it calls and what calls it."""
    root = find_project_root(Path("."))
    sidecar = Sidecar(root)
    refresh_sidecar(root, sidecar)
    symbols = sidecar.data["symbols"]
    rec = symbols.get(symbol_id)
    if not rec:
        click.echo(f"no record: {symbol_id}", err=True)
        sys.exit(2)

    called_by = compute_called_by(symbols)

    calls = rec.get("calls", [])
    callers = called_by.get(symbol_id, [])

    click.echo(f"Symbol: {symbol_id}")
    click.echo(f"\nCalls ({len(calls)}):")
    for c in calls:
        click.echo(f"  → {c}")
    click.echo(f"\nCalled by ({len(callers)}):")
    for c in callers:
        click.echo(f"  ← {c}")


@cli.command()
@click.argument("symbol_id")
@click.argument("tags", nargs=-1, required=True)
@click.option("--remove", is_flag=True, help="Remove tags instead of adding.")
def tag(symbol_id: str, tags: tuple[str, ...], remove: bool) -> None:
    """Add or remove tags on a symbol. Usage: sig tag <symbol> tag1 tag2 ..."""
    root = find_project_root(Path("."))
    sidecar = Sidecar(root)
    rec = sidecar.data["symbols"].get(symbol_id)
    if not rec:
        click.echo(f"no record: {symbol_id}", err=True)
        sys.exit(2)
    current = set(rec.get("tags", []))
    if remove:
        current -= set(tags)
    else:
        current |= set(tags)
    rec["tags"] = sorted(current)
    sidecar.save()
    click.echo(f"{symbol_id}: tags={rec['tags']}")


@cli.command()
@click.argument("symbol_id")
@click.option("--reads", multiple=True, help="Resources this function reads from.")
@click.option("--writes", multiple=True, help="Resources this function writes to.")
@click.option("--clear", is_flag=True, help="Clear all data flow annotations.")
def annotate(symbol_id: str, reads: tuple[str, ...], writes: tuple[str, ...], clear: bool) -> None:
    """Annotate a symbol with data flow info (reads/writes).

    Examples:
      sig annotate myfile.py::load_config --reads config.yaml --reads .env
      sig annotate myfile.py::save_results --writes runs/metadata.json
    """
    root = find_project_root(Path("."))
    sidecar = Sidecar(root)
    rec = sidecar.data["symbols"].get(symbol_id)
    if not rec:
        click.echo(f"no record: {symbol_id}", err=True)
        sys.exit(2)

    if clear:
        rec.pop("reads", None)
        rec.pop("writes", None)
        sidecar.save()
        click.echo(f"{symbol_id}: data flow annotations cleared")
        return

    if not reads and not writes:
        # Display current annotations.
        r = rec.get("reads", [])
        w = rec.get("writes", [])
        click.echo(f"{symbol_id}")
        click.echo(f"  reads:  {', '.join(r) if r else '(none)'}")
        click.echo(f"  writes: {', '.join(w) if w else '(none)'}")
        return

    # Merge with existing.
    existing_reads = set(rec.get("reads", []))
    existing_writes = set(rec.get("writes", []))
    existing_reads |= set(reads)
    existing_writes |= set(writes)
    rec["reads"] = sorted(existing_reads)
    rec["writes"] = sorted(existing_writes)
    sidecar.save()
    click.echo(f"{symbol_id}")
    click.echo(f"  reads:  {', '.join(rec['reads'])}")
    click.echo(f"  writes: {', '.join(rec['writes'])}")


@cli.command()
@click.option("--min-cooccurrence", default=2, type=int, help="Min co-modification count.")
@click.option("--json-output", "json_out", is_flag=True, help="Output as JSON.")
def clusters(min_cooccurrence: int, json_out: bool) -> None:
    """Show functions that are frequently modified together in commits."""
    from sigil.clusters import compute_clusters

    root = find_project_root(Path("."))
    sidecar = Sidecar(root)
    result = compute_clusters(root, sidecar.data["symbols"], min_cooccurrence=min_cooccurrence)
    if not result:
        click.echo("no change clusters found (need git history with multiple co-modified files)")
        sys.exit(0)
    if json_out:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        for sid, partners in sorted(result.items()):
            click.echo(f"{sid}")
            for p in partners[:5]:
                click.echo(f"  ↔ {p}")
            if len(partners) > 5:
                click.echo(f"  ... and {len(partners) - 5} more")


@cli.command()
@click.option("--json-output", "json_out", is_flag=True, help="Output as JSON.")
def modules(json_out: bool) -> None:
    """List module-level summaries (imports, exports, line counts)."""
    root = find_project_root(Path("."))
    sidecar = Sidecar(root)
    mods = sidecar.data.get("modules", {})
    if not mods:
        click.echo("no modules indexed — run `sig init` first")
        sys.exit(0)
    if json_out:
        click.echo(json.dumps(mods, indent=2, sort_keys=True))
    else:
        for rel, info in sorted(mods.items()):
            exports = info.get("exports", [])
            imports = info.get("imports", [])
            lines = info.get("line_count", 0)
            click.echo(f"{rel}  ({info.get('language', '?')}, {lines} lines)")
            if exports:
                click.echo(f"  exports: {', '.join(exports[:10])}")
            if imports:
                click.echo(f"  imports: {', '.join(imports[:10])}")


@cli.group()
def hook() -> None:
    """Internal — Claude Code hook entry points."""


@hook.command("post-tool")
def _hook_post_tool() -> None:
    """PostToolUse handler. Reads the Claude Code JSON payload from stdin."""
    from sigil.hook import hook_post_tool
    hook_post_tool()


@hook.command("session-start")
def _hook_session_start() -> None:
    """SessionStart handler. Reports drifted sigiled functions as additionalContext."""
    from sigil.hook import hook_session_start
    hook_session_start()


def main() -> None:
    cli()
