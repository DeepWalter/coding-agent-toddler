# Plan: Session → Conversation Hierarchy

## Context

The current AgentLoop builds messages from scratch on every call
(`loop.py:178-181`). Messages are persisted to SQLite but never loaded back —
Q₂ never sees Q₁/A₁.

We need a **Session → Conversation** hierarchy:
- **Session** = project-level container spanning days (already exists)
- **Conversation** = a continuous LLM context window within a session (new entity)
- `/clear` archives the current conversation and starts a clean one
- `/resume <id>` switches to a previously archived conversation
- Conversations are isolated — no context pollution across conversations

Additionally, `AgentLoop.__init__` currently takes **four separate
context-related parameters** (`system_prompt_builder`, `context_window_mgr`,
`conversation_compactor`, `session_manager`) and `AgentLoop._check_context_window`
directly orchestrates them. These concerns belong in the context package, not in
the agent loop.

---

## Package Responsibilities

A clean boundary between two packages:

| Concern | Package | Role |
|---|---|---|
| DB schema, migrations, raw CRUD | `toddler.session.store` | Data layer |
| Serialization, business logic, lifecycle | `toddler.session.manager` | Persistence facade |
| In-memory message buffer | `toddler.context.conversation_context` | Context management |
| System prompt assembly | `toddler.context.system_prompt` | Context management |
| Token tracking, thresholds | `toddler.context.window` | Context management |
| LLM summarisation of old turns | `toddler.context.compaction` | Context management |

**Rule**: `toddler.context` never touches SQLite directly. All DB I/O goes
through `SessionManager` methods. `AgentLoop` never calls `SessionManager`
directly — it goes through `ConversationContext`.

---

## Design

### Three new pieces

| Piece | Location | Role |
|---|---|---|
| `Conversation` + `ConversationSummary` | `toddler.session.models` | DB entity dataclasses |
| `SessionManager` conversation CRUD | `toddler.session.manager` | Persistence methods for conversations |
| `ConversationContext` | `toddler.context.conversation_context` | In-memory buffer **and** orchestrator — wires prompt building, window tracking, compaction, and delegates persistence to `SessionManager` |

### `ConversationContext` — buffer + orchestrator

This is the central abstraction. It replaces the four separate parameters
currently passed to `AgentLoop` with a single object that:

1. **Holds the message list** in memory across turns (no DB reload per turn)
2. **Builds the system prompt** via `SystemPromptBuilder` on each turn
3. **Tracks token usage** via `ContextWindowManager`
4. **Triggers and applies compaction** via `ConversationCompactor`
5. **Delegates persistence** to `SessionManager`

### Conversation Titling

Conversations have an optional `title` field. Titling follows a simple
priority chain:

| Priority | Source | When |
|---|---|---|
| 1 (highest) | `/clear <title>` — user-provided | At archive time |
| 2 | First user input message (truncated) | On first turn, if title is still *None* |
| 3 (lowest) | *None* — displayed as "Untitled" | Default |

**Auto-titling** happens inside `prepare_turn()`: when the message list is
empty (first turn) and the conversation has no title, the first user input is
truncated to 80 chars and set as the title. No extra DB round-trip — the
title is persisted alongside messages in the next `save()` call.

**Explicit titling** happens on `/clear <title>`: the title is set (or
overridden) on the conversation before archiving. This gives the user final
say over what appears in `/conversations` listings.

```python
class ConversationContext:
    """In-memory buffer and management orchestrator for a conversation.

    Created when a conversation is activated.  Holds messages across
    turns — no DB reload per turn.  Syncs deltas to DB on save().

    Wires together the three context-management components
    (SystemPromptBuilder, ContextWindowManager, ConversationCompactor)
    so AgentLoop only deals with ONE object instead of four.
    """

    def __init__(
        self,
        conversation: Conversation,
        session_mgr: SessionManager,
        prompt_builder: SystemPromptBuilder,
        *,
        window_mgr: ContextWindowManager | None = None,
        compactor: ConversationCompactor | None = None,
    ) -> None:
        self._conv = conversation
        self._mgr = session_mgr
        self._prompt_builder = prompt_builder
        self._window_mgr = window_mgr
        self._compactor = compactor

        self._messages: list[Message] = []
        self._loaded = False
        self._persisted_count = 0
        self._has_compacted = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Load active context from DB. Called once on activation."""
        after = self._conv.compacted_at_seq or -1
        recent = await self._mgr.get_messages(
            conversation_id=self._conv.id,
            after_sequence=after,
        )
        self._messages = []
        if self._conv.compacted_summary:
            self._messages.append(
                Message.user(
                    f"[Compacted history — summary of the conversation so far]\n\n"
                    f"{self._conv.compacted_summary}"
                )
            )
        self._messages.extend(recent)
        self._persisted_count = len(recent)
        self._loaded = True

    async def save(self) -> None:
        """Persist new messages to DB and update conversation metadata."""
        new_msgs = self._messages[self._persisted_count:]
        for msg in new_msgs:
            await self._mgr.append_message(self._conv.id, msg)
        self._persisted_count = len(self._messages)

        # Persist conversation metadata (title, counters, compaction pointer).
        await self._mgr.update_conversation(self._conv)

    # ------------------------------------------------------------------
    # Turn preparation (replaces AgentLoop's inline message building)
    # ------------------------------------------------------------------

    def prepare_turn(self, user_input: str, mode: str = "execute") -> list[Message]:
        """Prepare the message list for a new agent turn.

        On first turn: builds system prompt, auto-titles the conversation
        from *user_input* if no title is set, then appends user_input.
        On subsequent turns: appends user_input to existing history.

        Returns the mutable message list — AgentLoop can modify it in-place
        (appending assistant responses, tool results, etc.).
        """
        if not self._messages:
            # Fresh conversation — build system prompt from scratch.
            sys_text = self._prompt_builder.build(mode)
            self._messages = [Message.system(sys_text)]
            self._maybe_auto_title(user_input)

        self._messages.append(Message.user(user_input))
        return self._messages

    # ------------------------------------------------------------------
    # Titling
    # ------------------------------------------------------------------

    def _maybe_auto_title(self, user_input: str) -> None:
        """Set conversation title from first user input if not already set."""
        if not self._conv.title:
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
        logger.info(f"Context: {self._window_mgr.status_line(self._messages)}")

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
                # The compactor keeps the last _keep_recent messages;
                # everything before that (minus system messages) was summarized.
                up_to_seq = self._compute_compacted_up_to()

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

        Updates the conversation metadata so the compaction survives
        restarts.  Does NOT reset _persisted_count — the recent messages
        were already persisted in earlier save() calls and the summary is
        stored on the conversation row (not as a message row).  Only
        genuinely new messages from future turns will be persisted.
        """
        self._messages = list(compacted)
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
        return self._conv.id

    @property
    def conversation(self) -> Conversation:
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

    def _compute_compacted_up_to(self) -> int:
        """Return the sequence_num of the last message covered by the summary.

        We track this by counting how many non-system messages were in the
        original list before compaction minus the _keep_recent that were kept.
        The caller (SessionManager) can map this to actual sequence numbers.
        """
        # Simplified: the compactor keeps _keep_recent body messages, so
        # everything before that was summarized.  We store the count of
        # summarized messages; the actual sequence_num mapping is handled
        # during save().
        return 0  # Filled in by caller; see Compaction section below.

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
```

### How context is managed across turns

```
REPL start
  → ctx = ConversationContext(conv, mgr, prompt_builder,
                               window_mgr=wm, compactor=cp)
  → ctx.load()                              # DB → memory (once)

Turn 1: Q₁
  → ctx.prepare_turn(Q₁, mode)             # builds [sys, Q₁], auto-titles from Q₁ if no title
  → AgentLoop.run(ctx.messages)             # sees [sys, Q₁]
  → loop iterates, messages accumulate
  → ctx.messages = [sys, Q₁, A₁, tool_results...]
  → ctx.save()                              # append new msgs to DB + persist title

Turn 2: Q₂                                   # NO DB LOAD — ctx already has history
  → ctx.prepare_turn(Q₂, mode)             # appends Q₂: [sys, Q₁, A₁, ..., Q₂]
  → AgentLoop.run(ctx.messages)             # sees full history
  → context growth triggers compaction at 80%
  → ctx.check_and_compact()
    → compactor produces summary + keeps last 12 messages
    → ctx.apply_compaction(messages, summary, up_to_seq)
    → ctx.messages = [sys_compact, summary_msg, ...recent 12...]
  → ctx.save()                              # append Q₂/A₂/tool_results to DB
                                            # + UPDATE conversation SET compacted_summary, compacted_at_seq

Turn 3: Q₃
  → ctx.prepare_turn(Q₃, mode)             # still in memory from turn 2
  → AgentLoop.run(ctx.messages)             # sees [sys_compact, summary, recent12, Q₃]

/clear ["Optional title"]
  → ctx.set_title(title) if provided        # override auto-title
  → ctx.save()                              # persist final state with title
  → ctx = ConversationContext(new_conv, mgr, prompt_builder, ...)
  → ctx.load()                              # fresh — only system prompt

/resume <old_id>
  → ctx.save()                              # persist current
  → ctx = ConversationContext(old_conv, mgr, prompt_builder, ...)
  → ctx.load()                              # DB → memory for old conversation
```

---

### DB: clean separation of original vs compacted

**`messages` table** — append-only, immutable history:

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sequence_num INTEGER NOT NULL,        -- global within session (sparse per conversation)
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence_num)
);
```

Messages are **never mutated**. No `is_compacted` flag. The `conversation_id`
is set at insert time and never changes. On conversation delete, messages
cascade-delete.

**`conversations` table** — compaction is metadata, not mutation:

```sql
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    title TEXT,
    sequence_num INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'archived'
    compacted_summary TEXT,                   -- LLM summary covering early messages
    compacted_at_seq INTEGER,                 -- messages with seq <= this are covered
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0
);
```

**How active context is derived from DB on load:**

```python
async def load(self):
    conv = await mgr.get_conversation(self.conversation_id)
    after = conv.compacted_at_seq or -1
    recent = await mgr.get_messages(
        conversation_id=self.conversation_id,
        after_sequence=after,              # skip messages covered by summary
    )
    self._messages = []
    if conv.compacted_summary:
        # Summary is a synthetic message, NOT stored in messages table
        self._messages.append(
            Message.user(f"[Compacted history]\n\n{conv.compacted_summary}")
        )
    self._messages.extend(recent)
```

The `messages` table always contains the **original** messages. Compaction
doesn't touch them — it just sets a pointer on the conversation. On load,
messages covered by the summary are skipped. This is clean, auditable, and
reversible (clear the pointer to "uncompact").

**Key insight on `apply_compaction` and `_persisted_count`:** The recent
messages kept after compaction were already persisted in earlier `save()` calls.
The summary text is stored on the conversation row (`compacted_summary`), not as
a message row. Therefore `apply_compaction` does NOT reset `_persisted_count =
0` — only genuinely new messages from future turns will be appended to the
messages table. No duplicate message rows.

---

### `AgentLoop` — before and after

**Before** (4 context params + 50-line `_check_context_window` method):

```python
class AgentLoop:
    def __init__(self, llm, registry, executor, settings, *,
                 system_prompt_builder=None,
                 context_window_mgr=None,
                 conversation_compactor=None,
                 session_manager=None):
        self._prompt_builder = system_prompt_builder or SystemPromptBuilder()
        self._window_mgr = context_window_mgr
        self._compactor = conversation_compactor
        self._session_mgr = session_manager
        self._has_compacted = False

    async def run(self, user_input, *, session_id=None, mode="execute", ...):
        self._has_compacted = False
        # ... build messages from scratch ...
        # ... call _check_context_window(messages, session_id) ...
```

**After** (1 context param, no `_check_context_window`, no `_has_compacted`):

```python
class AgentLoop:
    def __init__(self, llm, registry, executor, settings, *,
                 context: ConversationContext | None = None):
        self._ctx = context

    async def run(self, user_input, *, mode="execute", ...):
        if self._ctx is not None:
            messages = self._ctx.prepare_turn(user_input, mode)
        else:
            # Legacy path — no session, no history.
            sys_text = _DEFAULT_SYSTEM_PROMPT  # or inline minimal prompt
            messages = [Message.system(sys_text), Message.user(user_input)]

        while True:
            if self._ctx is not None:
                await self._ctx.check_and_compact()
            # ... LLM call, tool execution — unchanged ...
```

- `_check_context_window` is **deleted** from `AgentLoop` — it becomes
  `ConversationContext.check_and_compact()`.
- `_has_compacted` is **deleted** from `AgentLoop` — the context tracks it.
- `session_id` parameter is **removed** from `run()` — the context already
  knows its conversation.
- `system_prompt` parameter is **removed** from `run()` — use
  `ctx.prepare_turn()` instead; for legacy mode, use a simple default.

---

### `CLIApp` — owns the `ConversationContext`

```python
class CLIApp:
    def __init__(self, settings, *, session_manager=None, llm=None, ...):
        # ... unchanged ...
        self._ctx: ConversationContext | None = None

    async def run_repl(self, *, session_id=None):
        # Resolve session + conversation, build ConversationContext.
        if self._session_mgr is not None:
            session = await self._session_mgr.get_or_create(session_id)
            conv = await self._session_mgr.get_or_create_active_conversation(session.id)
            self._ctx = ConversationContext(
                conv, self._session_mgr, self._prompt_builder,
                window_mgr=self._context_window_mgr,
                compactor=self._conversation_compactor,
            )
            await self._ctx.load()

        while True:
            user_input = await self._input.prompt(...)
            if user_input.startswith("/"):
                handled = await self._handle_slash_command(user_input)
                if not handled:
                    break
                continue
            await self._run_agent_turn(user_input)

    async def _run_agent_turn(self, user_input, *, force_plan=False):
        # No more manual append_message for user/assistant messages.
        # ConversationContext handles all persistence.
        stream = self._settings.streaming_enabled
        if stream:
            await self._run_streaming_turn(user_input, ...)
        else:
            gen = self._agent.run(user_input, stream=False, mode=mode_hint)
            await self._process_events(gen)

        # After turn completes, persist deltas.
        if self._ctx is not None:
            await self._ctx.save()

    def _handle_slash_command(self, text):
        # /clear → ctx.save(), create new conv, ctx = ConversationContext(...), ctx.load()
        # /resume <id> → ctx.save(), load conv, ctx = ConversationContext(...), ctx.load()
        ...

    @property
    def _agent(self) -> AgentLoop:
        if not hasattr(self, '_agent_impl'):
            self._agent_impl = AgentLoop(
                llm_provider=self._llm,
                tool_registry=self._registry,
                tool_executor=self._executor,
                settings=self._settings,
                context=self._ctx,
            )
        return self._agent_impl
```

Note: `_agent` is a property that reads `self._ctx` at call time. When the
context changes (e.g., `/clear` creates a new `ConversationContext`), the
next agent turn picks it up automatically.

---

## How Compaction Works End-to-End

```
1. AgentLoop calls ctx.check_and_compact() before each LLM call
2. ContextWindowManager detects 80% usage → should_compact() returns True
3. ctx.check_and_compact() calls compactor.compact(messages)
4. Compactor separates system messages, splits body at "keep last 12"
5. LLM summarizes older messages, returns [sys + summary_msg + recent_12]
6. ctx extracts summary text, computes up_to_seq
7. ctx.apply_compaction(compacted_messages, summary, up_to_seq):
   - Replaces ctx._messages in-place
   - Sets ctx._conv.compacted_summary
   - Sets ctx._conv.compacted_at_seq
   - Does NOT reset _persisted_count (recent msgs already in DB)
8. ctx replaces leading system messages with compact variant
9. Later, ctx.save():
   - Persists any new messages (post-compaction turns) to messages table
   - Updates conversation row with compacted_summary/compacted_at_seq
   - Original pre-compaction messages stay in messages table (immutable)
```

**Key insight:** The original messages compacted away are already in the
`messages` table (they were persisted in previous `ctx.save()` calls).
Compaction just sets a pointer so they're skipped on next load. They remain
as an audit trail.

---

## Clean Separation Summary

| What | Where | Mutated? |
|------|-------|----------|
| Original messages (append-only history) | `messages` table | Never — insert only |
| Compaction pointer | `conversations.compacted_summary` + `compacted_at_seq` | Updated on compaction |
| Active context (what LLM sees) | `ConversationContext._messages` (in-memory) | Every turn |
| System prompt assembly | `ConversationContext._prompt_builder` | Called each turn |
| Token tracking & thresholds | `ConversationContext._window_mgr` | Read each turn |
| Cross-conversation summary | `conversations.title` + summary injection in system prompt | Once, when archived |
| All DB I/O | `toddler.session.SessionManager` | Called by `ConversationContext` |
| Context orchestration | `toddler.context.ConversationContext` | N/A (the orchestrator) |

---

## Files to Modify (in order)

### Step 1: `toddler/session/models.py` — new dataclasses

Add:
```python
@dataclass
class Conversation:
    id: str
    session_id: str
    title: str | None = None
    sequence_num: int = 0
    status: str = "active"          # "active" | "archived"
    compacted_summary: str | None = None
    compacted_at_seq: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    message_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

@dataclass
class ConversationSummary:
    id: str
    session_id: str
    title: str | None
    sequence_num: int
    status: str
    message_count: int
    created_at: datetime
    updated_at: datetime
```

Add `conversation_id: str = ""` to `StoredMessage`.

### Step 2: `toddler/session/store.py` — schema v2 + CRUD

- Bump `CURRENT_SCHEMA_VERSION` to 2
- Add `_CREATE_CONVERSATIONS` DDL
- Add `conversation_id` column to messages (`ALTER TABLE messages ADD COLUMN conversation_id TEXT`)
- Add index `idx_messages_conversation ON messages(conversation_id, sequence_num)`
- `_migrate()` v1→v2: for each session with messages, create a default conversation, backfill `conversation_id`
- Add CRUD: `create_conversation`, `get_conversation`, `get_active_conversation`, `list_conversations`, `update_conversation`, `archive_conversation`
- Update `get_messages()`: add `conversation_id` parameter, `after_sequence` parameter. When `conversation_id` is provided, filter by it.
- Remove `is_compacted` filter from `get_messages()` (no longer needed — compaction is a pointer on conversation, not a message flag)
- Remove `replace_messages()` (no longer needed — messages are append-only)
- Add `_row_to_conversation` and `_row_to_conversation_summary` converters
- Update `_row_to_message` to populate `conversation_id`

### Step 3: `toddler/session/manager.py` — conversation lifecycle + simplified API

Add methods:
- `create_conversation(session_id) → Conversation`
- `get_or_create_active_conversation(session_id) → Conversation`
- `list_conversations(session_id) → list[ConversationSummary]`
- `get_conversation(conversation_id) → Conversation | None`
- `update_conversation(conv) → None`
- `archive_conversation(conversation_id) → None`
- `get_conversation_summaries(session_id) → list[tuple[int, str]]` — for cross-conversation context

Update `append_message`:
- Accept `conversation_id` (required parameter)
- Update both session and conversation counters

Update `get_messages`:
- Accept `conversation_id` and `after_sequence`
- No more `exclude_compacted` parameter

Remove:
- `replace_messages()` — no longer needed (messages are append-only)
- `save_compacted_messages()` — replaced by `update_conversation()` setting `compacted_summary`/`compacted_at_seq`

### Step 4: `toddler/context/conversation_context.py` — **new file**

Create the `ConversationContext` class as shown in the Design section above.
This is the central abstraction — in-memory buffer + orchestrator that wires
`SystemPromptBuilder`, `ContextWindowManager`, and `ConversationCompactor`.

The class:
- Takes `Conversation`, `SessionManager`, `SystemPromptBuilder`, and optional `ContextWindowManager` + `ConversationCompactor`
- `load()` / `save()` — DB round-trip (delegates to `SessionManager`)
- `prepare_turn(user_input, mode)` — builds system prompt, auto-titles from first user input, appends user input
- `set_title(title)` — explicitly set conversation title (for `/clear <title>`)
- `check_and_compact()` — moved from `AgentLoop._check_context_window`
- `apply_compaction()` — replaces in-memory messages, sets compaction pointer
- `messages` property — mutable list for AgentLoop to work with

### Step 5: `toddler/context/__init__.py` — export `ConversationContext`

Add `ConversationContext` to the public exports.

### Step 6: `toddler/agent/loop.py` — use `ConversationContext`, remove `_check_context_window`

Constructor changes:
- **Remove** `system_prompt_builder`, `context_window_mgr`, `conversation_compactor`, `session_manager` parameters
- **Add** `context: ConversationContext | None = None`
- **Remove** `self._prompt_builder`, `self._window_mgr`, `self._compactor`, `self._session_mgr`, `self._has_compacted`

`run()` changes:
- **Remove** `system_prompt`, `session_id` parameters
- Replace inline message building (lines 171-181) with:
  ```python
  if self._ctx is not None:
      messages = self._ctx.prepare_turn(user_input, mode)
  else:
      messages = [Message.system(_DEFAULT_SYSTEM_PROMPT), Message.user(user_input)]
  ```
- Replace `await self._check_context_window(messages, session_id)` with:
  ```python
  if self._ctx is not None:
      await self._ctx.check_and_compact()
  ```

Remove entirely:
- `_check_context_window()` method (~90 lines, lines 437-526)
- `_has_compacted` flag and its reset in `run()`

### Step 7: `toddler/cli/app.py` — own the `ConversationContext`

- Add `self._ctx: ConversationContext | None = None` attribute
- In `run_repl()` / `run_one_shot()`: after session resolution, create `ConversationContext`, call `ctx.load()`
- In `_run_agent_turn()`:
  - **Remove** manual `append_message` for user message (line 336-338) — `ctx.prepare_turn()` handles it
  - **Remove** manual `append_message` for assistant message (lines 499-502, 578-581) — `ctx.save()` handles all persistence
  - **Remove** `session_id` from `self._agent.run()` call
  - After turn completes: `await ctx.save()`
- `/clear` handler: if optional `<title>` provided, `ctx.set_title(title)`, then `ctx.save()`, create new conversation, `ctx = ConversationContext(...)`, `ctx.load()`
- `/resume <id>`: `ctx.save()`, load conversation, `ctx = ConversationContext(...)`, `ctx.load()`
- Update `_agent` property to pass `context=self._ctx` instead of the four individual params
- Update `_handle_slash_command` to handle `__NEW_CONVERSATION__:<title>` and `__RESUME_CONVERSATION__:<id>` sentinels

### Step 8: `toddler/cli/commands.py` — new slash commands

- Modify `_cmd_clear` → accepts optional `<title>` arg; returns `__NEW_CONVERSATION__:<title>` sentinel
- Add `_cmd_resume` → returns `__RESUME_CONVERSATION__:<id>`
- Add `_cmd_conversations` → returns `__LIST_CONVERSATIONS__`
- Register in `_COMMAND_TABLE`
- Update `HELP_TEXT`

### Step 9: `toddler/context/system_prompt.py` — cross-conversation summaries

- Add `prior_conversation_summaries: list[str] | None = None` to `build()` and `build_compact()`
- When provided, inject a "Prior Work in This Session" section

### Step 10: `toddler/session/__init__.py` — exports

- Export `Conversation`, `ConversationSummary`

---

## Backward Compatibility

| Concern | How Handled |
|---------|-------------|
| Sessions w/o conversations | Migration creates default conversation + backfills FK |
| `AgentLoop.run()` without context | `context=None` → legacy path: `[system, user_input]` |
| `AgentLoop` constructor without context | All old params removed; pass `context=None` or omit |
| `get_messages()` without `conversation_id` | Falls back to session-only filtering |
| CLI without session manager | `self._ctx` stays `None`; `_agent` passes `context=None` |
| Old `is_compacted` rows in messages | Migration clears flag; new code ignores the column |
| Tests calling `AgentLoop(...)` directly | Update to pass `context=None`; legacy message path works |

---

## Verification

1. **Unit tests**: `tests/test_conversation_context.py` — load, save, compaction apply, DB round-trip, `prepare_turn`, `check_and_compact`
2. **Unit tests**: `tests/test_conversations.py` — store CRUD, manager lifecycle, migration
3. **Agent loop tests**: extend `tests/test_agent_loop.py` — history loading via context, compaction interaction, legacy `context=None` path
4. **Manual end-to-end**:
   - Q₁ → A₁, Q₂ references Q₁ → agent remembers
   - Auto-titling: first user input becomes conversation title (visible in `/conversations`)
   - `/clear "Bug fix"` → new conversation, old one titled "Bug fix"
   - `/clear` → new conversation with auto-title from first message, old one keeps its title
   - `/conversations` → shows archived + active with titles
   - `/resume <id>` → back to first conversation, Q₄ references Q₁ → agent remembers
   - Long conversation → compaction triggers → continue → context still works
   - Restart REPL with `--session <id>` → conversation resumes correctly
   - `/session info` → shows session with conversation count
