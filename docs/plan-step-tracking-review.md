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

### Duplicate step ids silently collapse

`PlanState.activate` builds an `OrderedDict` keyed by step id; nothing in
`Plan.from_json` or the proposal prompt enforces unique ids. A duplicated id
collapses into one row — the dropped step can never be updated
(`plan_update` returns "Unknown step"), and the rendered panel disagrees
with the approved plan text.

- [toddler/tools/plan.py:91](toddler/tools/plan.py#L91)
- Fix: validate/reject duplicate ids at parse time, or key steps by position.
- **Fixed**: step ids are now canonical — `Plan.from_json` assigns
  `step-N` from position and ignores LLM-proposed ids, so duplicates,
  missing ids, and arbitrary ids all normalize to the same scheme (and
  the proposal prompt no longer asks the LLM for an `id` at all).
  `depends_on` was removed entirely (steps execute strictly
  top-to-bottom), so id references exist only via the execution prompt.
  `PlanState.activate` logs loudly if a hand-built plan ever breaks the
  uniqueness invariant.  Regression tests pin canonicalization
  (dup/missing/arbitrary ids → `step-N`), stray-`depends_on` tolerance,
  and the id-free prompt schema.

### `estimated_files_touched` defaults to the step count

Plans that omit the field display (and persist via `to_json`) a step count
as the file-touch estimate — an 8-step, 2-file refactor shows "Estimated
files touched: 8".

- [toddler/agent/planner.py:206-207](toddler/agent/planner.py#L206-L207)
- Fix: default to 0, or omit the field from display when absent.
- **Fixed**: `Plan.from_json` now defaults the field to `0` when the LLM
  omits it — matching the dataclass default, so parse, display, and
  `to_json` agree (an 8-step, 2-file refactor no longer reports "Estimated
  files touched: 8").  A regression test pins the minimal-parse default.

### Plan-panel budget off-by-one

The guard `remaining >= _PANEL_CHROME_LINES` accepts the branch when only
the chrome fits: `_max_plan_visible` becomes 0 but the full chrome cost is
still subtracted — the budget is charged for a panel that renders nothing.
A plan with no rows (an empty but non-`None` step list) left `remaining`
at `chrome + 1` drained to 1, dropping a tools panel that had exactly the
rows it needed.  (The originally-cited 13-row / 1-step-plan example does
not reproduce: a 1-step plan needs 5 rows and only 4 are left above the
output minimum, so dropping it there is correct — the defect is the
unearned chrome charge, not the drop.)

- [toddler/cli/renderer.py:1017](toddler/cli/renderer.py#L1017)
- Fix: require `remaining >= _PANEL_CHROME_LINES + 1`, and only subtract the chrome when rows > 0.
- **Fixed**: the guard now demands room for at least one row past the
  chrome, and the chrome is charged only when rows are actually shown —
  budget is consumed iff the panel renders.  Regression tests pin the
  reachable edge (an empty step list no longer drains the tools pool:
  `_max_tools_visible` stays 1 and output stays at its minimum), the
  1-row plan fitting at `chrome + 1`, and the chrome-only budget leaving
  the output panel intact.

### `reject_plan` contract change: silent no-op outside `PLAN_WAITING`

The new guard turned "reject always transitions and unblocks" into
"sometimes silently ignored". Safe for the shipped CLI flow (decisions only
arrive while parked in `PLAN_WAITING`), but the public coordinator API
contract changed and there is now no way to abort an executing plan.

- [toddler/agent/planner.py:566](toddler/agent/planner.py#L566)
- Decide and document: reject-during-execution should either abort the turn or the contract change should be explicit.
- **Fixed**: the contract change is now explicit and symmetrical with
  `approve_plan` — `Planner.reject_plan` / `SessionCoordinator.reject_plan`
  return `bool` (`True` = rejection took effect, `False` = ignored as
  stale/not-waiting), and the docstrings state the contract outright:
  rejection is honored only while parked in `PLAN_WAITING`; a rejection
  arriving while the plan executes is ignored and the execution runs to
  completion.  Decision: abort-during-execution is *not* implemented —
  the pre-guard code never actually stopped the running agent loop either
  (it only cleared the plan and moved the machine while execution
  continued), and no shipped caller can reach it (the REPL input loop is
  inactive while a turn runs), so a true abort is a turn-executor
  cancellation feature, not a decision-API concern.  The CLI feedback
  branch now resets the renderer only when the rejection took effect
  (mirroring the stale-approval handling).  Tests pin the bool contract
  on both layers, including reject-during-execution → `False` with state
  untouched.

### Regression to `pending` is never rendered

`_render_due` only fires for `in_progress` transitions or full completion,
so `plan_update(status="pending")` (a valid enum value) leaves the UI
showing a stale ▶️ until the phase-end flush.

- [toddler/tools/plan.py:162](toddler/tools/plan.py#L162)
- **Fixed**: `pending` is no longer writable — `plan_update` accepts only
  `in_progress`/`completed` (`PLAN_UPDATE_STATUSES`), the initial-state
  `pending` being set by `PlanState.activate` alone.  The schema enum,
  tool description, runtime validation, and render trigger now agree on
  the same two writable statuses, so every accepted mutation is either
  render-worthy or deliberately held back.  A regression test pins
  `plan_update(status="pending")` → rejected with the state unchanged.

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
