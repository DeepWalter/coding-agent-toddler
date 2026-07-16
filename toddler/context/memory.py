"""Persistent memory — user preferences that survive across sessions.

Stores a simple key-value dictionary as JSON at ``~/.toddler/memory.json``.
The contents are injected into the system prompt so the agent remembers user
preferences, coding conventions, and project-specific notes across restarts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MEMORY_DIR = Path.home() / ".toddler"
_DEFAULT_MEMORY_FILE = "memory.json"

# ---------------------------------------------------------------------------
# Memory entry dataclass
# ---------------------------------------------------------------------------

# Memory entries map to typed items in the JSON store.  For now they are
# unstructured dicts keyed by a user-provided name.  The store can hold
# arbitrary JSON-serialisable values; the ``prompt_section`` method knows
# how to format common schemas.


# ======================================================================
# PersistentMemory
# ======================================================================


class PersistentMemory:
    """Key-value store persisted as JSON at ``~/.toddler/memory.json``.

    Entries are arbitrary JSON-serialisable values keyed by a user-provided
    name.  The :meth:`prompt_section` method formats all entries into a
    block suitable for injection into the system prompt.

    Parameters
    ----------
    storage_dir:
        Directory that holds ``memory.json``.  Created if it doesn't exist.
        Defaults to ``~/.toddler``.
    """

    def __init__(
        self,
        storage_dir: str | Path = _DEFAULT_MEMORY_DIR,
    ) -> None:
        self._dir = Path(storage_dir).expanduser()
        self._file = self._dir / _DEFAULT_MEMORY_FILE

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    @property
    def file_path(self) -> Path:
        """Absolute path to the JSON backing file."""
        return self._file

    def load(self) -> dict[str, Any]:
        """Load all memory entries from disk.

        Returns an empty dict when the file doesn't exist or is corrupted.
        """
        if not self._file.is_file():
            return {}
        try:
            return self._read_file()
        except Exception:
            logger.exception(
                f"Failed to load memory from {self._file}; "
                f"returning empty store."
            )
            return {}

    def save(self, data: dict[str, Any]) -> None:
        """Atomically write *data* to the backing JSON file.

        Creates the storage directory if it doesn't exist.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            self._write_file(data)
        except Exception:
            logger.exception(f"Failed to save memory to {self._file}.")

    # ------------------------------------------------------------------
    # Convenience operations
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default*."""
        data = self.load()
        return data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set *key* to *value* and persist."""
        data = self.load()
        data[key] = value
        self.save(data)

    def delete(self, key: str) -> bool:
        """Remove *key* from the store.  Returns ``True`` if it existed."""
        data = self.load()
        existed = key in data
        if existed:
            del data[key]
            self.save(data)
        return existed

    def list_keys(self) -> list[str]:
        """Return all keys currently in the store."""
        return sorted(self.load().keys())

    # ------------------------------------------------------------------
    # System prompt injection
    # ------------------------------------------------------------------

    def prompt_section(self) -> str:
        """Format the full memory store as a system-prompt section.

        Returns an empty string when the store is empty.  The result is
        suitable for appending to the system prompt (surrounded by triple
        backticks for clarity).
        """
        data = self.load()
        if not data:
            return ""

        lines: list[str] = [
            "## Persistent Memory",
            "",
            "The following preferences and notes have been saved across sessions:",  # noqa: E501
            "",
        ]

        for key in sorted(data.keys()):
            value = data[key]
            lines.append(f"### {key}")
            lines.append("")
            if isinstance(value, str):
                lines.append(value)
            elif isinstance(value, dict):
                for k, v in value.items():
                    lines.append(f"- **{k}**: {v}")
            elif isinstance(value, list):
                for item in value:
                    lines.append(f"- {item}")
            else:
                lines.append(str(value))
            lines.append("")

        return "\n".join(lines)

    def compact_prompt_section(self) -> str:
        """Like :meth:`prompt_section` but more compact — one line per key.

        Useful for keeping the system prompt small in long conversations.
        """
        data = self.load()
        if not data:
            return ""

        lines: list[str] = [
            "## User Preferences (persistent)",
        ]
        for key in sorted(data.keys()):
            value = data[key]
            if isinstance(value, str) and len(value) < 120:
                lines.append(f"- **{key}**: {value}")
            elif isinstance(value, str):
                lines.append(f"- **{key}**: {value[:117]}...")
            elif isinstance(value, dict):
                items = list(value.items())
                preview = ", ".join(
                    f"{k}={str(v)[:40]}" for k, v in items[:3]
                )
                suffix = ", ..." if len(items) > 3 else ""
                lines.append(f"- **{key}**: {{{preview}{suffix}}}")
            else:
                lines.append(f"- **{key}**: (set)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _read_file(self) -> dict[str, Any]:
        """Read and parse the JSON file."""
        import json

        text = self._file.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            logger.warning(
                f"Memory file {self._file} contains {type(data).__name__} "
                f"instead of dict — resetting."
            )
            return {}
        return data

    def _write_file(self, data: dict[str, Any]) -> None:
        """Write *data* as pretty-printed JSON (atomic via temp file)."""
        import json
        import tempfile

        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)

        # Write to a temp file in the same directory, then rename.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self._dir),
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name

        Path(tmp_path).replace(self._file)
