"""File access and gitignore-aware tree walking for the REST API.

The ``ReadFile`` tool returns line-numbered output that the editor must
not parse, so the web endpoints use pathlib directly.  Writes stay
contained: every write request passes through :func:`resolve_relative`,
which enforces containment inside the served ``repo_root``.  Reads are
deliberately wider — :func:`resolve_any` serves any file the process can
read, so the editor can open what a tool call reached outside the root
(read-only there).  There is no auth: bound beyond loopback, this read
surface is reachable by anyone on the network.
"""

from __future__ import annotations

from pathlib import Path

from toddler.context.workspace import GitignoreMatcher
from toddler.tools.base import display_path

__all__ = [
    "FileApiError",
    "IGNORED_TOP",
    "build_file_tree",
    "read_file",
    "resolve_any",
    "resolve_relative",
    "tool_path",
    "tool_path_abs",
    "tool_path_named",
    "write_file",
]

# ---------------------------------------------------------------------------
# Errors and constants
# ---------------------------------------------------------------------------


class FileApiError(Exception):
    """A path/file problem that maps directly to an HTTP status code."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# Top-level names never shown in the file tree, even without a
# ``.gitignore`` — mirrors the project mapper's always-exclude set.
IGNORED_TOP = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", ".venv", "venv", "dist", "build",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".idea", ".vscode", ".DS_Store",
})


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_relative(root: Path, rel: str) -> Path:
    """Resolve *rel* inside *root*, rejecting any path that escapes.

    Returns an absolute :class:`Path` guaranteed to be inside *root*:
    ``..`` segments, absolute paths, and symlinks pointing outside all
    resolve to a location outside the root and raise
    :class:`FileApiError` (400).
    """
    if not isinstance(rel, str) or not rel.strip():
        raise FileApiError(400, "`path` query parameter is required.")
    if "\x00" in rel:
        raise FileApiError(400, "invalid path.")
    root_abs = root.resolve()
    candidate = (root_abs / rel).resolve()
    if not candidate.is_relative_to(root_abs):
        raise FileApiError(400, f"path escapes the repository root: {rel!r}")
    return candidate


def resolve_any(root: Path, raw: str) -> Path:
    """Resolve *raw* for a read, wherever it points.

    The read-path twin of :func:`tool_path_abs`: absolute paths, ``~``
    and paths relative to the served root all resolve, and nothing is
    refused for landing outside the root — the editor opens external
    files read-only.  :func:`resolve_relative` remains the gate for
    writes and git.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise FileApiError(400, "`path` query parameter is required.")
    if "\x00" in raw:
        raise FileApiError(400, "invalid path.")
    try:
        candidate = Path(raw).expanduser()
    except RuntimeError:  # ``~user`` for a user this machine hasn't got
        raise FileApiError(400, f"invalid path: {raw!r}") from None
    if not candidate.is_absolute():
        candidate = root.resolve() / candidate
    return candidate.resolve()


def tool_path_abs(root: Path, raw: object) -> str | None:
    """Where a path a tool call named actually is — or ``None`` when it
    names nothing at all.

    A call may name an absolute path, one relative to the agent's own cwd
    (which is the served root), or ``~``.  The file tools expand and
    resolve all three before touching the disk, and this is that same
    resolution, so the file the browser is told about is the file the
    call touched.  ``~`` is the case the browser could never work out for
    itself: expanding it needs the home directory, which only this side
    of the wire knows.
    """
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return None
    try:
        candidate = Path(raw).expanduser()
    except RuntimeError:  # ``~user`` for a user this machine hasn't got
        return None
    if not candidate.is_absolute():
        candidate = root.resolve() / candidate
    return str(candidate.resolve())


def tool_path_named(root: Path, raw: object) -> str | None:
    """The same file again, named the way the call wrote it.

    Where :func:`tool_path_abs` answers where the file *is* — resolved,
    which on macOS rewrites whole trees (``/tmp`` reads back as
    ``/private/tmp``, ``/home`` as ``/System/Volumes/Data/home``) — this
    is the spelling the card puts in front of a reader, who named the
    file the other way.  Same file, both times.

    A ``..`` standing after a link is the exception, and resolves: the
    kernel reads ``link/..`` as the parent of what the link points at, so
    the written form would name a file the call never touched.
    """
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return None
    try:
        candidate = Path(raw).expanduser()
    except RuntimeError:  # ``~user`` for a user this machine hasn't got
        return None
    if not candidate.is_absolute():
        candidate = root / candidate
    return str(display_path(candidate))


def tool_path(root: Path, raw: object) -> str | None:
    """The same file for a tab to hold: *root*-relative, or ``None`` when
    it lands outside the root, or names nothing at all.

    Every other pane names a file the way the tree does — POSIX, relative
    to the root — so a card that opens a tab names it that way too.  A
    call that resolved elsewhere has no such name: the card falls back to
    ``path_abs``, which opens in the editor read-only.

    Containment is :func:`resolve_relative`'s property, but this is not
    that call: a request naming a path outside the root is a 400, where a
    call that already ran has simply nothing here to open.
    """
    absolute = tool_path_abs(root, raw)
    if absolute is None:
        return None
    root_abs = root.resolve()
    candidate = Path(absolute)
    if not candidate.is_relative_to(root_abs):
        return None
    # A path that lands ON the root — ``.``, or the root spelled out —
    # names no file to open.
    rel = candidate.relative_to(root_abs)
    return rel.as_posix() if rel.parts else None


# ---------------------------------------------------------------------------
# File read / write
# ---------------------------------------------------------------------------


def read_file(root: Path, raw: str) -> dict:
    """Read the text file *raw* names, returning ``{path, content,
    total_lines}``.

    *raw* is root-relative or absolute, and an absolute path is not
    required to lie inside the root — the editor opens external files
    read-only.  The echoed ``path`` is the file's canonical name:
    root-relative inside the root, absolute outside.

    Raises :class:`FileApiError` — 404 when the file is missing, 400 for
    binary or non-UTF-8 content (a textarea editor must not be fed
    arbitrary bytes).
    """
    path = resolve_any(root, raw)
    if not path.is_file():
        raise FileApiError(404, f"file not found: {raw!r}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FileApiError(400, f"cannot read file: {exc}") from exc
    if b"\x00" in data:
        raise FileApiError(400, f"binary file — refusing to serve: {raw!r}")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FileApiError(400, f"not a UTF-8 text file: {raw!r}") from None
    total_lines = content.count("\n") + (
        1 if content and not content.endswith("\n") else 0
    )
    root_abs = root.resolve()
    shown = (
        path.relative_to(root_abs).as_posix()
        if path.is_relative_to(root_abs)
        else str(path)
    )
    return {"path": shown, "content": content, "total_lines": total_lines}


def write_file(root: Path, rel: str, content: str) -> dict:
    """Write *content* to *rel*, creating parent directories on demand.

    Mirrors the ``WriteFile`` tool's parent-dir behavior; the path must
    not escape *root*.  Returns ``{ok: True, bytes: n}`` with the byte
    count of the encoded content.
    """
    path = resolve_relative(root, rel)
    if path.is_dir():
        raise FileApiError(400, f"cannot write to a directory: {rel!r}")
    data = content.encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        raise FileApiError(400, f"cannot write file: {exc}") from exc
    return {"ok": True, "bytes": len(data)}


# ---------------------------------------------------------------------------
# Tree walk
# ---------------------------------------------------------------------------


def _load_gitignore(root: Path) -> GitignoreMatcher:
    """Load ``root/.gitignore`` (root-level only, like the project mapper)."""
    ignore_file = root / ".gitignore"
    if not ignore_file.is_file():
        return GitignoreMatcher([], root)
    return GitignoreMatcher(
        ignore_file.read_text(encoding="utf-8").splitlines(), root,
    )


def build_file_tree(root: Path, *, depth: int = 4) -> dict:
    """Build a gitignore-aware listing of *root* down to *depth* levels.

    Returns ``{"root": str, "entries": [{path, type}]}`` with relative
    POSIX paths, directories and files sorted.  ``IGNORED_TOP`` names and
    ``.gitignore`` matches are pruned — the explorer should show what the
    agent's tools can actually see.
    """
    root_abs = root.resolve()
    gitignore = _load_gitignore(root_abs)
    entries: list[dict[str, str]] = []

    def ignored(path: Path, *, is_dir: bool) -> bool:
        return (
            path.name in IGNORED_TOP
            or gitignore.is_ignored(path, is_dir=is_dir)
        )

    for dirpath, dirnames, filenames in root_abs.walk():
        level = len(dirpath.relative_to(root_abs).parts)
        if level >= depth:
            # Below the requested depth — drop the dirs so walk() prunes.
            dirnames[:] = []
            continue
        visible_dirs = sorted(
            d for d in dirnames if not ignored(dirpath / d, is_dir=True)
        )
        dirnames[:] = visible_dirs
        visible_files = sorted(
            f for f in filenames if not ignored(dirpath / f, is_dir=False)
        )
        for name in visible_dirs:
            entries.append({
                "path": (dirpath / name).relative_to(root_abs).as_posix(),
                "type": "dir",
            })
        for name in visible_files:
            entries.append({
                "path": (dirpath / name).relative_to(root_abs).as_posix(),
                "type": "file",
            })

    return {"root": str(root_abs), "entries": entries}
