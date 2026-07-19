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

---

## Design

### Two new pieces

| Piece | Role |
|---|---|
| `ConversationContext` | In-memory buffer. Loads from DB **once** on activation, lives across turns within the REPL session, syncs deltas to DB. No DB reload per turn. |
| Clean DB separation | `messages` table is append-only immutable history. Compaction is a **pointer** on the conversation row (`compacted_summary` + `compacted_at_seq`), not a mutation of message rows. |

### How context is managed across turns

```
REPL start
  → ctx = ConversationContext(conv, mgr)
  → ctx.load()                         # DB → memory (once)

Turn 1: Q₁
  → ctx.append(Message.user(Q₁))       # in-memory
  → AgentLoop.run(ctx.messages)        # sees [sys, Q₁]
  → loop iterates, messages accumulate
  → ctx.messages = [sys, Q₁, A₁, tool_results...]
  → ctx.save()                         # append new msgs to DB (Q₁, A₁, tool results)

Turn 2: Q₂                              # NO DB LOAD — ctx already has history
  → ctx.append(Message.user(Q₂))
  → AgentLoop.run(ctx.messages)        # sees [sys, Q₁, A₁, ..., Q₂]
  → context growth triggers compaction at 80%
  → compactor produces summary + keeps last 12 messages
  → ctx.apply_compaction(messages, summary, up_to_seq)
  → ctx.messages = [sys, summary_msg, ...recent 12...]
  → ctx.save()                         # append Q₂/A₂/tool_results to DB
                                       # + UPDATE conversation SET compacted_summary, compacted_at_seq

Turn 3: Q₃
  → ctx.append(Message.user(Q₃))       # still in memory from turn 2
  → AgentLoop.run(ctx.messages)        # sees [sys, summary, recent12, Q₃]

/clear
  → ctx.save()                         # persist final state
  → ctx = ConversationContext(new_conv, mgr)
  → ctx.load()                         # fresh — only system prompt

/resume <old_id>
  → ctx.save()                         # persist current
  → ctx = ConversationContext(old_conv, mgr)
  → ctx.load()                         # DB → memory for old conversation
```

### DB: clean separation of original vs compacted

**`messages` table** — append-only, immutable history:

```
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

### ConversationContext — the in-memory buffer

```python
class ConversationContext:
    """In-memory buffer for a conversation's active LLM message list.

    Created when a conversation is activated.  Holds messages across
    turns — no DB reload per turn.  Syncs deltas to DB on save().
    """

    def __init__(self, conversation: Conversation, session_mgr: SessionManager):
        self._conv = conversation
        self._mgr = session_mgr
        self._messages: list[Message] = []
        self._loaded = False
        self._persisted_count = 0       # how many messages have been saved to DB

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

        # Persist compaction metadata if changed
        await self._mgr.update_conversation(self._conv)

    def append(self, msg: Message) -> None:
        """Append a message in-memory."""
        self._messages.append(msg)

    @property
    def messages(self) -> list[Message]:
        """The active message list (mutated in-place by AgentLoop)."""
        return self._messages

    def apply_compaction(
        self,
        compacted: list[Message],
        summary: str,
        up_to_seq: int,
    ) -> None:
        """Replace in-memory messages with compacted version.

        Called by AgentLoop._check_context_window after the compactor
        returns.  Updates the conversation metadata so the compaction
        survives restarts.
        """
        self._messages = list(compacted)
        self._persisted_count = 0  # compaction changed everything
        self._conv.compacted_summary = summary
        self._conv.compacted_at_seq = up_to_seq

    @property
    def conversation_id(self) -> str:
        return self._conv.id
```

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
- Update `get_messages()`: add `conversation_id` parameter, `after_sequence` parameter (replaces the existing `after_sequence` which currently filters on session-level seq). When `conversation_id` is provided, filter by it.
- Remove `is_compacted` filter from `get_messages()` (no longer needed — compaction is a pointer on conversation, not a message flag)
- Remove `replace_messages()` (no longer needed — messages are append-only)
- Remove `_MIGRATE` comment placeholder, add real v2 migration
- Add `_row_to_conversation` and `_row_to_conversation_summary` converters
- Update `_row_to_message` to populate `conversation_id`

### Step 3: `toddler/session/manager.py` — conversation lifecycle + simplified persistence

Add methods:
- `create_conversation(session_id) → Conversation`
- `get_or_create_active_conversation(session_id) → Conversation`
- `list_conversations(session_id) → list[ConversationSummary]`
- `get_conversation(conversation_id) → Conversation | None`
- `update_conversation(conv) → None`
- `archive_conversation(conversation_id) → None`
- `get_conversation_summaries(session_id) → list[tuple[int, str]]` — for cross-conversation context

Update `append_message`:
- Accept `conversation_id` (required for new code, but keep `session_id` for backward compat)
- Update both session and conversation counters

Update `get_messages`:
- Accept `conversation_id` and `after_sequence`
- No more `exclude_compacted` parameter

Remove (or deprecate):
- `replace_messages()` — no longer needed
- `save_compacted_messages()` — replaced by `update_conversation()` setting `compacted_summary`/`compacted_at_seq`

### Step 4: `toddler/context/compaction.py` — adapt to new model

The compactor currently returns a new message list. It needs one addition:
track `up_to_seq` — the highest `sequence_num` of messages that were summarized.
This is needed so `ConversationContext.apply_compaction()` can set
`compacted_at_seq`.

The compactor receives messages with sequence numbers attached (or we track
the count of messages summarized). Simplest approach: return a tuple:

```python
@dataclass
class CompactionResult:
    messages: list[Message]    # system + summary + recent
    summary: str               # the raw LLM summary text
    compacted_up_to: int       # number of body messages summarized (not incl. system)
```

Actually, since sequence numbers are session-global and the compactor doesn't
know about them, we handle this differently: the caller (AgentLoop) knows
how many original messages were compacted. It passes `up_to_seq` to
`ctx.apply_compaction()`.

### Step 5: `toddler/agent/loop.py` — use ConversationContext

- Remove `session_id` parameter from `run()`, add `context: ConversationContext | None = None`
- At start of `run()`:
  ```python
  if context is not None:
      messages = context.messages                    # already loaded, includes history
      messages.append(Message.user(user_input))
      # Replace leading system message(s) with fresh prompt
      sys_text = self._prompt_builder.build(mode, prior_summaries=...)
      # ... replace system messages ...
  else:
      messages = [Message.system(sys_text), Message.user(user_input)]
  ```
- In `_check_context_window`: after compaction, call `context.apply_compaction(...)` instead of persisting to session store
- `run()` no longer directly touches the session store — all persistence goes through `context`

### Step 6: `toddler/cli/app.py` — own the ConversationContext

- Add `_ctx: ConversationContext | None` attribute
- In `run_repl()` / `run_one_shot()`: create `ConversationContext`, call `ctx.load()`
- In `_run_agent_turn()`:
  ```python
  # Before: persist user message to session store
  # After:  just append to context (it's in-memory)
  ctx.append(Message.user(user_input))
  await self._agent.run(user_input, context=ctx, ...)
  # After run completes, persist deltas
  await ctx.save()
  ```
- Remove individual `append_message` calls for user/assistant messages — AgentLoop owns the message list now
- `/clear` handler: `ctx.save()`, create new conversation, `ctx = ConversationContext(new_conv)`, `ctx.load()`
- `/resume <id>`: `ctx.save()`, load conversation, `ctx = ConversationContext(conv)`, `ctx.load()`
- Token accumulation: update `ctx._conv` counters and call `update_conversation` in `ctx.save()`

### Step 7: `toddler/cli/commands.py` — new slash commands

- Modify `_cmd_clear` → returns `__NEW_CONVERSATION__` sentinel
- Add `_cmd_resume` → returns `__RESUME_CONVERSATION__:<id>`
- Add `_cmd_conversations` → returns `__LIST_CONVERSATIONS__`
- Register in `_COMMAND_TABLE`
- Update `HELP_TEXT`

### Step 8: `toddler/context/system_prompt.py` — cross-conversation summaries

- Add `prior_conversation_summaries: list[str] | None = None` to `build()` and `build_compact()`
- When provided, inject a "Prior Work in This Session" section

### Step 9: `toddler/session/__init__.py` — exports

- Export `Conversation`, `ConversationSummary`

---

## How Compaction Works End-to-End

```
1. AgentLoop._check_context_window detects 80% usage
2. Calls compactor.compact(messages)
3. Compactor separates system messages, splits body at "keep last 12"
4. LLM summarizes older messages, returns [sys + summary_msg + recent_12]
5. AgentLoop calls ctx.apply_compaction(
       compacted_messages,
       summary="LLM output text",
       up_to_seq=<sequence_num of last summarized message>,
   )
6. ctx.apply_compaction:
   - Replaces ctx._messages in-place
   - Sets ctx._conv.compacted_summary
   - Sets ctx._conv.compacted_at_seq
   - Resets _persisted_count (next save() will persist everything)
7. Later, ctx.save():
   - Persists new messages (post-compaction) to messages table
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
| Cross-conversation summary | `conversations.summary` (set on `/clear`) | Once, when archived |

---

## Backward Compatibility

| Concern | How Handled |
|---------|-------------|
| Sessions w/o conversations | Migration creates default conversation + backfills FK |
| `run()` without context | `context=None` → legacy path: `[system, user_input]` |
| `get_messages()` without `conversation_id` | Falls back to session-only filtering |
| CLI without session manager | All conversation code guarded by `if self._session_mgr` |
| Old `is_compacted` rows in messages | Migration clears flag; new code ignores the column |
| Tests calling `run()` directly | Works — `context=None` preserves legacy behavior |

## Verification

1. **Unit tests**: `tests/test_conversation_context.py` — load, save, compaction apply, DB round-trip
2. **Unit tests**: `tests/test_conversations.py` — store CRUD, manager lifecycle, migration
3. **Agent loop tests**: extend `tests/test_agent_loop.py` — history loading via context, compaction interaction
4. **Manual end-to-end**:
   - Q₁ → A₁, Q₂ references Q₁ → agent remembers
   - `/clear` → new conversation, Q₃ → agent does NOT know Q₁/Q₂
   - `/conversations` → shows archived + active
   - `/resume <id>` → back to first conversation, Q₄ references Q₁ → agent remembers
   - Long conversation → compaction triggers → continue → context still works
   - Restart REPL with `--session <id>` → conversation resumes correctly
   - `/session info` → shows session with conversation count
