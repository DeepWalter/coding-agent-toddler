"""Filesystem snapshot strategies — git-preferred with file-copy fallback.

Two strategies are provided:

* :class:`GitSnapshotter` — uses ``git stash create`` (dangling commit,
  does **not** touch the stash stack) for zero-cost deduplicated snapshots.
* :class:`FileSnapshotter` — copies tracked files into a checkpoint
  directory with SHA-256 manifests.  Slower and uses disk space, but works
  without git.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from toddler.checkpoint.models import FileManifestEntry
from toddler.config.defaults import CHECKPOINT_BASE_DIR, SESSION_DIR

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Size of each read chunk when computing SHA-256 hashes.
_HASH_CHUNK = 64 * 1024  # 64 KiB


# ======================================================================
# GitSnapshotter
# ======================================================================


class GitSnapshotter:
    """Snapshot strategy that uses ``git stash create`` for fast, cheap
    pre-mutation checkpoints.

    ``git stash create`` builds a dangling commit object from the current
    index + worktree and returns its hash — **without** pushing anything
    onto the stash stack.  This gives us the same content-addressed
    deduplication that git already provides, at near-zero cost for
    unchanged files.

    Parameters
    ----------
    repo_root:
        Absolute path to the git repository root.  All git commands are
        executed with this as the working directory.
    """

    def __init__(self, repo_root: str | Path) -> None:
        self._root = Path(repo_root).resolve()
        self._available: bool | None = None  # cached availability check

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """``True`` when git is installed and *repo_root* is a git repo."""
        if self._available is None:
            self._available = self._probe()
        return self._available

    async def create(self) -> str | None:
        """Stage all changes and create a dangling stash commit.

        Returns
        -------
        str | None
            The commit hash (40-char hex), or *None* if git is unavailable
            or the working tree has no changes to snapshot.
        """
        if not self.available:
            return None

        try:
            # Stage everything so the stash commit captures the full state.
            await _run(["git", "add", "-A"], cwd=self._root)

            # Create a dangling commit — does NOT touch the stash stack.
            proc = await _run(
                ["git", "stash", "create"],
                cwd=self._root,
                capture=True,
            )
            ref = proc.stdout.strip()
            if ref:
                logger.debug(f"Git stash commit created: {ref[:12]}")
                return ref
            else:
                logger.debug("git stash create returned empty — no changes.")
                return None
        except Exception:
            logger.exception("Git stash create failed — falling back.")
            return None

    async def restore(self, ref: str) -> list[str]:
        """Restore the working tree to the state captured in *ref*.

        Returns the list of file paths that were restored.
        """
        if not self.available:
            raise RuntimeError("Git is not available — cannot restore.")

        # Collect which files changed between the checkpoint and HEAD.
        before = await _list_tracked_files(self._root)

        # Checkout the checkpoint tree into the working directory.
        await _run(
            ["git", "checkout", ref, "--", "."],
            cwd=self._root,
        )

        # Remove untracked files that appeared after the checkpoint.
        await _run(["git", "clean", "-fd"], cwd=self._root)

        # Unstage everything (we only care about worktree state).
        await _run(["git", "restore", "--staged", "."], cwd=self._root)

        after = await _list_tracked_files(self._root)

        return sorted(set(before) | set(after))

    async def restore_to(
        self, ref: str, paths: list[str],
    ) -> list[str]:
        """Restore specific *paths* from *ref* into the working tree."""
        if not paths:
            return []

        await _run(
            ["git", "checkout", ref, "--", *paths],
            cwd=self._root,
        )
        return list(paths)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _probe(self) -> bool:
        """Check whether *repo_root* is inside a git working tree."""
        try:
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                check=True,
            )
            logger.debug(f"Git detected at {self._root}.")
            return True
        except Exception:
            logger.debug("Git not available — snapshots will use file-copy fallback.")
            return False


# ======================================================================
# FileSnapshotter
# ======================================================================


class FileSnapshotter:
    """Snapshot fallback that copies files into a checkpoint directory.

    Used when git is unavailable.  Stores a SHA-256 manifest alongside
    the copied files so rollback can verify integrity.

    Parameters
    ----------
    base_dir:
        Root directory for checkpoint storage.  Per-session directories are
        created underneath (``{base_dir}/{session_id}/{checkpoint_id}/``).
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is None:
            base_dir = SESSION_DIR / CHECKPOINT_BASE_DIR
        self._base = Path(base_dir).expanduser().resolve()

    def checkpoint_dir(self, session_id: str, checkpoint_id: str) -> Path:
        """Return the directory where files for *checkpoint_id* are stored."""
        return self._base / session_id / checkpoint_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(
        self,
        session_id: str,
        checkpoint_id: str,
        files: list[Path],
    ) -> list[FileManifestEntry]:
        """Copy *files* into the checkpoint directory and return a manifest.

        Parameters
        ----------
        session_id:
            Session that owns this checkpoint.
        checkpoint_id:
            UUID4 string identifying the checkpoint.
        files:
            Absolute paths to the files to snapshot.

        Returns
        -------
        list[FileManifestEntry]
            Manifest with path, sha256, and size for each file.
        """
        dest_dir = self.checkpoint_dir(session_id, checkpoint_id)
        dest_dir.mkdir(parents=True, exist_ok=True)

        manifest: list[FileManifestEntry] = []

        for src in files:
            if not src.is_file():
                continue

            sha = await _sha256_file(src)
            size = src.stat().st_size

            # Preserve relative path structure inside the checkpoint dir.
            try:
                rel = src.resolve().relative_to(Path.cwd())
            except ValueError:
                # File is outside cwd — use its absolute path segments.
                rel = src.resolve()

            dest = dest_dir / str(rel).replace("/", "_").replace("\\", "_")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

            manifest.append(
                FileManifestEntry(
                    path=str(src),
                    sha256=sha,
                    size=size,
                )
            )

        logger.debug(
            f"File snapshot created: {len(manifest)} files → {dest_dir}"
        )
        return manifest

    async def restore(
        self,
        session_id: str,
        checkpoint_id: str,
        manifest: list[FileManifestEntry],
    ) -> list[str]:
        """Copy files from the checkpoint directory back to their original
        locations, verifying SHA-256 hashes.

        Returns the list of restored file paths.
        """
        src_dir = self.checkpoint_dir(session_id, checkpoint_id)
        restored: list[str] = []

        for entry in manifest:
            src_path = Path(str(entry.path))
            rel = src_path.resolve().relative_to(Path.cwd()) if src_path.is_absolute() else src_path
            safe_name = str(rel).replace("/", "_").replace("\\", "_")
            snapshot_file = src_dir / safe_name

            if not snapshot_file.exists():
                logger.warning(f"Snapshot file missing: {snapshot_file}")
                continue

            # Verify integrity.
            actual_sha = await _sha256_file(snapshot_file)
            if actual_sha != entry.sha256:
                logger.warning(
                    f"Checksum mismatch for {entry.path} — skipping restore."
                )
                continue

            src_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot_file, src_path)
            restored.append(entry.path)

        logger.debug(f"File snapshot restored: {len(restored)} files.")
        return restored

    async def cleanup(
        self, session_id: str, checkpoint_id: str,
    ) -> bool:
        """Delete the checkpoint directory and its contents.

        Returns ``True`` if the directory was removed.
        """
        target = self.checkpoint_dir(session_id, checkpoint_id)
        if target.exists():
            shutil.rmtree(target)
            logger.debug(f"Cleaned up snapshot dir: {target}")
            return True
        return False


# ======================================================================
# Internal helpers
# ======================================================================


async def _run(
    args: list[str],
    *,
    cwd: Path,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess asynchronously via``asyncio.to_thread``."""
    import asyncio

    return await asyncio.to_thread(
        subprocess.run,
        args,
        cwd=str(cwd),
        capture_output=capture,
        text=True,
        check=True,
    )


async def _list_tracked_files(root: Path) -> list[str]:
    """Return a list of paths tracked by git in *root*."""
    try:
        proc = await _run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture=True,
        )
        return [line for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        return []


async def _sha256_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of *path*."""
    import asyncio

    def _compute() -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(_HASH_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    return await asyncio.to_thread(_compute)
