# Plan Step Tracking — Code Review Follow-ups

Findings from the multi-angle review of `feat/plan-step-tracking...main`
(commits `0a48963`..`95f30b2`), 2026-08-19. The four runtime bugs surfaced
by the review (await on sync decision calls, `_single_line` crash on
non-string JSON, flush-after-`AgentFinished` dead-lettering,
approve-after-reject misreport) were already fixed in the reviewed commits
and are verified gone. This document records the remaining work: latent
issues and refactors, with current line references.

## Latent issues

### Stale approve mid-exploration leaves the terminal in the alt screen

When `approve_plan`'s transition fails (machine not in `PLAN_WAITING`, e.g.
an external caller approves while the planner is mid-re-exploration), the
turn ends with only a debug log — no `AgentFinished` — and in streaming
mode the renderer (re-entered by `on_plan_proposed`) is never stopped or
flushed.

- [toddler/session/coordinator.py:262](toddler/session/coordinator.py#L262) — the `is_executing` gate removed the old unconditional `_run_phase` backstop.
- Fix: always yield a terminal event (or stop/flush the renderer) when the gate skips execution.
- **Fixed**: `process_turn` now tracks whether the plan loop emitted a
  terminal event and always ends a plan turn with one — the gate's skip
  path (and a plan loop that returns without a terminal event) yields
  `AgentFinished` instead of a bare debug log, so the streaming renderer
  is stopped and flushed on the first terminal event rather than stranding
  the terminal in the alt screen.  Regression tests pin both exits.

### Non-streaming mode reprints the full step list on every update

The base renderer's `on_plan_step_update` prints the complete plan block on
each `PlanStepUpdate` (every step start plus the phase-end flush), so a
5-step plan yields up to 6 full copies of the plan, burying the agent's
actual output.

- [toddler/cli/renderer.py:354](toddler/cli/renderer.py#L354)
- Fix: diff against the previously printed snapshot and print only changed rows.

### Duplicate step ids silently collapse

`PlanState.activate` builds an `OrderedDict` keyed by step id; nothing in
`Plan.from_json` or the proposal prompt enforces unique ids. A duplicated id
collapses into one row — the dropped step can never be updated
(`plan_update` returns "Unknown step"), and the rendered panel disagrees
with the approved plan text.

- [toddler/tools/plan.py:91](toddler/tools/plan.py#L91)
- Fix: validate/reject duplicate ids at parse time, or key steps by position.

### `estimated_files_touched` defaults to the step count

Plans that omit the field display (and persist via `to_json`) a step count
as the file-touch estimate — an 8-step, 2-file refactor shows "Estimated
files touched: 8".

- [toddler/agent/planner.py:195](toddler/agent/planner.py#L195)
- Fix: default to 0, or omit the field from display when absent.

### Plan-panel budget off-by-one

The guard `remaining >= _PANEL_CHROME_LINES` accepts the branch when only
the chrome fits: `_max_plan_visible` becomes 0 but the full chrome cost is
still subtracted, so a 1-row plan panel that would fit is dropped and the
tools/output panels are shorted (e.g. a 13-row terminal with a 1-step
plan).

- [toddler/cli/renderer.py:1002](toddler/cli/renderer.py#L1002)
- Fix: require `remaining >= _PANEL_CHROME_LINES + 1`, and only subtract the chrome when rows > 0.

### `reject_plan` contract change: silent no-op outside `PLAN_WAITING`

The new guard turned "reject always transitions and unblocks" into
"sometimes silently ignored". Safe for the shipped CLI flow (decisions only
arrive while parked in `PLAN_WAITING`), but the public coordinator API
contract changed and there is now no way to abort an executing plan.

- [toddler/agent/planner.py:566](toddler/agent/planner.py#L566)
- Decide and document: reject-during-execution should either abort the turn or the contract change should be explicit.

### `to_json`/`from_dict` silently drops `status`

`PlanStep.to_dict` no longer emits `status` and `from_dict` drops it on
restore. No in-tree code serializes plans today, so this is a schema
contract change for persisted/external plan blobs rather than active data
loss.

- [toddler/agent/planner.py:98-106](toddler/agent/planner.py#L98-L106)

### Regression to `pending` is never rendered

`_render_due` only fires for `in_progress` transitions or full completion,
so `plan_update(status="pending")` (a valid enum value) leaves the UI
showing a stale ▶️ until the phase-end flush.

- [toddler/tools/plan.py:162](toddler/tools/plan.py#L162)

## Refactors

### plan_update protocol duplicated across four surfaces — and contradictory

The usage instructions are spelled out in the full system prompt, the
compact system prompt, the execution user prompt, and the tool
description. The user prompt says "call plan_update alone in its own
response — do not write any text alongside it", while the system prompt
says "after each step, briefly confirm what was done" — the model gets
conflicting instructions in the same prompt stack, and status literals are
embedded in prose where a vocabulary change cannot propagate.

- [toddler/context/builder.py:63](toddler/context/builder.py#L63), [toddler/context/builder.py:93](toddler/context/builder.py#L93), [toddler/agent/planner.py:280-285](toddler/agent/planner.py#L280-L285)
- Fix: one shared constant (e.g. next to `PLAN_STEP_STATUSES`) or a generic pointer — "report progress per the plan_update tool description" — so the tool schema is the single source.

### PlanState holds the data in four shapes and inverts the layer direction

`_statuses`, `_descriptions`, `_status_snapshot`, `_step_triples`, plus a
`steps` property used only by tests — five views of the same data. And
`tools/plan.py` imports `Plan` from `toddler.agent.planner`, the only
tools→agent import in the repo, inverting the established agent→tools
direction; it becomes a hard runtime cycle the moment `PlanState` holds a
`Plan` reference at runtime.

- [toddler/tools/plan.py:14](toddler/tools/plan.py#L14), [toddler/tools/plan.py:37-40](toddler/tools/plan.py#L37-L40), [toddler/tools/plan.py:71](toddler/tools/plan.py#L71), [toddler/tools/plan.py:173](toddler/tools/plan.py#L173), [toddler/tools/plan.py:179](toddler/tools/plan.py#L179)
- Fix: move `PlanState` beside `Plan` (agent layer), give `PlanUpdateTool` a narrow duck-typed sink (`mark_step`/`is_active`/`step_ids`), and store one `OrderedDict[str, (description, status)]` with a single derived triple view used for both diffing and emission.

### Renderer plan panel special-cased in five places with duplicated math

The plan panel adds a third parallel allocator branch (`plan_reserved` is a
line-for-line mirror of `tools_reserved`), the row format is built twice
(non-streaming at 362-366, streaming at 1183, already drifting on
wrap/overflow settings), and `_plan_steps`/`_max_plan_visible` are
special-cased in init/start/stop/height-budget/build.

- [toddler/cli/renderer.py:1002-1028](toddler/cli/renderer.py#L1002-L1028), [toddler/cli/renderer.py:1183](toddler/cli/renderer.py#L1183)
- Fix: a small per-panel spec (count, chrome, per-row cost) iterated by one cost-ranked allocation loop, and one shared `_format_plan_row` helper.

### Approve idempotency spread as coordinated guards across files

The double-approval fix is a `PLAN_WAITING` guard in the planner, an
`is_executing` gate in the coordinator, and an `accepted` check in the CLI
— three layers that must agree on one "stale decision" concept. The reject
path handles staleness wholesale; approve handles it partially.

- [toddler/agent/planner.py:523-528](toddler/agent/planner.py#L523-L528)
- Fix: route both through a single `decide(approved, feedback)` that no-ops unless the machine is in `PLAN_WAITING`, or make `transition()` idempotent for same-mode targets.

### ~10 coordinator tests copy-paste the approval preamble

Each test repeats the drive-to-`PlanProposed` → `approve_plan` → collect →
filter idiom (~60 lines total), and `_collect` is defined identically in two
test classes.

- [tests/test_plan_mode.py:709](tests/test_plan_mode.py#L709)
- Fix: an `approved_run` helper/fixture plus module-level `_collect` and `_events_of(gen, cls)`.

### `take_update()` polled after every event

The per-event poll runs after `TextDelta`, `ToolCallStart`, `ToolCallDelta`,
and `ToolCallEnd` alike — a long summary yields hundreds of TextDeltas,
each triggering an O(steps) snapshot build that always returns `None`
(statuses only change at tool execution).

- [toddler/session/coordinator.py:488](toddler/session/coordinator.py#L488)
- Fix: poll only after `ToolCallEnd` (plus the existing pre-`AgentFinished` flush).

### Render-trigger policy split across three layers

"Hold back bare completed, emit on in_progress transitions" lives in
`PlanState._render_due`, while the coordinator must know to poll every
event and flush at phase end, and the renderer must know each event carries
the full list. The `flush_update` mechanism exists only to support the
hold-back heuristic; emitting on any diff would collapse it to nothing.

- [toddler/tools/plan.py:162](toddler/tools/plan.py#L162), [toddler/session/coordinator.py:481](toddler/session/coordinator.py#L481)
- Fix: let `PlanState` push to a subscribed emit sink (policy internal to one layer), or drop the trigger filter and emit every mutation — the streaming renderer's throttle already collapses adjacent frames.

### One-expression mode properties with a single call site each

`is_plan_exploring`/`is_plan_executing`/`is_executing` are three properties
each wrapping one enum comparison and each used exactly once, while the
same check elsewhere is a raw `current_mode == AgentMode.X` comparison —
two idioms for the same test.

- [toddler/agent/state_machine.py:287](toddler/agent/state_machine.py#L287), [toddler/agent/state_machine.py:292](toddler/agent/state_machine.py#L292), [toddler/agent/state_machine.py:297](toddler/agent/state_machine.py#L297)
- Fix: drop the properties and compare at the call sites, or keep at most one.

### Icon maps hardcode the status vocabulary

`_PLAN_ICONS`/`_PLAN_STYLES` hardcode the three status keys — a verbatim
re-creation of the icon map this branch deleted from `planner.py`. A fourth
status in `PLAN_STEP_STATUSES` would silently render with the fallback icon
instead of failing loudly.

- [toddler/cli/renderer.py:90-91](toddler/cli/renderer.py#L90-L91)
- Fix: derive both maps from `PLAN_STEP_STATUSES`, or add a test asserting the key sets match.
