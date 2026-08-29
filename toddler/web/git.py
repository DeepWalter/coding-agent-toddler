"""Git working-tree status, diffs, and hunk actions for the REST API.

The agent's own git tools produce human text for the LLM; this module
runs ``git status --porcelain=v1 -z -uall --branch`` and parses the
NUL-framed records into ``{branch, files, dirs, sections}`` for the
frontend.  Non-repo and missing-git are not errors — they yield an
empty snapshot so the UI simply shows no status.  Per-file diffs are
parsed into structured hunks by :mod:`toddler.web.diffparse`, and
single-hunk stage/unstage/revert applies are server-authoritative: the
server re-runs the diff at apply time, exactly matches the client's
hunk against a fresh one (staleness check), and feeds ``git apply``
the matched hunk's raw bytes extracted from the fresh diff.
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
    "git_apply_hunk",
    "git_diff",
    "git_status",
    "parse_status",
]

_TIMEOUT = 30.0

# Server-side cap for hunk payloads — the server's own diffs cap at
# 10k lines, so a legitimate hunk is far smaller; this bounds a lying
# client's request body.
_MAX_HUNK_LINES = 20_000

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

# 409 body for hunks that no longer match the fresh diff — the UI shows
# it verbatim and every path raises the same message.
_STALE_MESSAGE = "file changed since this diff was shown — refresh"


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


async def _git(
    *args: str, cwd: Path, stdin: bytes | None = None
) -> tuple[bytes, bytes, int]:
    """Run git, returning raw ``(stdout, stderr, returncode)`` bytes.

    *stdin* feeds ``git apply`` patches without a temp file.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env={**os.environ},
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin), _TIMEOUT,
        )
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


async def _diff_untracked(root: Path, rel: str) -> bytes:
    """Diff an untracked file against ``/dev/null`` (``--no-index``).

    ``--no-index`` exits 1 on differences — the success case; exit 1
    with stderr means the file vanished between the status call and
    the diff.
    """
    try:
        stdout, stderr, rc = await _git(
            "diff", "--no-color", f"--unified={_CONTEXT_LINES}",
            "--no-index", "--", "/dev/null", rel, cwd=root,
        )
    except (FileNotFoundError, OSError, TimeoutError) as exc:
        raise DiffError(500, f"git diff failed: {exc}") from exc
    if rc == 1 and stderr.strip():
        raise DiffError(404, f"file not found: {rel!r}")
    if rc not in (0, 1):
        raise DiffError(
            500,
            stderr.decode("utf-8", "replace").strip() or "git diff failed",
        )
    return stdout


async def _path_state(root: Path, rel: str) -> tuple[str, str, str]:
    """Porcelain state of one path: ``(kind, x, y)``.

    ``kind`` is ``"untracked"`` (``??``), ``"unmerged"`` (a conflict
    pair), ``"tracked"`` (any other record), or ``"clean"`` (no
    record — unchanged or missing).  One status call discriminates
    every case, including staged deletions, which have left the index
    and would defeat ``ls-files --error-unmatch``.  Git failures raise
    :class:`DiffError` — a pathless non-zero status means not a repo.
    """
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
    if not records:
        return "clean", " ", " "
    x, y = records[0][1], records[0][2]
    if x + y == "??":
        return "untracked", x, y
    if x + y in _UNMERGED:
        return "unmerged", x, y
    return "tracked", x, y


async def _diff_raw(
    root: Path, rel: str, *, staged: bool, kind: str | None = None,
) -> tuple[bytes, str]:
    """Raw unified diff bytes for *rel*, plus its porcelain ``kind``.

    Shared by :func:`git_diff` and :func:`git_apply_hunk`, which pass
    their already-known ``kind`` to skip a second status spawn.  Enforces
    the same path guards the GET endpoint does — directories are 400,
    missing paths 404 — and diffs on the requested axis (untracked files
    against ``/dev/null``, where ``git diff --no-index`` exits 1 on
    differences — the success case).
    """
    try:
        path = resolve_relative(root, rel)
    except FileApiError as exc:
        raise DiffError(exc.status_code, exc.message) from None
    if path.is_dir():
        raise DiffError(400, f"not a file: {rel!r}")

    if kind is None:
        kind, _x, _y = await _path_state(root, rel)

    if kind == "untracked":
        if not path.is_file():
            raise DiffError(404, f"file not found: {rel!r}")
        stdout = await _diff_untracked(root, rel)
    elif kind == "clean":
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
    return stdout, kind


async def git_diff(root: Path, rel: str, *, staged: bool) -> dict[str, Any]:
    """Structured side-by-side diff for *rel*.

    ``staged=True`` diffs the index against HEAD; ``staged=False`` the
    worktree against the index.  Returns ``{path, staged, binary,
    truncated, old_path, new_path, hunks}`` with parsed hunks from
    :mod:`toddler.web.diffparse`; ``old_path``/``new_path`` are ``None``
    when that side is ``/dev/null`` (added/deleted files).  Raises
    :class:`DiffError` for bad paths and git failures.
    """
    stdout, _kind = await _diff_raw(root, rel, staged=staged)
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


# ---------------------------------------------------------------------------
# Per-hunk stage / unstage / revert
# ---------------------------------------------------------------------------


def _validate_hunk(
    hunk: dict[str, Any], old_path: str | None, new_path: str | None,
) -> None:
    """Reject malformed hunk payloads with a 400 :class:`DiffError`.

    The header-count checks also reject the partial last hunk of a
    truncated diff, which could never apply.  Per side, the start must
    be shaped the way git emits it: ``/dev/null`` is exactly ``0,0``
    and a real side starts at 1 with a count of at least 1 — any other
    combination (e.g. a parsed ``-0,5``) is malformed, not stale.
    """
    lines = hunk["lines"]
    if not lines or len(lines) > _MAX_HUNK_LINES:
        raise DiffError(400, "malformed hunk")
    kinds = [line.get("kind") for line in lines]
    ctx = kinds.count("ctx")
    dels = kinds.count("del")
    adds = kinds.count("add")
    if ctx + dels + adds != len(lines) or (dels == 0 and adds == 0):
        raise DiffError(400, "malformed hunk")
    if hunk["old_count"] != ctx + dels or hunk["new_count"] != ctx + adds:
        raise DiffError(400, "malformed hunk")
    for side, start, count in (
        (old_path, hunk["old_start"], hunk["old_count"]),
        (new_path, hunk["new_start"], hunk["new_count"]),
    ):
        if (start == 0) != (count == 0) or (side is None) != (start == 0):
            raise DiffError(400, "malformed hunk")


def _hunk_matches(hunk: dict[str, Any], fresh: diffparse.Hunk) -> bool:
    """True when the client's *hunk* equals the fresh server-side *fresh*.

    Starts/counts must be equal and every line's ``(kind, text,
    no_newline)`` must match pairwise — line numbers are display-only
    and ignored.  Lengths are compared first so an unequal zip cannot
    silently pass.
    """
    if (
        hunk["old_start"] != fresh.old_start
        or hunk["old_count"] != fresh.old_count
        or hunk["new_start"] != fresh.new_start
        or hunk["new_count"] != fresh.new_count
    ):
        return False
    lines = hunk["lines"]
    if len(lines) != len(fresh.lines):
        return False
    for echo, line in zip(lines, fresh.lines, strict=True):
        if echo.get("kind") != line.kind or echo.get("text") != line.text:
            return False
        if echo.get("no_newline", False) != line.no_newline:
            return False
    return True


async def _verify_hunk_matches(
    root: Path, rel: str, *, staged: bool, kind: str,
    old_path: str | None, new_path: str | None, hunk: dict[str, Any],
) -> bool:
    """Re-check staleness after a failed apply, on a fresh git run.

    A failed ``--index`` apply may have moved the index, so the diff is
    re-run and the hunk re-matched (the same checks ``git_apply_hunk``
    performs).  Raises :class:`DiffError` when the re-run itself cannot
    produce a diff (file deleted, git unavailable) — callers convert
    400/404 to staleness.
    """
    stdout, _kind = await _diff_raw(root, rel, staged=staged, kind=kind)
    parsed = diffparse.parse_diff(stdout)
    if parsed["binary"] or parsed["truncated"]:
        return False
    expected_old = None if parsed["old_dev_null"] else rel
    expected_new = None if parsed["new_dev_null"] else rel
    if old_path != expected_old or new_path != expected_new:
        return False
    return any(_hunk_matches(hunk, h) for h in parsed["hunks"])


async def _is_gitlink(root: Path, rel: str) -> bool:
    """True when *rel* is a submodule (gitlink index entry, mode 160000).

    Submodule diffs parse into hunks ("-Subproject commit …") that git
    apply cannot apply — reject before git's cryptic error.
    """
    try:
        out, _err, rc = await _git("ls-files", "--stage", "--", rel, cwd=root)
    except (FileNotFoundError, OSError, TimeoutError) as exc:
        raise DiffError(500, f"git is not available: {exc}") from exc
    if rc != 0:
        raise DiffError(500, "not a git repository")
    return out.lstrip().startswith(b"160000")


def _apply_command(action: str, staged: bool) -> tuple[tuple[str, ...], bool]:
    """The ``git apply`` argv for an action, plus its cached-axis flag.

    ``stage``/``unstage`` move worktree ↔ index (``--cached``);
    unstaged ``revert`` touches the worktree only; staged ``revert``
    uses ``--index`` so index and worktree revert together, atomically
    — a diverged worktree fails with nothing changed.
    """
    if action == "stage":
        return ("apply", "--cached", "-"), True
    if action == "unstage":
        return ("apply", "--cached", "--reverse", "-"), True
    if staged:
        return ("apply", "--index", "--reverse", "-"), False
    return ("apply", "--reverse", "-"), False


# Applies are serialized: two tabs — or a tab and a racing agent write —
# could otherwise interleave hunks and last-write-wins with both sides
# reporting success.
_APPLY_LOCK = asyncio.Lock()


async def git_apply_hunk(  # noqa: C901 — guarded pipeline, see verify step
    root: Path,
    rel: str,
    *,
    staged: bool,
    action: str,
    old_path: str | None,
    new_path: str | None,
    hunk: dict[str, Any],
) -> dict[str, Any]:
    """Stage, unstage, or revert one hunk of *rel*'s diff.

    Server-authoritative: at apply time the server re-runs the diff the
    UI would show, and the client's hunk must exactly match one of its
    hunks — the raw bytes of that matched hunk are what ``git apply``
    receives, so the client can neither forge headers nor apply stale
    content (no match is a 409, never corruption).  ``stage`` is only
    valid on the unstaged diff (worktree → index) and ``unstage`` only
    on the staged one (index → HEAD), but an MM file is legitimately
    stageable in its unstaged tab; ``revert`` works on either axis —
    unstaged reverts the worktree, staged reverts index and worktree
    together via ``--index`` so a diverged worktree fails with nothing
    changed.  A failed apply is re-verified on a fresh diff: the hunk
    still matching means ``git apply`` itself rejected it (500 with its
    stderr), no longer matching means the file moved underneath us (409
    refresh).
    """
    try:
        resolve_relative(root, rel)
    except FileApiError as exc:
        raise DiffError(exc.status_code, exc.message) from None

    if action == "stage" and staged:
        raise DiffError(400, "stage applies to the unstaged diff")
    if action == "unstage" and not staged:
        raise DiffError(400, "unstage applies to the staged diff")

    kind, _x, _y = await _path_state(root, rel)
    if kind == "untracked":
        raise DiffError(400, "untracked file — stage or revert the whole file")
    if kind == "unmerged":
        raise DiffError(400, "resolve conflicts before staging or reverting")

    _validate_hunk(hunk, old_path, new_path)

    stdout, _kind = await _diff_raw(root, rel, staged=staged, kind=kind)
    parsed = diffparse.parse_diff(stdout)
    if parsed["binary"]:
        raise DiffError(400, "binary diff — not supported")
    if parsed["truncated"]:
        raise DiffError(400, "diff truncated — refresh to retry")

    # The applied headers are server-owned raw bytes naming *rel* — a
    # client that forges side names is stale (409), never applied.
    expected_old = None if parsed["old_dev_null"] else rel
    expected_new = None if parsed["new_dev_null"] else rel
    if old_path != expected_old or new_path != expected_new:
        raise DiffError(409, _STALE_MESSAGE)

    fresh = next(
        (h for h in parsed["hunks"] if _hunk_matches(hunk, h)), None
    )
    if fresh is None:
        raise DiffError(409, _STALE_MESSAGE)

    if await _is_gitlink(root, rel):
        raise DiffError(400, "submodule — not supported")

    raw = diffparse.extract_hunk(
        stdout,
        old_start=fresh.old_start,
        old_count=fresh.old_count,
        new_start=fresh.new_start,
        new_count=fresh.new_count,
    )
    if raw is None:
        raise DiffError(409, _STALE_MESSAGE)

    args, _cached = _apply_command(action, staged)
    async with _APPLY_LOCK:
        try:
            _stdout, stderr, rc = await _git(*args, cwd=root, stdin=raw)
        except (FileNotFoundError, OSError, TimeoutError) as exc:
            raise DiffError(500, f"git is not available: {exc}") from exc
    if rc == 0:
        return {"ok": True}

    detail = stderr.decode("utf-8", "replace").strip()
    if not detail:
        detail = "git apply failed"

    # The apply failed — classify: a hunk that still matches the fresh
    # diff is a real failure (git itself rejected it), one that no
    # longer matches is staleness the first re-diff missed.
    try:
        still_matches = await _verify_hunk_matches(
            root, rel, staged=staged, kind=kind,
            old_path=old_path, new_path=new_path, hunk=hunk,
        )
    except DiffError as exc:
        if exc.status_code >= 500:
            raise
        still_matches = False
    if still_matches:
        raise DiffError(500, f"git apply failed: {detail[:500]}")
    raise DiffError(
        409,
        f"{_STALE_MESSAGE} ({detail[:500]})",
    )
