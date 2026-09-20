# The model + effort pill

> **Status.** Implemented, in the commits listed below.

## Motivation

The web input bar showed the model as inert text and showed the thinking
effort nowhere at all.  Both were reachable — `/model pro` and
`/effort low` typed into the box route through the slash dispatcher and
had since model selection landed — but the only affordance for either was
knowing the commands.

The switch itself needed nothing new.  `SessionManager.set_model(slot)` and
`set_effort(tier)` (`toddler/session/manager.py`) already persist to the
conversation row and re-key the token accounting; what was missing was a
control, and the state to drive one: `session_payload`
(`toddler/web/runners.py`) carried `model` (the resolved spec) but neither
the effort tier nor the slots it could be switched to, so a picker had
nothing to reflect.

**Outcome.** One pill in the input bar, reading `spec tier` — and a four-row
menu behind it: the three slots, then the effort control.

## Key design decisions

- **All eight tiers are stops, in the server's own order.**  They are the
  vocabulary every other surface speaks — `--reasoning-effort`,
  `TODDLER_EFFORT_LEVEL`, `/effort` — so a control offering a subset could
  neither show a tier a conversation was actually set to nor set the tier
  it showed.  This replaced an earlier four-stop design (`none`, `low`,
  `high`, `max`) that offered only what the endpoint distinguishes; what it
  bought in brevity it lost in honesty, since a conversation sitting on
  `xhigh` had no stop of its own to sit on.  The accepted cost: the
  endpoint collapses three pairs (`minimal`/`low` → `low`, `medium`/`high`/
  `xhigh` → `high`, `max`/`ultra` → `max`, `_DEEPSEEK_REASON_EFFORT_MAP` in
  `toddler/llm/provider.py`), so moving within a pair changes the label and
  none of the request.  That is the endpoint's granularity, and
  `website/src/effort.ts` says so where the scale is defined.
- **The label names the stored tier, and the knob sits on it.**  The two
  agree because a stop *is* a tier; the label exists for the two cases
  where no stop applies (below).  Unset (`null` — a row written before the
  column existed, or `set_effort(None)`) parks at `high`, which is both
  DeepSeek's own default and the 32K budget band `completion_budget_for`
  already assigns it, and reads `default`.
- **An unrecognized tier parks at `max` and keeps its spelling.**  Nothing
  validates `TODDLER_EFFORT_LEVEL`, so a bad value really does reach a
  conversation row; the knob takes `max` because that is where the
  provider's `.get(tier, "max")` coercion sends the request, and the row
  still shows the tier the server holds rather than renaming it.
- **The pill is disabled while a turn runs**, matching the busy gate on
  `/model` and `/effort`.  This keeps the deferral recorded in
  `model-selection.md` ("the display mirror … lands with the first UI that
  can change the selection mid-turn") intact rather than quietly
  overtaking it: with no mid-turn switch, the header cannot go stale.
- **Re-picking the selected row is not sent.**  The comparison is against
  the slot, not the spec: two slots routinely name one model, and picking
  the other one is a real change (the highlight is what moves).  See "Which
  row is selected" below.
- **The menu stays open on a model pick** (the mode menu closes on choose):
  it is a picker you compare within, and the `session_info` echo moves the
  highlight while it is still visible.
- **The effort row's label cycles; the rail's arrows clamp.**  A cycle that
  runs off the top belongs back at `none`; a *slider* that jumped `max` →
  `none` on one keypress would turn thinking off behind the user's back.
  The rail is a `role="slider"` inside the menu's `role="menu"` — a group
  member rather than a `menuitemradio`, because announcing a slider as one
  more radio would describe a widget that is not there.
- **A drag commits once, on release.**  The knob tracks the pointer and the
  row names the stop under it; nothing reaches the wire until `pointerup`.
  Committing on each crossing would re-key the context, write the row and
  broadcast to every tab mid-gesture, and would make an abandoned drag
  impossible to undo.
- **The two input-bar popups exclude each other through a shared ref**
  (`website/src/composables/usePicker.ts`).  Each picker's own
  outside-`pointerdown` handler covers the pointer path — one trigger is
  always outside the other's root — but the keyboard path has no such
  event: focus the mode pill, Enter, Tab past its three rows, Enter on the
  model pill, and two independent `open` refs would leave both menus up, a
  few dozen pixels apart and overlapping.
- **The per-slot window is computed server-side.**  `[1m]` is Toddler's own
  notation and the suffix table lives in `toddler/config/models.py`;
  re-deriving it in the browser would be a second copy, in another
  language, of `split_spec` and its unknown-suffix fallback.

## Files

**Backend**

- `toddler/session/models.py`, `database.py`, `storage.py` — the
  `model_slot` column and its v5 migration, INSERT/UPDATE and row mapper.
- `toddler/session/manager.py` — `model_slot` (the row's slot, or the
  settings' for a row that names none), `set_model` stamping the canonical
  name, `_apply_selection` taking it as an optional argument so an effort
  switch leaves it alone, and `new_conversation` carrying it forward.
- `toddler/web/runners.py` — `session_payload` gains `model_slot`, `effort`
  and `model_slots` (`{name, spec, context_tokens}` per slot, in
  `MODEL_SLOTS` order).  Both frames that carry the payload — `hello` and
  `session_info` — get them, so a reconnect and a mid-turn transition are
  both enough to render the picker.
- `toddler/web/ws.py` — `set_model` / `set_effort`, mirroring
  `_cmd_set_mode`: validate, mutate under `mutation_guard`, ack, broadcast
  `session_info`, no notice.

**Frontend**

- `website/src/effort.ts` (new) — the stop scale (the server's own eight
  tiers) and both directions of the tier↔stop mapping.
- `website/src/composables/usePicker.ts` (new) — which input-bar popup is
  open, shared by both pickers.
- `website/src/components/ModelPicker.vue` (new) — the pill, the four-row
  menu and the rail.
- `website/src/types.ts`, `useConsole.ts` — `SessionInfo.model_slot` /
  `.effort` / `.model_slots`, `ModelSlotInfo`, the two commands and their
  senders (no optimistic action: only the server holds the slot→spec table).
- `InputBar.vue` (which now renders the picker where the model text was,
  and uses `usePicker` for its own popup), `ConsoleDock.vue`, `App.vue` —
  two props down, two events up.
- `website/src/styles.css` — the pill, the menu rows, the rail.  The popup
  surface is shared with `.mode-menu` rather than duplicated, since both
  open from `.input-bar-meta` at the same level.

## Which row is selected

As first built, the highlight matched **specs**: every slot naming the live
model read selected.  On a default install (all three slots on
`deepseek-flash`) that meant three selected rows, and a pick between them
changed nothing visible — honest, since the conversation stores only the
resolved spec, but useless as a picker.

So the slot is now recorded.  `conversations.model_slot` (schema **v5**)
holds the name of the row the user pointed at, stamped by `set_model` and
carried into a new conversation by `/clear`.  The migration deliberately
does **not** backfill it: the slot an existing row was picked by is not
recorded anywhere, and guessing one from the settings would claim a
provenance the row never had.

**This does not reopen `model-selection.md`'s decision.**  The substance of
that decision is that the *spec* is what runs and what `total_tokens` is
valid for, so a retargeted slot cannot re-point an old conversation at a
model that never produced its history.  That is untouched: `model` is still
the spec, still stamped by `save()`, and the slot is a display field beside
it.  It is written as provenance in the model's docstring, and readers are
told to check it rather than trust it.

The check is the whole of the frontend's rule:

- **The stored slot is highlighted when it still names the live model** —
  the row the user picked.
- **Otherwise the highlight falls back to matching specs** — because a slot
  retargeted underneath a live conversation (`TODDLER_PRO_MODEL` changed
  since) names a model this conversation does not run, and highlighting it
  would claim otherwise.  A row written before the column existed takes the
  same path, and the manager reports the settings' slot for it — which is
  the slot `_activate_context` resolves its model from in exactly that case,
  so the answer is the truth rather than a guess.
- A pick is suppressed only when it re-picks **the stored slot**: two slots
  routinely name one model, and picking the other one is a real change —
  the highlight is what moves.

The CLI's `/model` still emphasizes every slot whose spec matches, since it
has no reason to change and the report is a summary rather than a control.

## Harness coverage

`website/scripts/ui-test.mjs` phase 11, against a mock whose `default` and
`flash` slots name **the same model** (so a pick between them is a pure slot
change) and whose `pro` names a `[1m]` spec: the pill's text; the three rows
with their specs and windows and exactly one selected; a pick between two
slots naming one model moving the highlight and nothing else; a pick that
changes the spec moving both; the label cycling; a drag that tracks live,
commits exactly once, and lands on the nearest stop; the arrows stepping and
clamping; the busy gate closing the menu and disabling the pill; another
tab's tier moving an open menu, and a collapsed tier keeping its name; the
pill **and the picked row** replaying after a reload; a slot retargeted
underneath the conversation dropping the highlight to whatever names the
running model; and the two popups excluding each other on the keyboard path.

The mock gained three backdoors: `set_busy`, because a real turn in flight
replaces the input bar with the tool gate and the busy state the pill must
react to is otherwise unreachable; `get_mutations`, which makes "one command
per drag" assertable from outside the page; and `retarget_slot`, which
repoints a slot the way restarting with a new `TODDLER_<SLOT>_MODEL` would,
leaving the conversation's own model and slot untouched — the divergence the
fallback exists for.

## Verification

- `.venv/bin/python -m pytest -q` (658 passed), `.venv/bin/python -m ruff check`.
- Migration: a v2 database gains `model_slot` and ends at v5
  (`TestV2ToV3Migration`); the provenance behaviour is
  `TestModelSlotProvenance` in `tests/test_model_selection.py` — including
  the case the field exists for, two slots naming one model.
- `cd website && npm run build` (`vue-tsc --noEmit` then Vite).
- The harness above: `node scripts/ui-test.mjs` against a **fresh** mock
  (`node scripts/mock-server.mjs`) — a run consumes the seeded transcript,
  so a second run against the same process fails at the seeding wait.
- Manual, against `tod serve`: the pill reads `deepseek-flash high`; a slot
  retargeted with `TODDLER_PRO_MODEL='deepseek-v4-pro[1m]'` moves the pill
  to that spec and the context gauge re-keys; the rail at `none` produces a
  turn with no thinking block; a second tab's pill follows the first's
  switch.
