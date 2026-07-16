"""Checkpoint subsystem — pre-mutation snapshots with rollback capability.

Public API
----------
- :class:`Checkpoint` — pre-mutation snapshot record
- :class:`AgentStateSnapshot` — agent loop state captured at checkpoint time
- :class:`RollbackResult` — outcome of a rollback operation
- :class:`FileManifestEntry` — individual file in a file-copy snapshot
- :class:`CheckpointManager` — create, rollback, list, prune
- :class:`GitSnapshotter` — git-based snapshot strategy (preferred)
- :class:`FileSnapshotter` — file-copy snapshot fallback
"""

from toddler.checkpoint.manager import CheckpointManager
from toddler.checkpoint.models import (
    AgentStateSnapshot,
    Checkpoint,
    FileManifestEntry,
    RollbackResult,
)
from toddler.checkpoint.snapshot import FileSnapshotter, GitSnapshotter

__all__ = [
    "AgentStateSnapshot",
    "Checkpoint",
    "CheckpointManager",
    "FileManifestEntry",
    "FileSnapshotter",
    "GitSnapshotter",
    "RollbackResult",
]
