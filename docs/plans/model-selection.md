# Model Selection & the Turn Pin

> **Status.** Implemented, in the commits listed below: the pin and the
> provider interface flip first, then the slots, the `[1m]` notation, the
> effort budgets, the conversation's own selection, and the `/model` and
> `/effort` commands. An echo rule that an earlier draft specified as a
> follow-up was dropped instead — see "What the echo actually requires".

## Motivation

`model` and `reasoning_effort` were provider properties, captured once at
construction from `Settings` (`toddler/llm/provider.py:97-121`) — and they were
the whole of what a model was. `Settings.model` is one string from
`DEEPSEEK_MODEL` (default `deepseek-v4-pro`), the context window is a fixed 128K
(`TODDLER_MAX_CONTEXT_LENGTH`), and the output budget is a fixed 8192
(`TODDLER_MAX_TOKENS`). Four costs:

1. **Mid-turn drift.** `generate()` falls back to provider state
   (`reasoning_effort or self.effort`), so a selection change while a turn is
   running splits one turn across two models — with nothing downstream noticing.
2. **Derived state snapshotted at the wrong time.** `ContextWindowManager` and
   `TokenCounter` are keyed to `llm_provider.model` once per `ContextManager`
   and cannot re-key; `Conversation.model` (the token-baseline cache key) is
   written from the provider at save time; `TurnRunner` freezes
   `model=provider.model` at app startup while `ws.py` re-reads it per frame, so
   the same frame type can carry two different models.
3. **One model per install, and nothing legible about it.** Moving between a
   fast model and a strong one means editing the environment and restarting.
   The window and the output budget are properties of the install rather than of
   the model, so a long-context variant differs 5x from its sibling and nothing
   says which one you are on; a vendor id like `deepseek-v4-pro` carries no hint
   of the window it was accounted for. A thinking turn spends its output budget
   on reasoning *and* on the answer, so 8192 truncates
   (`finish_reason="length"`, empty `content`).
4. **A cross-model hazard in the reasoning echo-back.** `_messages_to_openai`
   attaches DeepSeek's `reasoning_content` to any assistant message carrying a
   reasoning block, with no reference to the model that will serve the request.
   That is safe only because the model never changes — the assumption recorded
   in `docs/plans/reasoning-content-capture.md:78-85`.

**Outcome.** The session owns the selection; one complete agent turn runs one
config; the provider becomes a stateless translator; a model is picked from three
named slots whose value may carry a local `[1m]` window notation; the window and
the output budget follow from the name and the effort; a conversation remembers
what it ran with, so resuming restores it and `/model` / `/effort` change it
mid-session.

**Lineage.** This record subsumes two earlier ones. `docs/plans/turn-scoped-model.md`
(875c8ef) recorded the pin; it is folded in here and deleted. The other is the
*first* `docs/plans/model-selection.md` — added in c7e0150 as "model tiers and
the `[1m]` context notation", removed in 99e1078 as "nothing in the tree
implements it". The path is reused for the design that actually lands; what
changed from that draft is listed at the end.

## Key Design Decisions

### The pin

- **`TurnConfig` is the unit of selection and of pinning.** A frozen
  `(spec, reasoning_effort)` pair, defined in `toddler/config/models.py`
  alongside the notation it is built from. The agent layer imports it at runtime
  — it started life in `session/manager.py` under a `TYPE_CHECKING` import
  because the manager imports the agent loop, and moving it under `config/`
  (which imports nothing upward) dissolves that. No `from_settings()`
  constructor: that would re-create the implicit default this change deletes.
- **The pin is a local, not a field.** `process_turn` reads the selection once
  and threads the `TurnConfig` down as a parameter. A manager-level field is not
  load-bearing and is *wrong* for nested runs: `Planner.run` drives
  `agent_loop.run` inside the same turn, so an inner teardown would clear the
  outer pin. A local also cannot leak when a consumer abandons the generator.
- **`model` becomes a required keyword argument on `generate()`.** No default:
  a default is the fallback that caused the drift.
- **The token window follows the selection, not the pin.** One writer per
  column: `conversations.model` is "the model this conversation runs with" *and*
  the key `total_tokens` is valid for. A mid-turn selection change re-keys token
  accounting immediately while the wire stays pinned — the pin's job is what
  serves the requests.
- **`--model` keeps its meaning on resume.** The row normally wins, but an
  explicit CLI flag is user intent, so it is applied after `resolve()`.

### The selection

- **Three slots, and only three names.** `default`, `pro`, `flash`, valued from
  `TODDLER_DEFAULT_MODEL` / `TODDLER_PRO_MODEL` / `TODDLER_FLASH_MODEL`, each
  defaulting to `"deepseek-flash"` so a fresh install works with one id and
  retargeting is an env var away. `--model` and `/model` accept a slot name and
  nothing else — a literal vendor id is not a selection. `TODDLER_MODEL` names
  the slot a new conversation starts on (default `default`).
- **`DEEPSEEK_MODEL` survives as the default slot's value.** When
  `TODDLER_DEFAULT_MODEL` is unset it is read as `model_default`, so an existing
  install keeps the model it had instead of silently flipping to
  `deepseek-flash`. It is no longer a selection.
- **`TODDLER_EFFORT_LEVEL` replaces `TODDLER_REASONING_EFFORT`, default
  `"high"`.** As a `default_factory` rather than a class-body assignment, so it
  re-reads the environment per instantiation — today `_env()` runs once at
  import, which is why that setting cannot be tested. The *field* stays
  `reasoning_effort`: that is the API parameter's name, and the provider's
  translation table is written against it.
- **Switching is a write-through selection.** `set_model` / `set_effort` update
  the selection, re-key the context, stamp `conversation.model` /
  `conversation.reasoning_effort`, zero `total_tokens` when the model changed,
  and persist the row. Stamping the row from the pin instead would silently roll
  a mid-turn change back on disk while memory kept the new value, and would tag
  `total_tokens` with a model that did not compute it. `save()` stamps the
  selection; `new_conversation` carries it forward so `/clear` cannot revert to
  the settings default.
- **A conversation keeps the spec it was created with.** The slot resolves once,
  at entry, and the resolved spec is what the row holds — so retargeting
  `TODDLER_PRO_MODEL` later does not re-point an old conversation at a model
  that never produced its history or its token baseline. The corollary, accepted:
  a retired id has no recovery path but `/model`.
  **Later addition (schema v5):** the row also records `model_slot` — the *name*
  of the slot a pick came from — because the spec cannot tell two slots that
  name one model apart, and the browser's picker highlights the row the user
  pointed at. This does not weaken the rule above: `model` is still what runs
  and what `total_tokens` is valid for, the slot is display provenance beside
  it, and a reader must confirm the named slot still resolves to the running
  model before showing it as the selection. See `web-model-picker.md`.
- **The effort scale is shared.** `"none"` disables thinking outright; the
  tiers above it are handed to the provider, which collapses the finer ones onto
  DeepSeek's coarser low/high/max scale.

### The notation and the budgets

- **The spec is the identity; the wire id is derived from it.**
  `deepseek-flash[1m]` is Toddler's own notation. `wire_model(spec)` →
  `deepseek-flash` is the only string that reaches the endpoint, and it is
  produced in exactly one place, never re-derived per call site. Display,
  persistence, and the token-baseline cache key all hold the **spec**, so
  "which model produced these token counts" stays answerable.
- **`TurnConfig` stores two fields and exposes five members.** `(spec,
  reasoning_effort)` are stored; `model`, `max_context_tokens`, and
  `max_completion_tokens` are read-only properties. Derived beats stored: the
  group cannot hold a spec and a window that disagree, and `dataclasses.replace`
  becomes the switch primitive.
- **The completion budget follows the *effective* effort.** `none` → 4K, `max`
  or above → 64K, otherwise 32K. An unrecognized tier takes the top budget,
  because `_DEEPSEEK_REASON_EFFORT_MAP` (`llm/provider.py:41-52`) already
  coerces an unknown tier to `max` — the local number should agree with what the
  endpoint will actually do.
- **The window and its headroom come from the turn config.**
  `ContextWindowManager` receives `max_context_length=config.max_context_tokens`
  and `output_headroom=config.max_completion_tokens`, so the hardcoded
  `_DEFAULT_OUTPUT_HEADROOM = 4096` (`context/window.py:23`) stops being what
  the agent reserves. With a 32K/64K budget the headroom is the difference
  between compacting in time and overflowing the window.
- **Two knobs retire.** `TODDLER_MAX_TOKENS` and `TODDLER_MAX_CONTEXT_LENGTH`
  (with `Settings.max_tokens_per_response` and `Settings.max_context_length`)
  are deleted: the effort level and the `[1m]` suffix are the levers, and one
  source of truth beats a field that can contradict a derived value.
- **The reasoning dialect is read off the request's model.** The echo branch asks
  only whether the model is a `deepseek-` family one — the same family test
  `_parse_params` applies to the token-budget key. No third setting: the config
  the turn already carries decides it, and a second dialect extends the test at
  the branch. A dialect we cannot echo to is *not* handled by dropping the key:
  seeing another model's reasoning is a mismatch (the endpoint would reject the
  key or absorb reasoning it never wrote), so the branch logs the model and
  raises `NotImplementedError`. That is affordable precisely because a
  non-DeepSeek slot cannot start: `main` refuses one on the way in (see "What
  the echo actually requires"), so the raise is a tripwire for a state that
  should be unreachable rather than a routine path.

## Implementation

### `toddler/config/models.py` (new)

The notation's single owner plus the config itself:

- `split_spec(spec) -> (base, suffix | None)`, `wire_model(spec)`,
  `context_length_for(spec)` built on one regex (`\[([^\]]*)\]$`). A known
  suffix yields its length; an unrecognized one **warns and falls back to the
  default window after stripping** — a bracket is never legal in an OpenAI id,
  so a confused 404 is worse than a slightly wrong estimate.
- `completion_budget_for(effort)` per the rule above.
- `resolve_slot(name, slots)` — case-insensitive lookup raising `ValueError` on
  an unknown name. Callers differ deliberately: the CLI parser validates with
  `choices=`, `Settings` warns and falls back (it is constructed at import time
  and must not raise on a stale env), and `/model` reports the error.
- `TurnConfig(spec, reasoning_effort=None)` with its three derived properties
  and the pin rationale in its docstring.

### `toddler/config/`

- `defaults.py`: `DEFAULT_MODEL = "deepseek-flash"`, `MODEL_SLOTS`,
  `DEFAULT_SLOT`, `DEFAULT_MAX_CONTEXT_LENGTH = 200_000`,
  `CONTEXT_SUFFIXES = {"1m": 1_000_000}`, the three completion budgets,
  `DEFAULT_EFFORT_LEVEL = "high"`; delete `DEFAULT_MAX_TOKENS_PER_RESPONSE`.
- `settings.py`: `model_default` / `model_pro` / `model_flash` fields, a
  `model_slots` mapping property, a `model_spec` property (the resolved spec,
  for the surfaces that legitimately read server identity rather than the
  conversation), `model` reduced to the slot name, `reasoning_effort` as a
  `default_factory`, and the deletion of `max_context_length` /
  `max_tokens_per_response`. `_cli_fields()` is unchanged.
- `__init__.py`: re-export the new names (import block and `__all__` edited
  together).

### `toddler/llm/`

- `base.py`: drop the abstract `model` property; `model` becomes a required
  keyword on `generate()` and on `generate_compact()`.
- `provider.py`: delete `_model`/`_effort` and both properties (the `effort`
  setter has zero callers); delete the `or self.effort` fallback; the three wire
  call sites take the parameter. Keep one local for the model so `_parse_params`
  and `create(model=...)` cannot drift.
- `_messages_to_openai(messages, *, model, reasoning_effort=None)`, which
  resolves the dialect from the two per the rule above.

### `toddler/agent/` and `toddler/context/`

- `AgentLoop.run(..., config)` → `_call_llm(..., config)` → `generate()`, with
  `max_completion_tokens=config.max_completion_tokens` in place of
  `settings.max_tokens_per_response`.
- `Planner.run(user_input, *, config)` forwards the config to its exploration
  loop and to its own plan-proposal request; that request keeps its own small
  budget, which is deliberate for a fixed JSON reply.
- `ContextManager` takes the `TurnConfig`, exposes `config` and `model`, hands
  the **wire** id to the token counter, builds the window from the config's
  derived values, and gains `set_config()`, which **rebuilds only the window
  manager** — a fresh instance is exactly "re-key with the baseline dropped",
  and routing it through `load()` instead would reset the compaction bookkeeping
  (`_has_compacted`, `_last_compaction`, `_baseline_count`) that `save()` is
  about to persist.
- `ConversationCompactor.compact(messages, *, model)` → `generate_compact`.

### `toddler/session/`

- `Conversation.reasoning_effort` (new nullable column, schema v4) and reworded
  `model` / `total_tokens` docstrings: the pair is what the conversation runs
  with, and `model` is the key `total_tokens` is valid for.
- `models.py`, `storage.py`, `database.py`: the v4 migration
  (`_add_column_if_missing(conn, "conversations", "reasoning_effort", "TEXT")`),
  the INSERT / UPDATE / row-mapper, and
  `create_conversation(..., model=, reasoning_effort=)`.
- `manager.py`: `self._selection: TurnConfig`, resolved in `_activate_context()`
  from the row when it has one and from the settings slots otherwise; `model` /
  `effort` / `model_slots` for the display and command layers; `set_model(slot)`
  and `set_effort(tier)`; `save()` stamping the selection; `new_conversation`
  carrying it into the new row; and `_activate_context` comparing the stored
  baseline against the window's own key (`ctx.model`) rather than a value just
  loaded from the same column — the REPL and `tod serve` share one DB with
  independent settings, so a row written by another process is a real case.
  Drive-by fix while re-keying is in scope: `switch_session()` rebuilds the
  context but leaves the cached `Planner` (`manager.py:739-768`) holding the old
  one — clear it.

### `toddler/cli/`, `toddler/main.py`, `toddler/web/`

- `/model [slot]` and `/effort [tier]`: with no argument they report the current
  spec, window, budget, and effort (and which slot the spec matches); with an
  argument they switch and report the result. Registered in `_COMMAND_TABLE`,
  listed in `HELP_TEXT`, and named in the module docstring — the three places
  that must stay in sync.
- Display reads the selection: the CLI banner and `prompt_header`
  (`cli/app.py:99,107`), and the web `session_info` frames — `TurnRunner` loses
  its constructor-frozen `model` (`web/runners.py:112-121`) so it cannot drift
  from the manager. `/api/meta` keeps reporting server identity, from
  `settings.model_spec`.
- Web classification: both commands join `_SESSION_MUTATING_SLASH`
  (`web/ws.py:136`), so they are busy-gated under `mutation_guard` and broadcast
  a `session_info_frame` — which is also why the browser input bar gets them for
  free.
- `main.py`: an explicitly passed `--model` / `--reasoning-effort` is applied
  after `resolve()`, so the flag keeps its meaning on resume; and
  `require_supported_models` refuses an unservable slot before either entry
  point starts (see "What the echo actually requires").

### What the echo actually requires

The requirement attaches to the request carrying the `tools` parameter, not to
round boundaries and not to the thinking toggle:

> Please note that for requests carrying the `tools` parameter, the
> `reasoning_content` must be fully passed back to the API in all subsequent
> requests — even for turns where the model did not perform a tool call. If your
> code does not correctly pass back `reasoning_content`, the API will return a
> 400 error. — `api-docs.deepseek.com/guides/thinking_mode`

and, from the same page, for requests that do *not* carry it: "`reasoning_content`
does not need to be passed back; even if passed to the API, it will be ignored".

So the rule the code implements is: **echo whenever a reasoning block is present
and the model is a `deepseek-` family one.** No window over the message list, and
no consultation of `reasoning_effort` — a request running with thinking off still
owes the history's reasoning back. The agent loop always sends tools; the calls
that do not (the plan proposal, compaction) have the key ignored, so one rule
covers both regimes.

**A slot naming anything else is refused at startup.** `require_supported_models`
lives in `toddler/main.py` and is called ahead of the serve branch, so both entry
points fail the same way, and ahead of any DB or LLM wiring, so nothing is
half-built when they do. All three slots are checked, not just the selected one —
`/model` can reach any of them. What that prevents is not the echo itself but
where its absence lands: mid-turn, once the history carries reasoning, with the
conversation already written to disk. The predicate (a `deepseek-` prefix on the
notation-stripped spec) sits beside the gate; the provider applies the same test
inline in `_parse_params` and at the echo branch, and the three are meant to move
together.

An earlier draft of this record specified the opposite — echo only the in-flight
round, `i > last_user`, on the theory that completed-round reasoning is ignored.
That is the no-tools rule generalised to a tool loop, and it is wrong: it would
drop the key from the history on the second request of any tool-using
conversation, which is the shape the 400 is documented for. It was dropped before
being implemented.

**The 400 could not be reproduced.** A probe against both models this account
serves (`deepseek-flash`, `deepseek-v4-pro`) sent ~20 requests across the shapes
most likely to trigger it — the documented case (thinking on, echo missing),
streamed and not, cold and warmed prefix, single- and two-round histories, a
mixed per-round echo, and through `OpenAICompatibleProvider.generate` with
`reasoning_effort="none"` — and every one was accepted. The documented case was
among them, so the probe cannot detect this class of error at all: the result is
*inconclusive*, not evidence of safety. The echo stays where the documentation
puts it, on the asymmetry that a redundant key is inert while an omitted one is a
documented failure. Recorded so the next reader neither re-derives the rule from
the page alone nor over-trusts a probe that cannot fail. (The same probe sent
histories carrying no `reasoning_content` whatsoever — the shape of pre-capture
rows — and those were accepted too, which is the empirical half of the "no
backfill obligation" claim in `reasoning-content-capture.md`.)

## Commits

Each one green (`.venv/bin/python -m pytest -q`, `.venv/bin/python -m ruff check`).

1. `refactor(llm)!: take the model and effort per call` — the interface flip and
   the pin, plus the echo branch's dialect rule. Spans layers and cannot be
   split further; `TurnConfig` temporarily lives in `session/manager.py`.
2. `docs: plan model selection and the turn pin` — this record.
3. `refactor(config): move the turn config beside the model settings` — the pure
   move to `config/models.py`, dissolving the `TYPE_CHECKING` imports.
4. `feat(config): resolve model slots, windows, and effort budgets` — defaults,
   settings, the notation, the budget table, the derived members.
5. `feat(session): persist and switch the model selection` — schema v4,
   `_selection`, `set_model` / `set_effort`, carry-forward, the `--model`
   override, display consumers.
6. `feat(cli): add /model and /effort` — the commands, the help text, the web
   classification.

## Verification

- New `tests/test_model_selection.py` (the path `tests/test_cli_args.py:107`
  already names): notation parsing and the unknown-suffix fallback; the budget
  table for `none` / `low` / `high` / `max` / `ultra` / unknown / `None`; slot
  resolution from env and the fallback for an unknown slot; `TurnConfig`
  deriving the wire id, window, and budget from one spec.
- Turn pin: drive a tool-calling turn, change the selection after the first
  event, assert the remaining requests still carry the original pair (the mock
  already records `(model, reasoning_effort)` per call — `tests/mocks.py:63-80`).
- Selection: resume restores the pair; `/clear` carries it forward; `/model pro`
  stamps the row, re-keys the tokenizer, zeroes `total_tokens`, and survives a
  reload; `/effort low` changes effort without disturbing the model.
- Window: a `[1m]` spec yields a 1M window and a 200K spec does not; the
  effective limit reflects the turn's own output budget.
- Echo: every assistant message carrying a reasoning block keeps the key,
  including with `reasoning_effort="none"`; a history carrying none is accepted
  by the endpoint (probed live — see "What the echo actually requires").
- Migration: a pre-v4 DB gains `reasoning_effort` and `_schema_version == 4`
  (extends `TestV2ToV3Migration`).
- Manual: `TODDLER_PRO_MODEL='deepseek-v4-pro[1m]' .venv/bin/tod --model pro
  "list the files"` → the header shows the spec, the wire carries
  `deepseek-v4-pro`; `/model flash` mid-session → header, `session_info`, and the
  row all follow; `sqlite3 … "select model, reasoning_effort, total_tokens from
  conversations"` agrees with the header.
- Real DeepSeek (extends the checklist in
  `docs/plans/reasoning-content-capture.md:480-482`): a two-tool-round turn still
  succeeds, a follow-up turn succeeds after prior reasoning was dropped, and
  `/compact` mid-conversation succeeds.

## Deltas from the deleted plan

- Slot env vars are per-slot (`TODDLER_PRO_MODEL`), not a suffix pattern
  (`TODDLER_MODEL_<TIER>`), and the selection variable is `TODDLER_MODEL`.
- The output budget is derived from the effort tier (4K / 32K / 64K) rather than
  pinned at a flat 32K, and `TODDLER_MAX_TOKENS` is retired rather than kept as
  its source.
- The context window defaults to 200K (was 128K) and has **no** env override:
  the `[1m]` suffix is the notation, and the suffix table is where another
  window would be added.
- Effort defaults to `"high"` from `TODDLER_EFFORT_LEVEL` rather than being sent
  unconditionally as `"max"`, and a literal model id is no longer a valid
  selection.
- The slot resolves to a spec at entry and the spec is what persists — the
  deleted plan kept tiers as a display identity.

## Deferred

- The display mirror (`self._turn`) that shows the *in-flight* model rather than
  the selection, plus `contextlib.aclosing` in the CLI turn loop. Both land with
  the first UI that can change the selection mid-turn; with `/model` and
  `/effort` busy-gated, the header cannot be stale.
