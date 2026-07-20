"""System prompt assembly — layered prompt built from persona, project map,
persistent memory, and mode-specific instructions.

Phase 7.2: The ``SystemPromptBuilder`` replaces the old flat
``_DEFAULT_SYSTEM_PROMPT`` constant.  It stitches together the four layers,
caching expensive operations (filesystem walk, memory load) so they are
computed once and reused across turns.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toddler.context.memory import PersistentMemory
    from toddler.context.project_map import ProjectMapper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base persona — the core identity of the agent
# ---------------------------------------------------------------------------

_BASE_PERSONA = """\
You are Toddler, a coding assistant that helps with software engineering tasks.

You have access to tools for reading/writing files, running shell commands,
searching code, and interacting with git. Use them to accomplish the user's
request efficiently.

Guidelines:
- Read files before editing them — never guess their contents.
- Use the most specific tool for the job.
- When editing files, match the surrounding code style exactly.
- Report what you did and why after making changes.
- If a tool returns an error, read the error message and adapt — don't
  retry the same failing call.
- If you're unsure about something, ask before acting."""

# ---------------------------------------------------------------------------
# Mode-specific instruction layers
# ---------------------------------------------------------------------------

_EXECUTING_INSTRUCTIONS = """\
## Current Mode: EXECUTE

Make changes to accomplish the user's request.  You have full access to all
tools — read files to understand context, then edit, run commands, or search
as needed.  Be concise and direct."""

_PLAN_EXPLORING_INSTRUCTIONS = """\
## Current Mode: PLAN (Research)

RESEARCH only.  DO NOT make any changes to files or run mutating commands.
Your goal is to understand the codebase and gather enough information to
propose a concrete, actionable plan.

- Read relevant files to understand the current implementation.
- Search for patterns, usages, and related code.
- Note which files will need to be modified and why.
- When you have enough context, signal that you are ready to present a plan."""

_PLAN_EXECUTING_INSTRUCTIONS = """\
## Current Mode: PLAN (Execute)

Follow the approved plan steps in order.  Report progress after each step.
If you encounter unexpected issues (files that don't exist, conflicting
changes, errors), pause and report them — do not silently deviate from
the plan.

- Execute one step at a time, in dependency order.
- After each step, briefly confirm what was done.
- If a step cannot be completed as planned, explain why and wait for guidance."""

# ---------------------------------------------------------------------------
# Compact variants — shorter versions for long conversations
# ---------------------------------------------------------------------------

_COMPACT_PERSONA = """\
You are Toddler, a coding assistant. You have tools for reading/writing files,
shell commands, code search, and git. Read before editing; match surrounding
style; report what you did; adapt on errors."""

_COMPACT_EXECUTING = "Mode: EXECUTE — make changes, be concise."
_COMPACT_PLAN_EXPLORING = "Mode: PLAN (Research) — READ ONLY, gather context."
_COMPACT_PLAN_EXECUTING = "Mode: PLAN (Execute) — follow plan steps in order."

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class SystemPromptBuilder:
    """Assembles the system prompt from layered components.

    The prompt is built from four layers stacked in order:

    1. **Base persona** — the agent's core identity and guidelines.
    2. **Project map** — structural overview (directory tree, imports, config).
    3. **Persistent memory** — user preferences saved across sessions.
    4. **Mode-specific instructions** — behaviour rules for the current state.

    Parameters
    ----------
    project_mapper:
        Optional :class:`~toddler.context.project_map.ProjectMapper` for
        generating the project structure overview.  When *None* the project
        map section is omitted.
    persistent_memory:
        Optional :class:`~toddler.context.memory.PersistentMemory` for
        user preferences.  When *None* the memory section is omitted.
    """

    def __init__(
        self,
        project_mapper: ProjectMapper | None = None,
        persistent_memory: PersistentMemory | None = None,
    ) -> None:
        self._mapper = project_mapper
        self._memory = persistent_memory

        # Caches — built once and reused.
        self._project_map_text: str | None = None
        self._project_map_compact_text: str | None = None
        self._memory_text: str | None = None
        self._memory_compact_text: str | None = None
        self._cache_loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        mode: str = "execute",
        *,
        prior_conversation_summaries: list[str] | None = None,
    ) -> str:
        """Assemble the full system prompt for *mode*.

        Parameters
        ----------
        mode:
            One of ``"execute"``, ``"plan_exploring"``, or
            ``"plan_executing"``.  Controls which mode-specific
            instructions are appended.
        prior_conversation_summaries:
            Optional list of ``"(title)"`` strings from earlier
            conversations in the same session.  When provided, a
            "Prior Work in This Session" section is injected after
            the project map.

        Returns
        -------
        str
            The assembled system prompt, ready to be wrapped in a
            ``Message.system(...)`` call.
        """
        self._ensure_cache()
        sections: list[str] = [self._base_persona()]

        proj = self._project_map()
        if proj:
            sections.append(proj)

        if prior_conversation_summaries:
            sections.append(
                self._prior_work_section(prior_conversation_summaries)
            )

        mem = self._persistent_memory_section()
        if mem:
            sections.append(mem)

        sections.append(self._mode_instructions(mode))
        return "\n\n".join(sections)

    def build_compact(
        self,
        mode: str = "execute",
        *,
        prior_conversation_summaries: list[str] | None = None,
    ) -> str:
        """Like :meth:`build` but uses shorter variants of every layer.

        Useful when the conversation is long and every token counts.
        """
        self._ensure_cache()
        sections: list[str] = [self._compact_persona()]

        proj = self._compact_project_map()
        if proj:
            sections.append(proj)

        if prior_conversation_summaries:
            sections.append(
                self._prior_work_section_compact(
                    prior_conversation_summaries
                )
            )

        mem = self._compact_memory_section()
        if mem:
            sections.append(mem)

        sections.append(self._compact_mode_instructions(mode))
        return "\n\n".join(sections)

    def invalidate_cache(self) -> None:
        """Clear cached project map and memory so they are rebuilt.

        Call this when the project structure or memory store has changed
        (e.g. after a tool run that created or deleted files).
        """
        self._project_map_text = None
        self._project_map_compact_text = None
        self._memory_text = None
        self._memory_compact_text = None
        self._cache_loaded = False

    # ------------------------------------------------------------------
    # Layer accessors (public so AgentLoop can pick layers individually)
    # ------------------------------------------------------------------

    @staticmethod
    def base_persona() -> str:
        """Return the base persona text (always included)."""
        return _BASE_PERSONA

    @staticmethod
    def compact_persona() -> str:
        """Return the compact base persona."""
        return _COMPACT_PERSONA

    @staticmethod
    def mode_instructions(mode: str) -> str:
        """Return the mode-specific instructions for *mode*."""
        return _MODE_INSTRUCTIONS.get(mode, _EXECUTING_INSTRUCTIONS)

    @staticmethod
    def compact_mode_instructions(mode: str) -> str:
        """Return compact mode-specific instructions for *mode*."""
        return _COMPACT_MODE_INSTRUCTIONS.get(mode, _COMPACT_EXECUTING)

    # ------------------------------------------------------------------
    # Internal layer builders
    # ------------------------------------------------------------------

    def _ensure_cache(self) -> None:
        """Populate caches if they haven't been filled yet."""
        if self._cache_loaded:
            return
        self._cache_loaded = True

        if self._mapper is not None:
            try:
                self._project_map_text = self._mapper.build_map()
            except Exception:
                logger.exception("Failed to build project map.")
                self._project_map_text = ""

            try:
                self._project_map_compact_text = (
                    self._mapper.build_compact_map()
                )
            except Exception:
                logger.exception("Failed to build compact project map.")
                self._project_map_compact_text = ""

        if self._memory is not None:
            try:
                self._memory_text = self._memory.prompt_section()
            except Exception:
                logger.exception("Failed to load persistent memory.")
                self._memory_text = ""

            try:
                self._memory_compact_text = (
                    self._memory.compact_prompt_section()
                )
            except Exception:
                logger.exception(
                    "Failed to load compact persistent memory."
                )
                self._memory_compact_text = ""

    # -- persona --

    def _base_persona(self) -> str:
        return _BASE_PERSONA

    def _compact_persona(self) -> str:
        return _COMPACT_PERSONA

    # -- project map --

    def _project_map(self) -> str:
        return self._project_map_text or ""

    def _compact_project_map(self) -> str:
        return self._project_map_compact_text or ""

    # -- persistent memory --

    def _persistent_memory_section(self) -> str:
        return self._memory_text or ""

    def _compact_memory_section(self) -> str:
        return self._memory_compact_text or ""

    # -- prior work (cross-conversation context) --

    @staticmethod
    def _prior_work_section(titles: list[str]) -> str:
        """Build a "Prior Work in This Session" section from titles."""
        lines = [
            "## Prior Work in This Session",
            "",
            "You have had previous conversations in this session.  Their",
            "topics are listed below — use this context to understand what",
            "the user has been working on:",
            "",
        ]
        for t in titles:
            lines.append(f"- {t}")
        return "\n".join(lines)

    @staticmethod
    def _prior_work_section_compact(titles: list[str]) -> str:
        """Compact variant of the prior-work section."""
        items = ", ".join(titles)
        return f"Prior session work: {items}"

    # -- mode instructions --

    def _mode_instructions(self, mode: str) -> str:
        return _MODE_INSTRUCTIONS.get(mode, _EXECUTING_INSTRUCTIONS)

    def _compact_mode_instructions(self, mode: str) -> str:
        return _COMPACT_MODE_INSTRUCTIONS.get(mode, _COMPACT_EXECUTING)


# ---------------------------------------------------------------------------
# Mode instruction lookup tables
# ---------------------------------------------------------------------------

_MODE_INSTRUCTIONS: dict[str, str] = {
    "execute": _EXECUTING_INSTRUCTIONS,
    "plan_exploring": _PLAN_EXPLORING_INSTRUCTIONS,
    "plan_executing": _PLAN_EXECUTING_INSTRUCTIONS,
}

_COMPACT_MODE_INSTRUCTIONS: dict[str, str] = {
    "execute": _COMPACT_EXECUTING,
    "plan_exploring": _COMPACT_PLAN_EXPLORING,
    "plan_executing": _COMPACT_PLAN_EXECUTING,
}
