"""Context management — window tracking, compaction, conversation lifecycle,
project mapping, and persistent memory.

Phase 7: Context Management
"""

from __future__ import annotations

from toddler.context.compaction import ConversationCompactor
from toddler.context.conversation_context import ConversationContext
from toddler.context.memory import PersistentMemory
from toddler.context.project_map import ProjectMapper
from toddler.context.system_prompt import SystemPromptBuilder
from toddler.context.window import ContextWindowManager

__all__ = [
    "ContextWindowManager",
    "ConversationCompactor",
    "ConversationContext",
    "PersistentMemory",
    "ProjectMapper",
    "SystemPromptBuilder",
]
