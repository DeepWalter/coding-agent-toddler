# Turn-Scoped Model & Effort

## Motivation

`model` and `reasoning_effort` are provider properties, captured once at
construction from `Settings` (`toddler/llm/provider.py:97-121`). Three problems:

1. **Mid-turn drift.** `generate()` falls back to provider state
   (`reasoning_effort or self.effort`), so a selection change while a turn is
   running splits one turn across two models — with nothing downstream noticing.
2. **Derived state snapshotted at the wrong time.** `ContextWindowManager` and
   `TokenCounter` are keyed to `llm_provider.model` once per `ContextManager`
   and cannot re-key; `Conversation.model` (the token-baseline cache key) is
   written from the provider at save time; `TurnRunner` freezes
   `model=provider.model` at app startup while `ws.py` re-reads it per frame, so
   the same frame type can carry two different models.
3. **A cross-model hazard in the reasoning echo-back.** `_messages_to_openai`
   attaches DeepSeek's `reasoning_content` to any assistant message carrying a
   reasoning block, with no reference to the model that will serve the request.
   That is safe only because the model never changes — the assumption recorded
   in `docs/plans/reasoning-content-capture.md:78-85`.

**Outcome.** The session owns the selection; one complete agent turn runs one
`(model, reasoning_effort)` pair; the provider becomes a stateless translator;
the conversation remembers the pair, so resuming restores it.

This is the follow-up the deleted `docs/plans/model-selection.md` (removed in
99e1078) deferred as "per-session switching stays a follow-up".

## Key Design Decisions

- **`TurnConfig` is the unit of selection and of pinning.** A frozen
  `(model, reasoning_effort)` pair in `toddler/llm/config.py`. No
  `from_settings()` constructor — that would re-create the implicit default this
  change deletes.
- **The pin is a local, not a field.** `process_turn` reads the selection once
  and threads the `TurnConfig` down as a parameter. A manager-level field is not
  load-bearing and is *wrong* for nested runs: `Planner.run` drives
  `agent_loop.run` inside the same turn, so an inner teardown would clear the
  outer pin. A local also cannot leak when a consumer abandons the generator.
- **`model` becomes a required keyword argument on `generate()`.** No default:
  a default is the fallback that caused the drift.
- **The dialect flag is provider-side.** `TODDLER_REASONING_ECHO` (default on)
  is an endpoint fact like `base_url`, not a per-turn choice. It is recorded
  explicitly instead of sniffing the model name, which would break renamed or
  gateway-aliased DeepSeek endpoints. When the model-tier registry from the
  deleted plan returns, the flag belongs on the tier slot.
- **The token window follows the selection, not the pin.** One writer per
  column: `conversations.model` is "the model this conversation runs with" *and*
  the key `total_tokens` is valid for. A mid-turn selection change re-keys token
  accounting immediately while the wire stays pinned — the pin's job is what
  serves the requests.
- **`set_selection` is write-through** (row + zeroed `total_tokens` + context
  re-key). Stamping the row from the pin instead would silently roll a mid-turn
  change back on disk while memory kept the new value, and would tag
  `total_tokens` with a model that did not compute it.
- **`--model` keeps its meaning on resume.** The row normally wins, but an
  explicit CLI flag is user intent, so it is applied after `resolve()`.
- **A conversation remembers its model forever.** A retired id has no recovery
  path but a future `/model` command.

## Implementation

### `toddler/llm/`

- `config.py` (new): `TurnConfig(model, reasoning_effort=None)`, exported from
  `llm/__init__.py`.
- `base.py`: drop the abstract `model` property; `model` becomes a required
  keyword on `generate()` and on `generate_compact()`.
- `provider.py`: delete `_model`/`_effort` and both properties (the `effort`
  setter has zero callers); delete the `or self.effort` fallback; the three wire
  call sites take the parameter. Keep one local for the model so `_parse_params`
  and `create(model=...)` cannot drift — it matters if the `[1m]` notation
  returns.
- `_messages_to_openai(messages, *, echo_reasoning=True)` plus the narrowed
  echo rule (below).

### `toddler/agent/` and `toddler/context/`

- `AgentLoop.run(..., config)` → `_call_llm(..., config)` → `generate()`.
- `Planner.run(user_input, *, config)` forwards the config to its exploration
  loop and to its own plan-proposal request.
- `ContextManager` takes the model explicitly, exposes `model`, and gains
  `set_model()`, which **rebuilds** `ContextWindowManager` — a fresh instance is
  exactly "re-key with the baseline dropped". Do not route it through `load()`,
  which resets compaction bookkeeping `save()` is about to persist.
- `ConversationCompactor.compact(messages, *, model)` → `generate_compact`.

### `toddler/session/`

- `Conversation.reasoning_effort` (new nullable column, schema v4) and reworded
  `model`/`total_tokens` docstrings.
- `SessionManager._selection`, `set_selection()`, and `model`/`effort`
  properties for display. `save()` stamps the selection.
- `new_conversation` carries the selection into the new row, so `/clear` cannot
  silently revert to `settings.model`.
- `_activate_context` re-keys the window and compares the stored baseline
  against the window's own key (`ctx.model`) rather than a value just loaded
  from the same column — the REPL and `tod serve` share one DB with independent
  settings, so a row written by another process is a real case.

### `_messages_to_openai` — echo only the in-flight round

```python
last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=-1)
...
if reasoning and echo_reasoning and i > last_user:
    openai_msg["reasoning_content"] = reasoning
```

DeepSeek requires the echo only within a tool round and ignores completed-round
reasoning, so for DeepSeek this is a no-op on the wire. For every other
endpoint it removes a key it does not define — and the pin guarantees the
in-flight round came from the endpoint now serving the request, so no
per-message provenance and no name sniffing are needed. Round boundaries line
up: the compaction summary is a user message and the cancel repair appends a
user marker.

## Verification

- Full suite green after each commit.
- Turn pin: drive a tool-calling turn, change the selection after the first
  event, assert the remaining requests still carry the original pair.
- Selection: resume restores the pair; `/clear` carries it forward;
  `set_selection` zeroes `total_tokens`, re-keys the tokenizer, and survives a
  reload.
- Echo: a completed round before a later user message drops the key; an
  in-flight tool round keeps it on every assistant message.
- Migration: a pre-v4 DB gains `reasoning_effort` and `_schema_version == 4`.
- Real DeepSeek (extends the checklist in
  `docs/plans/reasoning-content-capture.md:480-482`): a two-tool-round turn
  still succeeds, a follow-up turn succeeds after prior reasoning was dropped,
  and `/compact` mid-conversation succeeds.

### Deferred

- A display mirror (`self._turn`) so the header shows the in-flight model rather
  than the selection, plus `contextlib.aclosing` in the CLI turn loop. Both land
  with the first UI that can change the selection mid-turn.
- The `/model` and `/effort` commands themselves, gated by the existing
  `TurnRunner.mutation_guard`.
