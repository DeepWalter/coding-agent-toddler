"""Context management — window tracking, compaction, conversation lifecycle,
project mapping, and persistent memory.

Phase 7: Context Management
"""

from __future__ import annotations

from toddler.context.builder import SystemPromptBuilder
from toddler.context.manager import CompactionResult, ContextManager
from toddler.context.memory import PersistentMemory
from toddler.context.summarizer import ConversationCompactor
from toddler.context.token_counter import TokenCounter
from toddler.context.window import ContextWindowManager
from toddler.context.workspace import ProjectMapper

__all__ = [
    "CompactionResult",
    "ContextManager",
    "ContextWindowManager",
    "ConversationCompactor",
    "PersistentMemory",
    "ProjectMapper",
    "SystemPromptBuilder",
    "TokenCounter",
]
