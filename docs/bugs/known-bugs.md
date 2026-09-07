# Known bugs

Verified defects in the context / session layer.  Both were found and
confirmed with scratch probes on 2026-09-05 and are **unfixed**.

1. [Reload after compaction resurrects folded messages](#bug-1-reload-after-compaction-resurrects-folded-messages)
2. [Plan-mode system prompt never reaches the model](#bug-2-plan-mode-system-prompt-never-reaches-the-model)

---

## Bug 1 — Reload after compaction resurrects folded messages

### Summary

`conversation.compacted_at_seq` — the reload fold pointer — is advanced with
message-**count** arithmetic that does not map onto per-session stored
**sequence** numbers.  After any compaction, a reload keeps messages that the
compaction folded away: old turns reappear (duplicated with the summary), the
buffer opens `user → system → user` (corrupted role order), and
`conversation.message_count` disagrees with the replayed transcript.  The gap
starts at 3 stored sequences on the first compaction and grows by 1 on every
subsequent one.

### Intended invariant

A reload after compaction should reproduce the folded buffer: the synthetic
`[Compacted history …]` summary message plus the stored messages that
survived the fold — nothing else.

### Verified reproduction

Fresh session, 7 turns → 15 stored rows, per-session sequence numbers dense
from 1 (`toddler/session/storage.py:192`):

| seq | 1 | 2 | 3 | 4 | … | 15 |
|---|---|---|---|---|---|---|
| role | system | user (Q0) | assistant (A0) | user (Q1) | … | assistant (A6) |

`compact_context()` folds the system prompt and Q0/A0.  In memory the buffer
goes 15 → 14 (`[compact system][summary][Q1…A6]`); the pointer must end at 3.
The persistence math (`toddler/session/manager.py:743-754`) instead persists
`compacted_at_seq = 0`, so `_activate_context` (`manager.py:426`) reloads
**every** row → 16 messages starting `[user: summary][system: original
prompt][Q0][A0]…`.

### Root cause

`toddler/session/manager.py:748-753` reconstructs the fold size from count
deltas because `CompactionResult` — the only channel out of the deliberately
storage-agnostic `ContextManager` — carries counts, not storage coordinates.
That reconstruction is wrong in two independent ways:

1. **`body_after = messages_after - 1` counts the synthetic summary as a kept
   body slot.**  The summary message is a real in-memory slot but is never
   stored (see appendix), so `summarized` under-counts the folded body
   messages by exactly 1 per compaction.
2. **The anchor is in count-space, not stored-seq-space.**
   `prev = compacted_at_seq or self._base_seq - 1` starts at −1, and the
   "minus system msg" on both sides pretends the stored system row (seq 1)
   was already excluded — but a real fold evicts it too.

Result: first-compaction pointer 0 instead of 3 (system + Q0 + A0 folded),
compounding by +1 on every later compaction.

### Consequences

- Folded turns resurrect on reload, duplicating content already covered by
  the summary.
- Role order corruption (`user → system → user`) survives into the next LLM
  call: `ContextManager.load` (`toddler/context/manager.py:121`) is a plain
  copy with no sanitising, and `prepare_turn` (`manager.py:212`) only builds
  a system prompt on an *empty* buffer.
- Even a *correct* pointer leaves the reloaded buffer with no system message
  at all (see appendix: only the conversation-start prompt is ever stored),
  so a naive counting fix would trade resurrected turns for a system-less
  context.  Any fix must also decide system-prompt semantics on reload.

### Why tests didn't catch it

The only compaction→reload test (`tests/test_compact_command.py:201-235`)
asserts just `reloaded[0]` (role + summary marker) and never compares the
reloaded set against the folded buffer, so it passes with a 16-message reload
where the in-memory buffer has 14.

### Fix direction

Derive the fold boundary from storage truth (which stored rows the compacted
buffer still contains) instead of reconstructing it from message-count
deltas — e.g. the session layer computing the pointer from the max stored
sequence at compaction time minus `keep_recent`, or the compactor reporting
how many leading body messages it folded.  Cover with a regression test that
asserts the reloaded set equals the folded buffer (modulo system-prompt
handling).  Not yet implemented.

---

## Bug 2 — Plan-mode system prompt never reaches the model

### Summary

The system prompt is built exactly once per conversation — on the first turn,
when the buffer is empty (`toddler/context/manager.py:212-218`).  A
mid-conversation switch to plan mode (`/plan`, `/mode plan`, or the
complexity heuristic) never rebuilds it, so the **explore and execute phases
of a plan turn run under the mode prompt the conversation started with** —
typically the execute-mode prompt.  The `plan_exploring` / `plan_executing`
instruction blocks exist but are unreachable in the plan lifecycle:

- `_PLAN_EXPLORING_INSTRUCTIONS` can only appear in a system prompt built as
  the *first* message of a conversation (`prepare_turn` on an empty buffer
  with mode `"plan_exploring"`) — i.e. only when the very first turn of a
  conversation is plan-flagged.
- `_PLAN_EXECUTING_INSTRUCTIONS` (`toddler/context/builder.py:63`) is **dead
  code**: it can never appear in any live path at all (see below).

### Why plan_executing is unreachable

`_PLAN_EXECUTING_INSTRUCTIONS` is referenced only by
`_MODE_INSTRUCTIONS["plan_executing"]` (`builder.py:363`), which feeds
`build()` (`builder.py:188`).  `build()`'s only caller in the codebase is
`prepare_turn`'s empty-buffer branch (`context/manager.py:214`), and mode
`"plan_executing"` is only ever reached **mid-cycle**: inside a plan turn,
`process_turn` runs explore → propose → wait → execute within one in-flight
`planner.run()` generator (`toddler/session/manager.py:256-303`,
`toddler/agent/planner.py:440-474`), by which point the buffer is long
populated.  A restart cannot make execution a first turn — the approval gate
is in-memory and a reset clears the state machine.  Compaction is not a route
either: `build_compact` reads `_COMPACT_MODE_INSTRUCTIONS`, which maps
`plan_executing` to its own shorter constant (`builder.py:366-370`).

The remaining accessor — `SystemPromptBuilder.mode_instructions` via
`AgentStateMachine.get_system_prompt_extension` (`toddler/agent/state_machine.py:458`)
— has **zero callers**: an apparent per-phase append hook that was never
wired.

### What the plan phases actually get

Verified by probe (execute-start conversation → plan-flagged turn): the
buffer's system message is byte-identical across the mode switch, and the DB
still holds exactly one system row.  Plan execution is steered only by:

- the approved plan arriving as a **user-role message**
  (`plan.format_for_prompt()`);
- the `plan_update` tool registered during execution
  (`toddler/session/manager.py:556-565`);
- permission-gating pinned to MANUAL.

None of these carry the mode instructions (`"## Current Mode: PLAN (Execute)"`
etc.) the builder defines for them.

### Consequences

- In a conversation that started in execute mode, a `/plan` turn explores,
  proposes, and executes with **no plan instructions anywhere in the model
  input** — research-only discipline and step-by-step execution guidance
  depend entirely on gating and tool schemas.
- The mode hint does reach one place: `ContextManager._mode` records the last
  hint, which a later compaction's `build_compact` uses — so a compaction
  during the execution phase can produce an ephemeral compact prompt with
  plan_executing instructions.  That prompt is never persisted (see
  appendix), so it vanishes on reload.
- A conversation that *starts* plan-flagged keeps its stored
  plan_exploring prompt forever, including in later execute-mode turns, and
  its stored system row is reused verbatim on every resume — prompt changes
  in later builds never reach resumed conversations.

### Fix direction

Decide where mode instructions belong per phase and wire them:

- rebuild (or append the mode extension to) the system message when the state
  machine enters a plan phase — the dead
  `get_system_prompt_extension` hook suggests that was the original intent —
  and persist/reconstruct it consistently; or
- steer plan phases through user-role scaffolding by design and delete the
  dead plan instruction blocks and hook.

Not yet implemented.

---

## Appendix — related observations (verified)

- **Only the conversation-start system prompt is ever stored.**  It is
  persisted with the first `save()` of the conversation as a normal message
  row (`role='system'`, seq 1) because the first `prepare_turn` runs on an
  empty buffer and the baseline is 0.  The compact-variant prompt rebuilt at
  each compaction (`build_compact`) is never stored.
- **The synthetic summary is stored as text, not as a message.**  Its full
  text lives in `conversation.compacted_summary` (a column); the `user`-role
  summary *message* is re-synthesised from that column on every reload and is
  never a row in the messages table.  Folded rows are never deleted, so
  stored sequence numbers stay dense and always include everything folded.
- **Second compactions fold the first summary.**  A prior summary is treated
  as an ordinary body message (`toddler/context/summarizer.py:117-138`): it
  sits at the front of the body, and the next compaction re-feeds its full
  text to the summariser LLM.  The summary chain is therefore a nested,
  lossy re-compression (`S₂ ≈ compress(S₁ + turns aged out)`); an immediate
  second `/compact` with no new turns re-compresses `[S₁]` alone.  No-op
  guard for summary-only folds is not implemented.
- **`session.message_count` on the live `SessionManager.session` object goes
  stale.**  `append_message` updates the DB row's counter
  (`toddler/session/storage.py:206-210`) but nothing refreshes the in-memory
  session object, which read 0 after the first save while the conversation
  counter was 3.
