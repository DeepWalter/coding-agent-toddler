"""Conversation compactor — LLM-powered summarisation of old turns.

When the context window fills up, this module summarises older messages into
a compact form that preserves the essential context (decisions, findings,
file modifications) while freeing up tokens for the active conversation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from toddler.llm.types import Message

if TYPE_CHECKING:
    from toddler.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Number of most-recent messages to keep intact (not summarised).
_DEFAULT_KEEP_RECENT = 12

# Maximum tokens the compaction LLM call is allowed to produce.
_DEFAULT_MAX_SUMMARY_TOKENS = 1024

# ---------------------------------------------------------------------------
# Compaction prompt template
# ---------------------------------------------------------------------------

_COMPACTION_PROMPT = """\
Summarise the following conversation excerpt into a compact bullet-point \
summary for the system prompt.  Preserve:

- **Key decisions** the user and assistant agreed on.
- **Files** that were read, created, or modified.
- **Important findings** (bugs discovered, architectural notes, test results).
- **Ongoing tasks** — what is still in progress or needs follow-up.
- **User preferences** or explicit instructions.

Exclude:
- Chitchat, greetings, or filler.
- Verbatim tool calls or raw file contents (just note *which* files were touched).
- Transient error messages that were resolved.

Format: use markdown bullet points.  Keep the summary under 500 words.

---

{conversation}
---

Summary:"""


# ======================================================================
# ConversationCompactor
# ======================================================================


class ConversationCompactor:
    """Summarises old conversation turns via a separate LLM call.

    Parameters
    ----------
    llm_provider:
        The LLM backend, used via :meth:`~BaseLLMProvider.generate_compact`
        for the summarisation call.
    keep_recent:
        Number of most-recent messages to keep intact.  Messages before
        this window (excluding the system prompt) are compacted.
    """

    def __init__(
        self,
        llm_provider: BaseLLMProvider,
        *,
        keep_recent: int = _DEFAULT_KEEP_RECENT,
    ) -> None:
        self._llm = llm_provider
        self._keep_recent = keep_recent

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compact(self, messages: list[Message]) -> list[Message]:
        """Summarise old turns and return a compacted message list.

        The returned list has the following structure:

        1. The original system message (unchanged).
        2. A new system-like message containing the compaction summary.
        3. The *keep_recent* most-recent messages (unchanged).

        When the conversation is short enough that no compaction is
        needed, *messages* is returned unchanged.

        Parameters
        ----------
        messages:
            The full conversation history.

        Returns
        -------
        list[Message]
            The compacted conversation (system + summary + recent).
        """
        if not messages:
            return messages

        # Separate system message(s) from the body.
        system_msgs: list[Message] = []
        body_start = 0
        for i, msg in enumerate(messages):
            if msg.role == "system":
                system_msgs.append(msg)
                body_start = i + 1
            else:
                break

        body = messages[body_start:]

        # Not enough messages to compact.
        if len(body) <= self._keep_recent:
            logger.debug(
                f"Skipping compaction: {len(body)} messages ≤ "
                f"keep_recent={self._keep_recent}"
            )
            return messages

        # Split: oldest messages to summarise, most-recent to keep.
        to_summarise = body[: -self._keep_recent]
        to_keep = body[-self._keep_recent:]

        if not to_summarise:
            return messages

        logger.info(
            f"Compacting {len(to_summarise)} messages; "
            f"keeping {len(to_keep)} recent."
        )

        # Format the summarisation prompt.
        conversation_text = self._format_conversation(to_summarise)
        prompt = _COMPACTION_PROMPT.format(conversation=conversation_text)

        # Call the LLM for summarisation.
        try:
            summary = await self._llm.generate_compact(prompt)
        except Exception:
            logger.exception("Compaction LLM call failed — keeping original messages.")
            return messages

        if not summary.strip():
            logger.warning("Compaction produced empty summary — keeping original messages.")
            return messages

        # Build the compacted message list.
        compacted: list[Message] = list(system_msgs)
        compacted.append(self._make_summary_message(summary.strip()))
        compacted.extend(to_keep)

        logger.debug(
            f"Compacted {len(messages)} → {len(compacted)} messages "
            f"(summary: {len(summary)} chars)."
        )

        return compacted

    async def compact_with_checkpoint(
        self, messages: list[Message]
    ) -> tuple[list[Message], str | None]:
        """Like :meth:`compact` but also returns a checkpoint summary.

        The checkpoint summary is a compact single-paragraph recap suitable
        for persisting alongside a session (Phase 8+).

        Returns
        -------
        tuple[list[Message], str | None]
            The compacted message list and a short checkpoint summary
            (``None`` when no compaction was needed).
        """
        compacted = await self.compact(messages)

        # If no change, no checkpoint summary.
        if compacted is messages:
            return compacted, None

        # The summary is already in the second message (after system).
        for msg in compacted:
            if msg.role == "system" and msg.content:
                # Skip original system messages.
                continue
            if msg.role == "user" and msg.content:
                text = msg.text
                if text.startswith("[Compacted"):
                    return compacted, text
                break

        return compacted, None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_conversation(messages: list[Message]) -> str:
        """Convert a list of messages into a readable text format."""

        lines: list[str] = []
        for msg in messages:
            role = msg.role
            for block in msg.content:
                if block.type == "text" and block.text:
                    lines.append(f"[{role}]: {block.text}")
                elif block.type == "tool_use":
                    tool_name = block.tool_name or "unknown"
                    lines.append(
                        f"[{role} → tool_call]: {tool_name}(...)"
                    )
                elif block.type == "tool_result":
                    text = block.tool_result_content or ""
                    # Truncate very long tool results.
                    if len(text) > 500:
                        text = text[:497] + "..."
                    lines.append(f"[tool_result]: {text}")

        return "\n".join(lines)

    @staticmethod
    def _make_summary_message(summary: str) -> Message:
        """Wrap the compaction *summary* as a system-like message.

        We use the ``user`` role for the summary so it appears as a
        conversation artifact rather than a system directive, but we prefix
        it with a clear marker so the LLM knows it is historical context.
        """
        text = (
            "[Compacted history — summary of the conversation so far]\n\n"
            + summary
        )
        return Message.user(text)
