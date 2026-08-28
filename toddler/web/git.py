"""Git working-tree status for the REST API.

The agent's own git tools produce human text for the LLM; this module
runs ``git status --porcelain=v1 -z -uall --branch`` and parses the
NUL-framed records into ``{branch, files, dirs}`` for the frontend.
Non-repo and missing-git are not errors — they yield an empty snapshot
so the UI simply shows no status.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

__all__ = ["git_status", "parse_status"]

_TIMEOUT = 30.0

# Badge-letter severity for directories — the most important change under
# a directory wins its badge.  Conflicts surface first (they block a
# commit), then deletions (files gone from disk), modifications, additions,
# renames, typechanges; untracked files are the least severe.
_PRIORITY = {"C": 0, "D": 1, "M": 2, "A": 3, "R": 4, "T": 5, "U": 6}

# Unmerged XY codes in porcelain v1 (merge conflicts) — "MM" is NOT one
# (that's staged + unstaged modification).
_UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


# ---------------------------------------------------------------------------
# Porcelain v1 -z parsing
# ---------------------------------------------------------------------------


def _letter(x: str, y: str) -> str:
    """Collapse the XY pair to one badge letter.

    Letters match VS Code's file decorations: ``U`` untracked, ``C``
    conflict, staged status takes precedence over unstaged, copies show
    as renames, and everything else falls back to ``M``.
    """
    if (x, y) == ("?", "?"):
        return "U"
    if x + y in _UNMERGED:
        return "C"
    primary = x if x != " " else y
    return {"M": "M", "A": "A", "D": "D", "R": "R", "C": "R", "T": "T"}.get(
        primary, "M"
    )


def _parse_branch(header: str) -> str | None:
    """Branch name from the ``## ...`` header record."""
    if header.startswith("No commits yet on "):  # unborn branch
        return header[len("No commits yet on ") :]
    name = header.split(" ", 1)[0]  # detached → "HEAD (no branch)" → "HEAD"
    return name.split("...", 1)[0] or None


def parse_status(output: bytes) -> tuple[str | None, dict[str, str]]:
    """Parse ``git status --porcelain=v1 -z --branch`` bytes.

    Returns ``(branch, {path: letter})`` — only changed paths appear.
    Rename/copy records arrive as two NUL-terminated fields: the first
    carries the status and the NEW path, the second is the bare OLD path
    (verified against real git output), so the old path is skipped — it
    no longer exists on disk.  Untracked directories would collapse to
    ``dir/``; with ``-uall`` each file is listed individually, and the
    trailing-slash strip stays as defense-in-depth.
    """
    fields = output.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    branch: str | None = None
    if fields and fields[0].startswith(b"## "):
        branch = _parse_branch(fields[0][3:].decode("utf-8", "replace"))
        fields = fields[1:]
    files: dict[str, str] = {}
    skip_source = False
    for field in fields:
        if skip_source:  # rename/copy: bare OLD-path record — drop it
            skip_source = False
            continue
        if len(field) < 3 or field[2] != 0x20:
            continue  # malformed — skip, never crash the snapshot
        x, y = chr(field[0]), chr(field[1])
        path = field[3:].decode("utf-8", "replace")
        if x in "RC":
            skip_source = True  # next NUL record is the old path
        files[path.rstrip("/")] = _letter(x, y)
    return branch, files


def _dir_badges(files: dict[str, str]) -> dict[str, str]:
    """Badge letters for every parent directory of a changed file.

    Each changed path badges all of its ancestor directories, so a
    collapsed subtree still signals its changes; the most severe letter
    under a directory wins (see ``_PRIORITY``).  ``a.txt`` never badges
    a directory ``a`` — only real parent prefixes (``a/...``) get
    entries.  Computed over the full status map, so it stays correct
    below the explorer tree's depth limit.
    """
    dirs: dict[str, str] = {}
    for path, letter in files.items():
        parts = path.split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            if dirs.get(prefix) is None or _PRIORITY[letter] < _PRIORITY[dirs[prefix]]:
                dirs[prefix] = letter
    return dirs


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def _git(*args: str, cwd: Path) -> tuple[bytes, bytes, int]:
    """Run git, returning raw ``(stdout, stderr, returncode)`` bytes."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env={**os.environ},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), _TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return stdout, stderr, proc.returncode or 0


async def git_status(root: Path) -> dict[str, Any]:
    """Working-tree snapshot for *root*: ``{"branch", "files", "dirs"}``.

    ``files`` maps changed paths to badge letters; ``dirs`` maps every
    parent directory of a changed file to its badge (most severe
    descendant wins), so explorer folders signal changes.  Non-repo,
    missing git, and timeouts all return the empty snapshot — the UI
    treats "no git info" as a normal state, not an error.
    """
    try:
        stdout, _stderr, rc = await _git(
            "status", "--porcelain=v1", "-z", "-uall", "--branch", cwd=root
        )
    except (FileNotFoundError, OSError, TimeoutError):
        return {"branch": None, "files": {}, "dirs": {}}
    if rc != 0:
        return {"branch": None, "files": {}, "dirs": {}}
    branch, files = parse_status(stdout)
    return {"branch": branch, "files": files, "dirs": _dir_badges(files)}
