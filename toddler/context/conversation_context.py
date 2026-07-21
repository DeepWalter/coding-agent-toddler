"""ConversationContext — in-memory buffer and management orchestrator.

Wires together the three context-management components
(:class:`SystemPromptBuilder`, :class:`ContextWindowManager`,
:class:`ConversationCompactor`) and delegates persistence to
:class:`StorageManager`.  A single instance lives for the lifetime of the
REPL — it switches between conversations via :meth:`activate` while holding
messages across turns so there is no DB reload per turn.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from toddler.llm.types import Message

if TYPE_CHECKING:
    from toddler.context.compaction import ConversationCompactor
    from toddler.context.system_prompt import SystemPromptBuilder
    from toddler.context.window import ContextWindowManager
    from toddler.session.manager import StorageManager
    from toddler.session.models import Conversation

logger = logging.getLogger(__name__)


class ConversationContext:
    """In-memory buffer and management orchestrator for a conversation.

    A single instance lives for the lifetime of the REPL.  It holds the
    shared collaborators (StorageManager, SystemPromptBuilder,
    ContextWindowManager, ConversationCompactor) and switches between
    conversations via :meth:`activate`.

    Holds messages across turns — no DB reload per turn.  Syncs deltas to
    DB on save().

    Wires together the three context-management components so AgentLoop
    only deals with ONE object instead of four.
    """

    def __init__(
        self,
        storage_mgr: StorageManager | None,
        prompt_builder: SystemPromptBuilder,
        *,
        window_mgr: ContextWindowManager | None = None,
        compactor: ConversationCompactor | None = None,
    ) -> None:
        # Shared collaborators — never change across conversations.
        self._mgr = storage_mgr
        self._prompt_builder = prompt_builder
        self._window_mgr = window_mgr
        self._compactor = compactor

        # Per-conversation state — reset on each activate().
        self._conv: Conversation | None = None
        self._messages: list[Message] = []
        self._base_seq: int = 0        # min sequence_num from last load
        self._loaded = False
        self._persisted_count = 0
        self._has_compacted = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def activate(self, conversation: Conversation) -> None:
        """Save current conversation (if any) and switch to *conversation*.

        Call on startup, ``/clear``, and ``/resume``.  After this returns,
        :attr:`messages` contains the loaded history (or is ready for a
        fresh first turn).
        """
        if self._conv is not None:
            await self.save()

        self._conv = conversation
        self._messages.clear()
        self._base_seq = 0
        self._loaded = False
        self._persisted_count = 0
        self._has_compacted = False

        await self._load()

    async def start_fresh(self, session_id: str) -> None:
        """Create a new active conversation and activate it."""
        if self._mgr is None:
            return
        conv = await self._mgr.get_or_create_active_conversation(session_id)
        await self.activate(conv)

    async def _load(self) -> None:
        """Load active context from DB.  Called once per activation."""
        if self._mgr is None:
            self._loaded = True
            return

        after_seq = self._conv.compacted_at_seq or -1
        recent = await self._mgr.get_messages(
            session_id=self._conv.session_id,
            conversation_id=self._conv.id,
            after_sequence=after_seq,
        )

        # The first non-compacted message in the conversation has
        # sequence_num = after_seq + 1 (or 0 if never compacted).
        self._base_seq = after_seq + 1
        self._messages = []

        # Persisted count tracks how many message-list positions
        # correspond to already-persisted content.  The synthetic
        # summary is stored on the conversation row (not the messages
        # table), so we count it as "already persisted" to prevent
        # save() from trying to write it as a message row.
        self._persisted_count = 0

        if self._conv.compacted_summary:
            self._messages.append(
                Message.user(
                    "[Compacted history — summary of the conversation"
                    " so far]\n\n"
                    + self._conv.compacted_summary
                )
            )
            self._persisted_count = 1  # synthetic — lives on conversation row

        self._messages.extend(recent)
        self._persisted_count += len(recent)
        self._loaded = True

    async def save(self) -> None:
        """Persist new messages to DB and update conversation metadata.

        Only persists messages added since the last save() or load().
        The synthetic compaction-summary message (if present) is already
        accounted for in ``_persisted_count`` and is never written to the
        messages table — its content lives on
        ``conversation.compacted_summary``.

        When ``_mgr`` is *None* (no-persistence mode), this is a no-op.
        """
        if self._mgr is None or self._conv is None:
            return

        new_msgs = self._messages[self._persisted_count:]
        for msg in new_msgs:
            await self._mgr.append_message(
                self._conv.session_id,
                msg,
                conversation_id=self._conv.id,
            )
        self._persisted_count = len(self._messages)

        # append_message already updates the conversation row's message_count
        # in the DB.  Sync it back into the in-memory model so the
        # update_conversation call below doesn't overwrite with a stale value.
        self._conv.message_count += len(new_msgs)

        # Persist conversation metadata (title, counters, compaction pointer).
        await self._mgr.update_conversation(self._conv)

    # ------------------------------------------------------------------
    # Turn preparation (replaces AgentLoop's inline message building)
    # ------------------------------------------------------------------

    async def prepare_turn(self, user_input: str, mode: str = "execute") -> list[Message]:  # noqa: E501
        """Prepare the message list for a new agent turn.

        On first turn: builds system prompt (with cross-conversation
        summaries), auto-titles the conversation from *user_input* if no
        title is set, then appends user_input.
        On subsequent turns: appends user_input to existing history.

        Returns the mutable message list — AgentLoop can modify it in-place
        (appending assistant responses, tool results, etc.).
        """
        if not self._messages:
            # Collect prior conversation titles for context.
            prior_titles: list[str] | None = None
            if self._mgr is not None and self._conv is not None:
                summaries = await self._mgr.get_conversation_summaries(
                    self._conv.session_id,
                    exclude_id=self._conv.id,
                )
                if summaries:
                    prior_titles = [title for _, title in summaries]

            # Fresh conversation — build system prompt from scratch.
            sys_text = self._prompt_builder.build(
                mode, prior_conversation_summaries=prior_titles,
            )
            self._messages = [Message.system(sys_text)]
            self._maybe_auto_title(user_input)

        self._messages.append(Message.user(user_input))
        return self._messages

    # ------------------------------------------------------------------
    # Titling
    # ------------------------------------------------------------------

    def _maybe_auto_title(self, user_input: str) -> None:
        """Set conversation title from first user input if not already set."""
        if self._conv is not None and not self._conv.title:
            self._conv.title = user_input[:80]

    def set_title(self, title: str) -> None:
        """Explicitly set the conversation title (e.g. from /clear <title>)."""
        self._conv.title = title.strip() or None

    # ------------------------------------------------------------------
    # Context window management (moved from AgentLoop._check_context_window)
    # ------------------------------------------------------------------

    async def check_and_compact(self) -> bool:
        """Check token usage and trigger compaction or truncation if needed.

        Called before every LLM call.  Returns True if compaction occurred
        (so the caller knows to use compact prompt variants for subsequent
        turns).
        """
        if self._window_mgr is None:
            return False

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

                # Count how many original body messages were summarized.
                body_after = sum(
                    1 for m in compacted
                    if m.role != "system"
                    and not (m.role == "user" and m.content
                             and m.text.startswith("[Compacted"))
                )
                up_to_seq = self._compute_compacted_up_to(body_after)

                self.apply_compaction(compacted, summary, up_to_seq)

                # Rebuild system prompt with compact variant.
                compact_sys = self._prompt_builder.build_compact()
                self._replace_system_messages(compact_sys)

                self._has_compacted = True
                logger.warning(
                    f"Compaction complete: {before:,} → {after:,} tokens "
                    f"({len(compacted)} messages)."
                )
                return True

            except Exception:
                logger.exception(
                    "Compaction failed — continuing with original messages."
                )
                return False

        # --- truncation (emergency brake) ---
        if self._window_mgr.should_truncate(self._messages):
            before = token_count
            truncated = self._window_mgr.truncate(self._messages)
            after = self._window_mgr.count_tokens(truncated)
            self._messages.clear()
            self._messages.extend(truncated)
            logger.error(
                f"EMERGENCY TRUNCATION: {before:,} → {after:,} tokens."
            )

        return False

    # ------------------------------------------------------------------
    # Compaction application
    # ------------------------------------------------------------------

    def apply_compaction(
        self,
        compacted: list[Message],
        summary: str,
        up_to_seq: int,
    ) -> None:
        """Replace in-memory messages with compacted version.

        Mutates ``_messages`` **in-place** (``clear()`` + ``extend()``)
        so that any external references to the list (e.g. a local variable
        in AgentLoop.run()) remain valid.

        Updates the conversation metadata so the compaction survives
        restarts.  Does NOT reset ``_persisted_count`` — the recent
        messages were already persisted in earlier save() calls and the
        summary is stored on the conversation row (not as a message row).
        Only genuinely new messages from future turns will be persisted.
        """
        self._messages.clear()
        self._messages.extend(compacted)
        self._conv.compacted_summary = summary
        self._conv.compacted_at_seq = up_to_seq

    # ------------------------------------------------------------------
    # Direct access
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        """The active message list (mutated in-place by AgentLoop)."""
        return self._messages

    @property
    def has_compacted(self) -> bool:
        """Whether compaction has occurred in this conversation."""
        return self._has_compacted

    def append(self, msg: Message) -> None:
        """Append a message in-memory (for tool results, etc.)."""
        self._messages.append(msg)

    @property
    def conversation_id(self) -> str:
        return self._conv.id if self._conv else ""

    @property
    def conversation(self) -> Conversation | None:
        return self._conv

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

    def _compute_compacted_up_to(self, kept_count: int) -> int:
        """Return the sequence_num of the last message covered by the summary.

        ``kept_count`` is the number of body messages the compactor
        preserved (passed in by ``check_and_compact``).  Everything before
        those was summarized.

        Uses ``_base_seq`` (the sequence_num of the first non-compacted
        message at load time).
        """
        body_before = sum(1 for m in self._messages if m.role != "system")
        summarized = max(0, body_before - kept_count)
        if summarized <= 0:
            return self._conv.compacted_at_seq or self._base_seq - 1
        return self._base_seq + summarized - 1

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
