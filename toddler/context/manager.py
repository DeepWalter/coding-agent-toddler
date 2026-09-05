"""ContextManager — pure in-memory message buffer for LLM conversations.

Provides a single-responsibility message buffer that prepares messages for
LLM API requests.  It knows nothing about storage, sessions, or persistence
— those concerns belong to the session layer (:class:`SessionManager`).

Wires together three sub-components:

- :class:`SystemPromptBuilder` — assembles layered system prompts
- :class:`ContextWindowManager` — token counting and compaction/truncation triggers
- :class:`ConversationCompactor` — LLM-powered summarisation of old turns
"""  # noqa: E501

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from toddler.config.settings import Settings
from toddler.context.builder import SystemPromptBuilder
from toddler.context.summarizer import ConversationCompactor
from toddler.context.window import ContextWindowManager
from toddler.llm import BaseLLMProvider, ContentBlock, Message, TokenUsage

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

    A single instance lives for the lifetime of the REPL.  It owns the
    shared sub-components (SystemPromptBuilder, ContextWindowManager,
    ConversationCompactor) — building them internally from the raw
    ingredients — and is reset between conversations via :meth:`load`.

    Holds messages across turns — the session layer is responsible for
    loading initial messages and persisting new ones.  The context tracks
    a *baseline count* so the session layer can discover what's new
    (``new_message_count``) and call :meth:`acknowledge` after persisting.

    Wires together the three context-management sub-components so
    AgentLoop only deals with ONE object.
    """

    def __init__(
        self,
        settings: Settings,
        llm_provider: BaseLLMProvider,
        *,
        project_root: Path | None = None,
        memory_dir: Path | None = None,
    ) -> None:
        # Build sub-components internally from raw ingredients.
        self._prompt_builder = SystemPromptBuilder(
            project_root=project_root,
            memory_dir=memory_dir,
        )
        self._window_mgr = ContextWindowManager(
            llm_provider.model,
            max_context_length=settings.max_context_length,
        )
        self._compactor = ConversationCompactor(llm_provider)

        # Message buffer state — reset on each load().
        self._messages: list[Message] = []
        self._baseline_count: int = 0
        self._has_compacted: bool = False

        # Most-recent compaction result (cleared on load).
        self._last_compaction: CompactionResult | None = None

        # Current mode — set by prepare_turn(), used by _auto_compact()
        # so the compact system prompt keeps the right instructions.
        self._mode: str = "execute"

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
        self._window_mgr.reset_baseline()

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
    # Token tracking
    # ------------------------------------------------------------------

    def record_usage(self, usage: TokenUsage) -> None:
        """Feed API-reported token counts back into the window manager.

        Must be called AFTER the assistant message is appended so that
        ``usage.total`` covers every message currently in the buffer.
        """
        if usage.total > 0:
            self._window_mgr.set_baseline(
                total_tokens=usage.total,
                message_count=len(self._messages),
            )

    def count_tokens(self) -> int:
        """Return the current token count of the in-memory buffer.

        Uses the window manager's baseline + tiktoken delta — exact for the
        persisted message list when the baseline was seeded from storage.
        """
        return self._window_mgr.count_tokens(self._messages)

    @property
    def usage_ratio(self) -> float:
        """Current context usage as a fraction of the effective limit (0.0–1.0+)."""  # noqa: E501
        return self._window_mgr.usage_ratio(self._messages)

    def set_token_baseline(
        self, *, total_tokens: int, message_count: int,
    ) -> None:
        """Seed the window-manager baseline from persisted conversation data.

        Called by the session layer immediately after :meth:`load` when the
        stored ``total_tokens`` is usable (nonzero and the model matches),
        so the first ``count_tokens`` call doesn't need a full tiktoken
        estimate.
        """
        self._window_mgr.set_baseline(
            total_tokens=total_tokens, message_count=message_count,
        )

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
        self._mode = mode

        if not self._messages:
            # Fresh conversation — build system prompt from scratch.
            sys_text = self._prompt_builder.build(
                mode,
                prior_conversation_summaries=self._prior_titles,
            )
            self._messages = [Message.system(sys_text)]

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
        token_count = self._window_mgr.count_tokens(self._messages)
        logger.info(
            f"Context: {self._window_mgr.status_line(self._messages)}"
        )

        result = None
        if self._window_mgr.should_compact(self._messages):
            logger.warning(
                f"Compaction triggered. "
                f"Compacting {len(self._messages)} messages..."
            )
            result = await self.compact()
        if result is not None:
            return result

        # --- truncation (emergency brake) ---
        if self._window_mgr.should_truncate(self._messages):
            before = token_count
            truncated = self._window_mgr.truncate(self._messages)
            after = self._window_mgr.count_tokens(truncated)
            self._messages.clear()
            self._messages.extend(truncated)
            self._baseline_count = len(self._messages)
            self._window_mgr.reset_baseline()
            logger.error(
                f"EMERGENCY TRUNCATION: {before:,} → {after:,} tokens."
            )

        return None

    async def compact(self) -> CompactionResult | None:
        """Summarise older messages and fold the buffer when a summary is
        produced.

        Runs the compaction routine directly, without regard to the
        auto-compaction token threshold — callers decide when it is
        warranted (the auto path gates on usage in
        :meth:`_auto_compact`; manual compaction arrives through the
        session layer's ``compact_context``).  Summarises everything
        older than the compactor's keep-recent window and, when a
        summary was actually produced, replaces the buffer in place
        (new compact system prompt, baseline reset) and records a
        :class:`CompactionResult`.

        Returns *None* — leaving the buffer untouched — when the
        conversation was too short to summarise or the summarisation
        failed / produced empty output.  The compactor never mutates
        its input and returns the identical list object on every skip
        path, so the identity check below reliably detects a no-op.
        """
        try:
            compacted = await self._compactor.compact(self._messages)

            # Nothing was summarised (short conversation or LLM failure /
            # empty output) — do not record an empty compaction or swap
            # in the compact system prompt for nothing.
            if compacted is self._messages:
                return None
            summary = self._extract_summary(compacted)
            if not summary:
                return None

            # Everything fallible happens before the buffer swap below,
            # so an exception (or interrupt) here leaves the original
            # messages untouched and the except clause can honour its
            # "continuing with original messages" promise.
            before = self._window_mgr.count_tokens(self._messages)
            after = self._window_mgr.count_tokens(compacted)

            # Rebuild system prompt with compact variant,
            # preserving the current mode's instructions.
            compact_sys = self._prompt_builder.build_compact(
                mode=self._mode,
                prior_conversation_summaries=self._prior_titles,
            )

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
            self._replace_system_messages(compact_sys)

            # Reset baseline — the compacted list is now the canonical
            # buffer, and new_messages / new_message_count should only
            # reflect additions made after this point.
            self._baseline_count = len(self._messages)
            self._window_mgr.reset_baseline()

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

    def mark_turn_cancelled(self) -> None:
        """Repair the in-memory context after a cancelled turn.

        The accumulated messages stay — the next turn continues from
        them — but a cancel can leave assistant ``tool_use`` blocks
        with no matching ``tool_result`` (a paused approval, or a
        stream cut short mid-tool-call), which some providers reject.
        Answer each dangling tool call with a ``tool_result`` marked as
        an error saying the user cancelled it, then append a marker
        message so the model knows the previous turn was cut short, not
        finished.  Both are persisted immediately by the session
        layer's ``cancel_turn()`` — they survive a restart, not just
        the next turn's ``save()``.
        """
        if self._messages and self._messages[-1].role == "assistant":
            tool_uses = [
                b for b in self._messages[-1].content
                if b.type == "tool_use" and b.tool_id
            ]
            if tool_uses:
                self._messages.append(Message.tool([
                    ContentBlock.tool_result_block(
                        b.tool_id,
                        "The tool call was cancelled by the user before "
                        "it could run.",
                        is_error=True,
                    )
                    for b in tool_uses
                ]))
        self._messages.append(Message.user(
            "[The previous turn was cancelled by the user.]"
        ))

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
