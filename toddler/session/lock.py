"""Per-session process claims — one live process per session.

A session is one conversation history, so two processes appending turns to
it would interleave their transcripts.  Resolving a session therefore
claims it first: a small status file under
``<session_dir>/sessions/<session_id>.json`` held with an OS advisory
lock (``flock``).  The kernel drops the lock when the process exits —
including on a crash — so a leftover file never pins a session forever:
the next process to open it takes the lock and rewrites it.

The file body (pid, start time) is diagnostic; the lock itself is the
authority.  Release removes the file while the lock is still held, and a
claim re-checks the inode after locking, so a file unlinked between
``open`` and ``flock`` cannot hand one session to two processes.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "SessionInUseError",
    "SessionLock",
    "SessionLockManager",
]

logger = logging.getLogger(__name__)

# How many times a claim retries when the status file is replaced under it
# (a releasing holder unlinking the path between our open and flock).
_CLAIM_RETRIES = 3


# ======================================================================
# SessionInUseError
# ======================================================================


class SessionInUseError(RuntimeError):
    """The session is already open in another live process."""

    def __init__(self, session_id: str, holder: dict | None = None) -> None:
        self.session_id = session_id
        self.holder = holder
        pid = holder.get("pid") if holder else None
        where = f" (pid {pid})" if pid else ""
        super().__init__(
            f"Session {session_id[:12]}... is already open in another "
            f"process{where}."
        )


# ======================================================================
# SessionLock
# ======================================================================


@dataclass
class SessionLock:
    """One held claim on a session — release it when the session is done."""

    session_id: str
    path: Path
    _fd: int = field(repr=False)

    def release(self) -> None:
        """Drop the claim and delete the status file.

        The file is unlinked while the lock is still held.  A process that
        opened the same path a moment earlier will see the inode changed
        (or the path gone) after it locks and retry against the fresh
        file, so this cannot hand the session to two processes.
        """
        fd, self._fd = self._fd, -1
        if fd < 0:
            return
        try:
            if _path_is_ours(self.path, fd):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
        finally:
            # Closing drops the flock.  Unlinking first means the next
            # claim creates a fresh inode rather than locking a dead one.
            os.close(fd)


# ======================================================================
# SessionLockManager
# ======================================================================


class SessionLockManager:
    """Claims sessions through status files in one directory.

    Constructing the manager sweeps files no live process holds — the
    residue of processes that were killed before they could release.
    """

    def __init__(self, lock_dir: str | Path) -> None:
        self._dir = Path(lock_dir)
        self._sweep()

    def status_path(self, session_id: str) -> Path:
        """Where *session_id*'s claim file lives."""
        return self._dir / f"{session_id}.json"

    def try_acquire(self, session_id: str) -> SessionLock | None:
        """Claim *session_id*, or return *None* when another process holds it."""  # noqa: E501
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.status_path(session_id)
        for _ in range(_CLAIM_RETRIES):
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
            except OSError:
                os.close(fd)
                raise

            if not _path_is_ours(path, fd):
                # A releasing holder unlinked the file between our open
                # and flock; the path now names a newer inode (or none).
                os.close(fd)
                continue

            _write_status(fd, session_id)
            logger.debug(f"Claimed session {session_id[:12]}.")
            return SessionLock(session_id=session_id, path=path, _fd=fd)

        return None

    def acquire(self, session_id: str) -> SessionLock:
        """Claim *session_id*, raising :class:`SessionInUseError` if held."""
        lock = self.try_acquire(session_id)
        if lock is None:
            raise SessionInUseError(session_id, self.holder(session_id))
        return lock

    def holder(self, session_id: str) -> dict | None:
        """The status the claiming process wrote, for diagnostics."""
        try:
            text = self.status_path(session_id).read_text()
        except OSError:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _sweep(self) -> None:
        """Delete status files whose process is gone."""
        if not self._dir.is_dir():
            return
        for path in self._dir.glob("*.json"):
            try:
                fd = os.open(path, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)  # Held by a live process.
                continue
            try:
                if _path_is_ours(path, fd):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(path)
            finally:
                os.close(fd)


# ======================================================================
# Helpers
# ======================================================================


def _path_is_ours(path: Path, fd: int) -> bool:
    """Whether *path* still names the inode locked through *fd*."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    st_fd = os.fstat(fd)
    return (st.st_dev, st.st_ino) == (st_fd.st_dev, st_fd.st_ino)


def _write_status(fd: int, session_id: str) -> None:
    status = {
        "session_id": session_id,
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps(status).encode())
