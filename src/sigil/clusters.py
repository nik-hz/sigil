"""Change clusters: find functions that are frequently co-modified in commits."""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path


def _git_log_file_groups(root: Path, max_commits: int = 500) -> list[list[str]]:
    """Get lists of files changed together per commit from git log."""
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", f"-{max_commits}"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0:
        return []

    groups: list[list[str]] = []
    current: list[str] = []
    for line in result.stdout.split("\n"):
        line = line.strip()
        if not line:
            if current:
                groups.append(current)
                current = []
        else:
            current.append(line)
    if current:
        groups.append(current)
    return groups


def compute_clusters(root: Path, symbols: dict, min_cooccurrence: int = 2) -> dict[str, list[str]]:
    """Find symbols that are frequently modified together.

    Returns a dict mapping symbol_id → list of co-modified symbol_ids,
    sorted by frequency (most frequent first).
    """
    # Map files to their symbols.
    file_to_symbols: dict[str, list[str]] = {}
    for sid, rec in symbols.items():
        f = rec.get("file", "")
        file_to_symbols.setdefault(f, []).append(sid)

    # Count co-occurrences: how often each pair of symbols' files appear in the same commit.
    pair_counts: Counter[tuple[str, str]] = Counter()
    file_groups = _git_log_file_groups(root)

    for files in file_groups:
        # Only consider tracked files.
        tracked = [f for f in files if f in file_to_symbols]
        if len(tracked) < 2:
            continue
        # Count all symbol pairs across co-modified files.
        all_syms: list[str] = []
        for f in tracked:
            all_syms.extend(file_to_symbols[f])
        # Count pairs.
        for i, a in enumerate(all_syms):
            for b in all_syms[i + 1:]:
                pair = (min(a, b), max(a, b))
                pair_counts[pair] += 1

    # Build clusters: for each symbol, list co-modified symbols above threshold.
    clusters: dict[str, list[str]] = {}
    for (a, b), count in pair_counts.items():
        if count >= min_cooccurrence:
            clusters.setdefault(a, []).append((count, b))
            clusters.setdefault(b, []).append((count, a))

    # Sort by frequency descending.
    return {
        sid: [s for _, s in sorted(partners, reverse=True)]
        for sid, partners in clusters.items()
    }
