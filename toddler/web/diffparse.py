"""Structured parsing of ``git diff`` unified output for the web UI.

The agent's own git tools return raw diff text for the LLM; this module
parses the same unified format into per-line entries with old/new line
numbers, so the frontend renders a side-by-side view without its own
parser.  Only the shapes git actually emits are handled — ``@@`` hunk
headers, ``--- /dev/null`` / ``+++ /dev/null`` markers, ``Binary files
... differ``, and the ``\\ No newline at end of file`` marker.  All other
header lines (``diff --git``, ``index``, mode and ``similarity index``
lines, ``rename from/to``) are skipped — and never parsed for paths,
because git C-quotes special characters there and appends a trailing
TAB to ``+++`` lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["DiffLine", "Hunk", "parse_diff"]

DEFAULT_MAX_LINES = 10_000

# ``@@ -old_start[,old_count] +new_start[,new_count] @@`` — counts are
# optional in git's own output (``@@ -1 +1 @@``), and a trailing function
# section is tolerated for robustness.  ``-0,0``/``+0,0`` (new/deleted
# files) match naturally.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


@dataclass
class DiffLine:
    """One unified-diff content line, with its line numbers.

    ``old_ln``/``new_ln`` are ``None`` on the side the line does not
    exist (adds have no old number, deletions no new number).  ``text``
    excludes the leading ``space/+/-`` marker and any trailing ``\\r``.
    """

    kind: str  # "ctx" | "del" | "add"
    old_ln: int | None
    new_ln: int | None
    text: str
    no_newline: bool = False


@dataclass
class Hunk:
    """A ``@@ -o,c +n,c @@`` section: contiguous lines from both sides."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)


def parse_diff(output: bytes, *, max_lines: int = DEFAULT_MAX_LINES) -> dict:
    """Parse unified-diff *output* into ``{"hunks", "binary", ...}``.

    Returns ``hunks`` (list of :class:`Hunk`), ``binary`` (git reported
    binary content and the diff was not parsed), ``truncated`` (parsing
    stopped at *max_lines* lines), and ``old_dev_null``/``new_dev_null``
    (the old/new side was ``/dev/null`` — the file was added or deleted).
    """
    hunks: list[Hunk] = []
    binary = truncated = old_dev_null = new_dev_null = False
    hunk: Hunk | None = None
    old_ln = new_ln = 0
    emitted = 0

    for raw in output.decode("utf-8", "replace").splitlines():
        line = raw[:-1] if raw.endswith("\r") else raw  # CRLF repos

        if line == "\\ No newline at end of file":
            # Belongs to the last content line of the current hunk.
            if hunk and hunk.lines:
                hunk.lines[-1].no_newline = True
            continue

        if line.startswith("Binary files "):
            binary = True
            break

        match = _HUNK_RE.match(line)
        if match:
            if hunk and hunk.lines:
                hunks.append(hunk)
            old_start, new_start = int(match.group(1)), int(match.group(3))
            hunk = Hunk(
                old_start=old_start,
                old_count=int(match.group(2) or 1),
                new_start=new_start,
                new_count=int(match.group(4) or 1),
            )
            old_ln, new_ln = old_start, new_start
            continue

        if hunk is None:
            # Pre-hunk header area — only the /dev/null markers matter.
            if line == "--- /dev/null":
                old_dev_null = True
            elif line == "+++ /dev/null":
                new_dev_null = True
            continue

        # A content line inside a hunk: ' ' context, '-' deletion, '+'
        # addition — anything else is malformed and skipped.
        marker, text = line[0], line[1:]
        if marker == " ":
            hunk.lines.append(DiffLine("ctx", old_ln, new_ln, text))
            old_ln += 1
            new_ln += 1
        elif marker == "-":
            hunk.lines.append(DiffLine("del", old_ln, None, text))
            old_ln += 1
        elif marker == "+":
            hunk.lines.append(DiffLine("add", None, new_ln, text))
            new_ln += 1
        else:
            continue

        emitted += 1
        if emitted >= max_lines:
            truncated = True
            break

    if hunk and hunk.lines:
        hunks.append(hunk)
    return {
        "hunks": hunks,
        "binary": binary,
        "truncated": truncated,
        "old_dev_null": old_dev_null,
        "new_dev_null": new_dev_null,
    }
