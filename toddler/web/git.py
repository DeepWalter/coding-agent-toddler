"""Git working-tree status and diffs for the REST API.

The agent's own git tools produce human text for the LLM; this module
runs ``git status --porcelain=v1 -z -uall --branch`` and parses the
NUL-framed records into ``{branch, files, dirs, sections}`` for the
frontend.  Non-repo and missing-git are not errors — they yield an
empty snapshot so the UI simply shows no status.  Per-file diffs are
parsed into structured hunks by :mod:`toddler.web.diffparse`.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from toddler.web import diffparse
from toddler.web.files import FileApiError, resolve_relative

__all__ = [
    "DiffError",
    "build_sections",
    "git_diff",
    "git_status",
    "parse_status",
]

_TIMEOUT = 30.0

# Context lines requested around each change — also the module default.
_CONTEXT_LINES = 3

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


def _parse_records(output: bytes) -> tuple[str | None, list[tuple[str, str, str]]]:
    """Porcelain v1 -z records: ``(branch, [(path, x, y), ...])``.

    Rename/copy records arrive as two NUL-terminated fields: the first
    carries the status and the NEW path, the second is the bare OLD path
    (verified against real git output), so the old path is skipped — it
    no longer exists on disk.  Untracked directories would collapse to
    ``dir/``; with ``-uall`` each file is listed individually, and the
    trailing-slash strip stays as defense-in-depth.  The XY pair is kept
    so :func:`build_sections` can split staged from unstaged changes.
    """
    fields = output.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    branch: str | None = None
    if fields and fields[0].startswith(b"## "):
        branch = _parse_branch(fields[0][3:].decode("utf-8", "replace"))
        fields = fields[1:]
    records: list[tuple[str, str, str]] = []
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
        records.append((path.rstrip("/"), x, y))
    return branch, records


def parse_status(output: bytes) -> tuple[str | None, dict[str, str]]:
    """Parse ``git status --porcelain=v1 -z --branch`` bytes.

    Returns ``(branch, {path: letter})`` — only changed paths appear,
    each collapsed to its badge letter by :func:`_letter`.
    """
    branch, records = _parse_records(output)
    return branch, {path: _letter(x, y) for path, x, y in records}


def build_sections(
    records: list[tuple[str, str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Split raw XY records into ``(staged, unstaged)`` letter maps.

    Untracked (``??``) lands only in the unstaged section as ``U``; a
    file changed in both axes (``MM``) appears in both; unmerged pairs
    get ``C`` in both, mirroring :func:`_letter`; copies (``C``) collapse
    to ``R`` like everything else.
    """
    staged: dict[str, str] = {}
    unstaged: dict[str, str] = {}
    for path, x, y in records:
        if x + y in _UNMERGED:
            staged[path] = unstaged[path] = "C"
            continue
        if x in "MADRCT":
            staged[path] = "R" if x == "C" else x
        if y in "MADRTU":
            unstaged[path] = y
        elif y == "?":  # untracked — worktree-only by definition
            unstaged[path] = "U"
    return staged, unstaged


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
# Snapshot + diffs
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


# The shape returned for non-repo / missing-git / timeout — `sections`
# present so the frontend never sees an undefined key.
_EMPTY_SNAPSHOT = {
    "branch": None,
    "files": {},
    "dirs": {},
    "sections": {"staged": {}, "unstaged": {}},
}


async def git_status(root: Path) -> dict[str, Any]:
    """Working-tree snapshot for *root*.

    Returns ``{"branch", "files", "dirs", "sections"}`` — ``files`` maps
    changed paths to badge letters, ``dirs`` maps every parent directory
    of a changed file to its badge (most severe descendant wins), and
    ``sections`` splits the same paths into ``{"staged", "unstaged"}``
    maps for the source-control panel.  Non-repo, missing git, and
    timeouts all return the empty snapshot — the UI treats "no git info"
    as a normal state, not an error.
    """
    try:
        stdout, _stderr, rc = await _git(
            "status", "--porcelain=v1", "-z", "-uall", "--branch", cwd=root
        )
    except (FileNotFoundError, OSError, TimeoutError):
        return _EMPTY_SNAPSHOT
    if rc != 0:
        return _EMPTY_SNAPSHOT
    branch, records = _parse_records(stdout)
    files = {path: _letter(x, y) for path, x, y in records}
    staged, unstaged = build_sections(records)
    return {
        "branch": branch,
        "files": files,
        "dirs": _dir_badges(files),
        "sections": {"staged": staged, "unstaged": unstaged},
    }


class DiffError(Exception):
    """A diff request problem that maps directly to an HTTP status code."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def git_diff(root: Path, rel: str, *, staged: bool) -> dict[str, Any]:
    """Structured side-by-side diff for *rel*.

    ``staged=True`` diffs the index against HEAD; ``staged=False`` the
    worktree against the index.  Untracked paths are diffed against
    ``/dev/null`` (where ``git diff --no-index`` exits 1 on differences
    — the success case).  Returns ``{path, staged, binary, truncated,
    old_path, new_path, hunks}`` with parsed hunks from
    :mod:`toddler.web.diffparse`; ``old_path``/``new_path`` are ``None``
    when that side is ``/dev/null`` (added/deleted files).  Raises
    :class:`DiffError` for bad paths and git failures.
    """
    try:
        path = resolve_relative(root, rel)
    except FileApiError as exc:
        raise DiffError(exc.status_code, exc.message) from None
    if path.is_dir():
        raise DiffError(400, f"not a file: {rel!r}")

    # One status call discriminates every case: no record = unchanged (or
    # missing) path, `??` = untracked, anything else = tracked — including
    # staged deletions, which have left the index and would defeat
    # `ls-files --error-unmatch`.
    try:
        stdout, _stderr, rc = await _git(
            "status", "--porcelain=v1", "-z", "--", rel, cwd=root,
        )
    except (FileNotFoundError, OSError, TimeoutError) as exc:
        raise DiffError(500, f"git is not available: {exc}") from exc
    if rc != 0:
        # Not a repository (or git is otherwise unusable) — never
        # reachable from the UI, whose file list comes from git_status.
        raise DiffError(500, "not a git repository")
    _branch, records = _parse_records(stdout)

    if records and records[0][1] + records[0][2] == "??":
        # Untracked — diff the file against the empty /dev/null side.
        if not path.is_file():
            raise DiffError(404, f"file not found: {rel!r}")
        try:
            stdout, stderr, rc = await _git(
                "diff", "--no-color", f"--unified={_CONTEXT_LINES}",
                "--no-index", "--", "/dev/null", rel, cwd=root,
            )
        except (FileNotFoundError, OSError, TimeoutError) as exc:
            raise DiffError(500, f"git diff failed: {exc}") from exc
        if rc == 1 and stderr.strip():
            raise DiffError(404, f"file not found: {rel!r}")
        if rc not in (0, 1):  # rc 1 = differences found — the success case
            raise DiffError(
                500,
                stderr.decode("utf-8", "replace").strip() or "git diff failed",
            )
    elif not records:
        # No record — either a clean tracked file or a path that does not
        # exist.  A missing path is a 404; a clean file diffs to nothing.
        if not path.is_file():
            raise DiffError(404, f"file not found: {rel!r}")
        stdout = b""
    else:
        try:
            stdout, _stderr, rc = await _git(
                "diff", "--no-color", f"--unified={_CONTEXT_LINES}",
                *(["--staged"] if staged else []), "--", rel, cwd=root,
            )
        except (FileNotFoundError, OSError, TimeoutError) as exc:
            raise DiffError(500, f"git diff failed: {exc}") from exc
        if rc != 0:
            raise DiffError(500, "git diff failed")

    parsed = diffparse.parse_diff(stdout)
    return {
        "path": rel,
        "staged": staged,
        "binary": parsed["binary"],
        "truncated": parsed["truncated"],
        "old_path": None if parsed["old_dev_null"] else rel,
        "new_path": None if parsed["new_dev_null"] else rel,
        "hunks": [asdict(h) for h in parsed["hunks"]],
    }
