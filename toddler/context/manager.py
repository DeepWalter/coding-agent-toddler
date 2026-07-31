"""ContextManager — pure in-memory message buffer for LLM conversations.

Provides a single-responsibility message buffer that prepares messages for
LLM API requests.  It knows nothing about storage, sessions, or persistence
— those concerns belong to the session layer (:class:`SessionCoordinator`).

Wires together three sub-components:

- :class:`SystemPromptBuilder` — assembles layered system prompts
- :class:`ContextWindowManager` — token counting and compaction/truncation triggers
- :class:`ConversationCompactor` — LLM-powered summarisation of old turns
"""  # noqa: E501

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from toddler.llm import Message

if TYPE_CHECKING:
    from toddler.context.builder import SystemPromptBuilder
    from toddler.context.summarizer import ConversationCompactor
    from toddler.context.window import ContextWindowManager

logger = logging.getLogger(__name__)


# ======================================================================
# Result dataclasses
# ======================================================================


@dataclass
class CompactionResult:
    """Metadata returned after a successful compaction.

    Attributes
    ----------
    summary:
        The extracted compaction summary text (the ``[Compacted history...]``
        content).
    messages_before:
        Total message count before compaction.
    messages_after:
        Total message count after compaction.
    token_count_before:
        Token count before compaction.
    token_count_after:
        Token count after compaction.
    """

    summary: str
    messages_before: int
    messages_after: int
    token_count_before: int = 0
    token_count_after: int = 0


# ======================================================================
# ContextManager
# ======================================================================


class ContextManager:
    """Pure in-memory message buffer for LLM conversations.

    A single instance lives for the lifetime of the REPL.  It holds the
    shared sub-components (SystemPromptBuilder, ContextWindowManager,
    ConversationCompactor) and is reset between conversations via
    :meth:`load`.

    Holds messages across turns — the session layer is responsible for
    loading initial messages and persisting new ones.  The context tracks
    a *baseline count* so the session layer can discover what's new
    (``new_message_count``) and call :meth:`acknowledge` after persisting.

    Wires together the three context-management sub-components so
    AgentLoop only deals with ONE object.
    """

    def __init__(
        self,
        prompt_builder: SystemPromptBuilder,
        *,
        window_mgr: ContextWindowManager | None = None,
        compactor: ConversationCompactor | None = None,
    ) -> None:
        # Shared sub-components — never change across conversations.
        self._prompt_builder = prompt_builder
        self._window_mgr = window_mgr
        self._compactor = compactor

        # Message buffer state — reset on each load().
        self._messages: list[Message] = []
        self._baseline_count: int = 0
        self._has_compacted: bool = False

        # Most-recent compaction result (cleared on load).
        self._last_compaction: CompactionResult | None = None

        # Cross-conversation context — set by the session layer before
        # the first prepare_turn() of a turn, consumed by prepare_turn().
        self._prior_titles: list[str] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load(self, messages: list[Message]) -> None:
        """Replace the buffer with pre-loaded *messages*.

        Called by the session layer after loading messages from storage
        (including any synthetic compaction-summary message).  Resets
        compaction state and sets the baseline to ``len(messages)`` so
        that :meth:`new_message_count` starts at zero.
        """
        self._messages = list(messages)
        self._baseline_count = len(messages)
        self._has_compacted = False
        self._last_compaction = None

    def set_cross_conversation_context(
        self, prior_titles: list[str] | None,
    ) -> None:
        """Set prior conversation titles for the next system prompt build.

        Called by the session layer once per turn before
        :meth:`prepare_turn`.  The value is consumed (cleared to *None*)
        when the system prompt is built on the first turn of a
        conversation.
        """
        self._prior_titles = prior_titles

    # ------------------------------------------------------------------
    # Turn preparation
    # ------------------------------------------------------------------

    async def prepare_turn(
        self,
        user_input: str,
        mode: str = "execute",
    ) -> None:
        """Prepare the message list for a new agent turn.

        On the first turn (empty buffer): builds the system prompt (with
        cross-conversation summaries from
        :meth:`set_cross_conversation_context`), then appends *user_input*.

        On subsequent turns: appends *user_input* to the existing history.

        After this returns, :attr:`messages` is ready for LLM calls and
        :meth:`append` can be used to add assistant/tool messages.
        """
        if not self._messages:
            # Fresh conversation — build system prompt from scratch.
            sys_text = self._prompt_builder.build(
                mode,
                prior_conversation_summaries=self._prior_titles,
            )
            self._messages = [Message.system(sys_text)]
            self._prior_titles = None  # consumed

        self._messages.append(Message.user(user_input))

    # ------------------------------------------------------------------
    # Context window management
    # ------------------------------------------------------------------

    async def _auto_compact(self) -> CompactionResult | None:
        """Check token usage and trigger compaction or truncation if needed.

        Called internally before each turn.  Returns a
        :class:`CompactionResult` if compaction occurred (the session
        layer uses this to persist ``compacted_summary`` /
        ``compacted_at_seq``), or ``None`` if no compaction was needed.
        """
        if self._window_mgr is None:
            return None

        token_count = self._window_mgr.count_tokens(self._messages)
        logger.info(
            f"Context: {self._window_mgr.status_line(self._messages)}"
        )

        # --- compaction ---
        if (
            self._compactor is not None
            and self._window_mgr.should_compact(self._messages)
        ):
            logger.warning(
                f"Compaction triggered. "
                f"Compacting {len(self._messages)} messages..."
            )
            try:
                compacted = await self._compactor.compact(self._messages)
                before = token_count
                after = self._window_mgr.count_tokens(compacted)

                # Extract summary text from the compacted list.
                summary = self._extract_summary(compacted)

                result = CompactionResult(
                    summary=summary,
                    messages_before=len(self._messages),
                    messages_after=len(compacted),
                    token_count_before=before,
                    token_count_after=after,
                )

                # Apply compaction in-place.
                self._messages.clear()
                self._messages.extend(compacted)

                # Rebuild system prompt with compact variant.
                compact_sys = self._prompt_builder.build_compact()
                self._replace_system_messages(compact_sys)

                # Reset baseline — the compacted list is now the canonical
                # buffer, and new_messages / new_message_count should only
                # reflect additions made after this point.
                self._baseline_count = len(self._messages)

                self._has_compacted = True
                self._last_compaction = result
                logger.warning(
                    f"Compaction complete: {before:,} → {after:,} tokens "
                    f"({len(compacted)} messages)."
                )
                return result

            except Exception:
                logger.exception(
                    "Compaction failed — continuing with original messages."
                )
                return None

        # --- truncation (emergency brake) ---
        if self._window_mgr.should_truncate(self._messages):
            before = token_count
            truncated = self._window_mgr.truncate(self._messages)
            after = self._window_mgr.count_tokens(truncated)
            self._messages.clear()
            self._messages.extend(truncated)
            self._baseline_count = len(self._messages)
            logger.error(
                f"EMERGENCY TRUNCATION: {before:,} → {after:,} tokens."
            )

        return None

    # ------------------------------------------------------------------
    # Compaction metadata
    # ------------------------------------------------------------------

    @property
    def last_compaction(self) -> CompactionResult | None:
        """The most recent compaction result, or *None*.

        The session layer reads this after each turn to persist
        ``compacted_summary`` and ``compacted_at_seq`` on the conversation
        row.
        """
        return self._last_compaction

    def clear_compaction_result(self) -> None:
        """Clear the stored compaction result.

        Called by the session layer after it has persisted the compaction
        metadata, so the same compaction isn't applied twice.
        """
        self._last_compaction = None

    # ------------------------------------------------------------------
    # Direct access
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        """The active message list (read-only access, no auto-compaction).

        For LLM calls, use :meth:`get_messages` instead — it auto-compacts
        before returning the list.
        """
        return self._messages

    async def get_messages(self) -> list[Message]:
        """Return the message list, auto-compacting if needed.

        Call this before each LLM API call to ensure the context fits
        within the model's window.  The returned list is the same mutable
        object as :attr:`messages` — mutations affect the buffer.
        """
        await self._auto_compact()
        return self._messages

    @property
    def has_compacted(self) -> bool:
        """Whether compaction has occurred since the last :meth:`load`."""
        return self._has_compacted

    def append(self, msg: Message) -> None:
        """Append a message in-memory (for tool results, feedback, etc.)."""
        self._messages.append(msg)

    # ------------------------------------------------------------------
    # Persistence coordination
    # ------------------------------------------------------------------

    @property
    def new_message_count(self) -> int:
        """Number of messages added since the last :meth:`load` or
        :meth:`acknowledge`.  The session layer uses this to discover
        what needs persisting."""
        return max(0, len(self._messages) - self._baseline_count)

    @property
    def new_messages(self) -> list[Message]:
        """Messages added since the last :meth:`load` or
        :meth:`acknowledge`.  The session layer persists these and then
        calls :meth:`acknowledge`."""
        return self._messages[self._baseline_count:]

    def acknowledge(self) -> None:
        """Mark all current messages as baseline.

        Called by the session layer after persisting new messages.
        After this, :attr:`new_message_count` returns 0 until more
        messages are appended.
        """
        self._baseline_count = len(self._messages)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_summary(compacted: list[Message]) -> str:
        """Pull the compaction summary text out of the compacted list."""
        for msg in compacted:
            if msg.role == "user" and msg.content:
                text = msg.text
                if text.startswith("[Compacted"):
                    return text
        return ""

    def _replace_system_messages(self, new_sys_text: str) -> None:
        """Replace leading system message(s) with a single new one."""
        cut = 0
        for i, m in enumerate(self._messages):
            if m.role == "system":
                cut = i + 1
            else:
                break
        new_sys = Message.system(new_sys_text)
        self._messages[:cut] = [new_sys]
