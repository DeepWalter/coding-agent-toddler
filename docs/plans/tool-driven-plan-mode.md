# Tool-Driven Plan Mode

## Context

Plan mode currently runs as two loops. `Planner` owns an
explore → propose → wait lifecycle
([planner.py:486-608](toddler/agent/planner.py#L486-L608)) and calls
`AgentLoop` for exploration; when exploration ends, a second, tool-less LLM
call writes the plan as structured JSON. That call sees only the last five
assistant messages, capped at 4 000 characters
([planner.py:746-779](toddler/agent/planner.py#L746-L779)) — no tool results,
no file reads, most of the transcript discarded. The plan is then presented
through bespoke gating state (`PLAN_PROPOSING` / `PLAN_WAITING`), approved
through `approve_plan(plan_id)`, and execution is kicked off by a second
`AgentLoop.run` with a synthetic user message
([session/manager.py:504-583](toddler/session/manager.py#L504-L583)).

The loop already has the decision vocabulary this workflow needs:
`APPROVE` runs a paused tool, `FEEDBACK` returns the user's words for the
model to act on, and `DENY` on every call in a message ends the turn
([loop.py:455-461](toddler/agent/loop.py#L455-L461),
[loop.py:885-900](toddler/agent/loop.py#L885-L900)).

This document replaces the `Planner` with a `propose_plan` tool so one
`AgentLoop.run` covers exploration, proposal, approval and execution. The
plan is written as **markdown**, not structured JSON, and progress tracking
becomes a **general `todo_write` tool** available in any execution turn —
the Claude Code model (a markdown plan document plus a TodoWrite-style
list).

1. The model explores with the ordinary read/search tools.
2. When ready, it calls `propose_plan({title, plan_markdown})`.
3. The tool pauses like any `INTERACT` call; the user approves, rejects, or
   asks for changes.
4. Approval arms plan execution in the same loop. The model maintains a
   todo list with `todo_write`, which the session turns into progress
   events.
5. Rejection with feedback becomes the tool result and the model revises in
   place.

## Goals / non-goals

**Goals**

- One `AgentLoop.run` per plan turn: explore → propose → approve → execute.
- The plan is authored in context by the model that did the research, as
  markdown the user reads.
- Plan approval keeps the existing wire protocol (`PlanProposed`,
  `approve_plan` / `reject_plan`) and both frontends' decision UI.
- Progress tracking is decoupled from the plan and reusable in plain
  execution turns.
- Delete `Planner`, `PlanStep`, `PlanState`, `plan_update`, `PlanStepUpdate`,
  `PLAN_PROPOSING`, `PLAN_WAITING` and `PLAN_EXECUTING`; rename
  `PLAN_EXPLORING` to `PLAN`, leaving four modes: `IDLE`, `EXECUTING`,
  `PLAN`, `FINISHED`.

**Non-goals**

- Persisting plans or todos across restarts, or replaying them as
  first-class transcript blocks.
- Re-planning mid-execution.
- Enforcing read-only exploration at the registry level (today it is prompt
  plus `MANUAL` gating; unchanged).
- An audit trail of what was approved. Todos are a live progress UI and the
  model may revise them.

## Target flow

```
process_turn (one AgentLoop.run, PLAN)
  ├─ explore with read/search tools
  ├─ LLM calls propose_plan({title, plan_markdown})
  ├─ loop pauses (Permission.INTERACT)
  │    SessionManager translates AgentPaused → PlanProposed   (wire unchanged)
  │    user decides via approve_plan / reject_plan            (API unchanged)
  ├─ approve → tool runs, returns a short ack
  │    SessionManager: PLAN → EXECUTING,
  │    ContextManager.set_mode("execute"), permission mode,
  │    deregister propose_plan, register todo_write
  ├─ execute in the SAME loop; the model calls todo_write
  │    SessionManager emits TodoUpdate after each ToolCallEnd
  └─ AgentFinished / AgentCancelled / stopped
```

- **Reject with feedback** → `FEEDBACK` tool result ("The user did not
  approve this call … They asked you to change it: …"); the model revises
  and calls `propose_plan` again in the same loop.
- **Reject outright** → `DENY`; when it is the only call in the message,
  `all_denied` ends the turn with `AgentFinished(stopped=True)`.
- **Cancel** → the loop's existing `AgentCancelled` path and the session's
  cancel repair, unchanged.

## What stays the same

- `PlanProposed` and the plan approval UI: the web plan document tab, plan
  block and decision dock, and reconnect snapshot
  ([events.py:319](toddler/web/events.py#L319),
  [runners.py:344-347](toddler/web/runners.py#L344-L347),
  [App.vue:62-110](website/src/App.vue#L62-L110),
  [ConsoleDockPlanAsk.vue](website/src/components/ConsoleDockPlanAsk.vue));
  the CLI alt-screen plan panel
  ([renderer.py:1015](toddler/cli/renderer.py#L1015)).
- `SessionManager.approve_plan(*, plan_id, permission_mode)` and
  `reject_plan(*, plan_id, feedback)` — signatures and staleness semantics.
- The ws protocol for approval: `approve_plan`, `reject_plan`,
  `plan_proposed` ([ws.py:286-311](toddler/web/ws.py#L286-L311)).
- The permission gate, checkpoints, cancel repair, title requests and token
  accounting.

## Key design decisions

**1. The plan is markdown plus a short title.** `Plan` becomes
`{id, title, markdown}`; `propose_plan` takes `{title, plan_markdown}`. The
UI already renders markdown — `serialize_plan` ships
`format_for_display()`'s text and the CLI prints the same
([events.py:56-74](toddler/web/events.py#L56-L74)). The structured fields
(`steps`, `rationale`, `risks`, `estimated_files_touched`) existed to feed
the step panel and `plan_update`, not the document; once tracking is
decoupled they have no consumer. The tool description asks for concrete
sections (context, approach, ordered steps, files, risks, verification).

**2. Plan id = tool call id.** `plan.id = event.tool_id`. The existing
`plan_id` round-trip then maps directly onto `answer_tool_call`'s `tool_id`
staleness check ([loop.py:488-525](toddler/agent/loop.py#L488-L525)),
replacing the hand-rolled decision state in `Planner.approve_plan` /
`reject_plan`.

**3. The proposal tool is hidden and `INTERACT`.** `ProposePlanTool`
declares `visible = False` (its effect is `PlanProposed`) and
`in_default_registry = False` (registered for plan turns only).
`SessionManager` synthesizes `PlanProposed` in place of the tool's
`AgentPaused`; both frontends keep their plan UI unchanged.

**4. Progress is a general `todo_write` tool.** Whole-list replacement, no
ids, statuses `pending` / `in_progress` / `completed`; at most one
`in_progress` and at most 20 items; an empty list clears. It is bookkeeping
(`Permission.READ`), hidden (`visible = False`), and its `TodoState` is
shared per session. It is registered only while the turn is in `EXECUTING`
— before a plain turn, or swapped in at plan approval — never during
`PLAN`.

**5. Approval and auto-accept stay at the session facade.** A `SINGLE`
pause does not pass its chosen answer to `execute`
([loop.py:823-835](toddler/agent/loop.py#L823-L835)), and both approve rows
carry `ChoiceLevel.APPROVE`. `approve_plan(permission_mode=…)` sets gating
and answers with the matching row index; the tool never needs to
distinguish them.

**6. Approval folds plan execution into `EXECUTING`.** The approve facade
arms execution *before* answering the pause — transition `PLAN →
EXECUTING`, `ContextManager.set_mode("execute")`, deregister
`propose_plan`, register `todo_write` — so observers see the mode switch
before the gating flip.  `ToolCallEnd` arms as a fallback for an approval
answered directly through the loop.  There is no plan-execution mode: the
only plan-specific guidance (follow the approved plan in order; stop and
explain rather than deviate) rides in the tool result, and the ordinary
EXECUTE prompt is the only execution prompt. No plan-step state is armed.

**7. The todo list is session-scoped and in-memory.** It survives turns
within a conversation, is cleared when the model writes `[]` and on
`new_conversation`, and is lost on resume (as plan statuses are today).
Persisting it is a follow-up.

**8. The UI keeps one progress surface.** The web already has a `todo`
block kind and `ConsolePaneTodoBlock.vue` rendering a step snapshot
([useConsole.ts:348-369](website/src/composables/useConsole.ts#L348-L369),
[ConsolePaneTodoBlock.vue](website/src/components/ConsolePaneTodoBlock.vue)).
Todays it is fed by `plan_step_update`; the new `todo_update` frame feeds it
with `{content, status}` rows. The CLI reuses its Plan panel as a progress
panel: todo rows when present, plan-step rows during the migration.

**9. Plan models move to `tools/plan.py`.** The tool needs `Plan` at
runtime, and importing `toddler.agent.*` from the tools package risks a
cycle. `Plan` and `TodoItem` live with their tools; `agent.planner` is
deleted.

**10. The system prompt is rebuilt on a mode change.** The prompt is built
only for an empty buffer today
([manager.py:245-255](toddler/context/manager.py#L245-L255)), so the
execution phase currently runs under "PLAN (Research) — DO NOT make any
changes". `ContextManager.set_mode("execute")` after approval fixes that for
this workflow, and the same mechanism rebuilds the prompt on the next turn
after a plan turn.

**11. Tool schemas are rebuilt each iteration.** Today they are snapshotted
once at [loop.py:313](toddler/agent/loop.py#L313). Per-iteration refresh is
what lets `propose_plan` be deregistered and `todo_write` registered on
approval.

**12. Four modes: `IDLE`, `EXECUTING`, `PLAN`, `FINISHED`.** `PLAN` is
today's `PLAN_EXPLORING` — research, proposal and the approval pause all
live there. `PLAN_EXECUTING` is deleted: after approval the turn is ordinary
execution, so the mode pill returns to the gating label and `todo_write`
exists only in `EXECUTING`.

## Implementation

Eleven commits, grouped by phase; each is self-consistent with the suite
green. The old `Planner` keeps working until the switch in phase C.

### Phase A — prerequisites

#### Commit 1 — `refactor(tools): move the plan models beside the plan tools`

Remove the future import cycle before the new tools need `Plan`; no
behavior change.

- [tools/plan.py](toddler/tools/plan.py): move `_single_line`, `PlanStep`,
  `Plan` from [planner.py](toddler/agent/planner.py); drop the
  `TYPE_CHECKING` planner import.
- [planner.py](toddler/agent/planner.py): re-export the models so existing
  imports keep working until the file is deleted.
- Update runtime imports: [web/events.py:32](toddler/web/events.py#L32);
  TYPE_CHECKING: [agent/events.py:9](toddler/agent/events.py#L9); tests:
  [mocks.py:17](tests/mocks.py#L17), [test_renderer.py:24](tests/test_renderer.py#L24),
  [test_web_ws.py:26](tests/test_web_ws.py#L26),
  [test_web_events.py:22](tests/test_web_events.py#L22).
- Export `Plan`, `PlanStep` from [tools/__init__.py](toddler/tools/__init__.py).

**Tests:** full suite green; no new tests.

#### Commit 2 — `refactor(agent): refresh tool schemas each iteration`

- [loop.py](toddler/agent/loop.py): build `self._registry.to_api_schemas()`
  inside `while True`, before `_call_llm`.
- [test_agent_loop.py](tests/test_agent_loop.py): a tool registered while a
  turn runs appears in the next request's `tools`.

#### Commit 3 — `feat(context): rebuild the system prompt on mode change`

- [context/manager.py](toddler/context/manager.py):
  - `set_mode(mode)`: update `_mode`; when messages exist, rebuild with
    `_prompt_builder.build(mode, prior_conversation_summaries=None)` and
    `_replace_system_messages`.
  - `prepare_turn`: rebuild the leading system message when `mode` differs
    from `_mode` and the buffer is non-empty.
  - `load()`: reset `_mode` to a sentinel so the first turn after a reload
    rebuilds (the persisted prompt may be stale from a previous process);
    guard `_auto_compact`'s `build_compact` against the sentinel.
- New [tests/test_context_mode.py](tests/test_context_mode.py): same mode
  does not rebuild; a switch replaces the leading system message; reload
  forces a rebuild.

### Phase B — the general todo tool

#### Commit 4 — `feat(tools): add the general todo_write tool`

Backend and wire only; the frontends ignore the new frame until commits 5-6.

- New [tools/todo.py](toddler/tools/todo.py):
  - `TodoStatus(StrEnum)`: `pending`, `in_progress`, `completed`.
  - `TodoItem` dataclass (`content`, `status`, `to_dict`); `MAX_TODOS = 20`.
  - `parse_todos(raw)`: 0–20 dicts; non-empty single-line content; valid
    status; at most one `in_progress`; empty list clears; raises
    `TodoValidationError` with a model-actionable message.
  - `TodoState`: `items`, `replace(items)`, `take_update()` (snapshot-diff,
    like `PlanState.take_update`), `reset()`.
  - `TodoWriteTool(BaseTool)`: name `todo_write`; description carries the
    protocol (send the whole list each call; one `in_progress`; mark
    completed immediately; `[]` when done); parameters
    `{todos: [{content, status}]}`; `require_description_parameter=False`;
    `visible=False`; `in_default_registry=False`; `permission → READ`;
    `execute` parses, replaces the state, returns a short ack.
  - `TODO_TOOL_NAME = "todo_write"`.
- [agent/events.py](toddler/agent/events.py): `TodoUpdate(items)`.
- [session/manager.py](toddler/session/manager.py):
  - `self._todo_state = TodoState()`; a `todos` property for the web
    snapshot.
  - The tool opts out of the default set (`in_default_registry=False`)
    because it needs the session's state. Register it for execution phases
    only: in the pre-switch code that is `_run_execution` (activate at
    entry, deregister in `finally`), so a `PLAN` turn's exploration never
    sees it.
  - `_run_phase`: after yielding each `ToolCallEnd`, emit
    `TodoUpdate(self._todo_state.take_update())` when the list changed.
    Both execution paths use `_run_phase`, so plan and plain turns get it.
  - `new_conversation`: `self._todo_state.reset()`.
- [web/events.py](toddler/web/events.py): serialize as
  `{"type": "todo_update", "todos": [{"content": ..., "status": ...}]}`.
- [context/builder.py](toddler/context/builder.py): the `EXECUTE` prompt
  (and compact variant) tells the model to maintain a todo list with
  `todo_write` for multi-step work.
- New [tests/test_tools_todo.py](tests/test_tools_todo.py): parse bounds,
  statuses, one-`in_progress` rule, empty-clear, state replace/update,
  execute, visibility, schema. Session test: a `todo_write` call produces a
  `TodoUpdate` after `ToolCallEnd`; a replacement produces the next one;
  `[]` clears. Web test: the frame shape.

`plan_update` remains registered for the old plan path; both progress
events can coexist during the migration.

#### Commit 5 — `feat(cli): render the todo checklist`

- [cli/renderer.py](toddler/cli/renderer.py): add `on_todo_update` to the
  base, streaming and non-streaming renderers, mirroring
  `on_plan_step_update` ([renderer.py:444](toddler/cli/renderer.py#L444),
  [1031](toddler/cli/renderer.py#L1031),
  [1795](toddler/cli/renderer.py#L1795)).
- Reuse the Plan panel as a single progress panel: todo rows when the todo
  list is non-empty (title "Todos"), else plan-step rows (title "Plan").
  Status glyphs/styles come from `TodoStatus`; the height budgeting
  (`_max_plan_visible` → a shared visible count) stays as is.
- [test_renderer.py](tests/test_renderer.py): todo rows render, statuses
  map, the panel hides when empty, plan rows remain during migration.

#### Commit 6 — `feat(web): render the todo checklist`

- [types.ts](website/src/types.ts): `TodoItem`, `TodoUpdate` frame, and the
  `todo` block's `steps: PlanStepRow[]` becomes `items: TodoItem[]`.
  `hello.todos` added to the hello payload.
- [useConsole.ts](website/src/composables/useConsole.ts): handle
  `todo_update` — replace the list, close prose/thinking like a tool call,
  print a checklist block at the moments worth reading (an item starting,
  or all done), mirroring the `plan_step_update` case.
- [ConsolePaneTodoBlock.vue](website/src/components/ConsolePaneTodoBlock.vue):
  render `{content, status}` rows; keep the status marks (`*`, `✓`).
- [ws.py:91](toddler/web/ws.py#L91): `_hello_frame` carries
  `"todos": [item.to_dict() for item in state.session_mgr.todos]` so a
  reconnecting tab restores the list; `new_conversation` clears it and
  rebroadcasts `hello`.
- Tests: frame serialization, hello payload, reducer snapshot, renderer.

### Phase C — the plan switch

#### Commit 7 — `feat(tools): add the markdown propose_plan tool`

Unwired; the old Planner path is untouched.

- [tools/plan.py](toddler/tools/plan.py):
  - `Plan` gains `markdown: str = ""`; `format_for_display()` returns
    `markdown` when set, else today's structured rendering (temporary).
  - `Plan.from_arguments(parameters, *, plan_id) -> Plan`: validates a
    non-empty `title` and `plan_markdown`, ignores unknown keys; raises
    `PlanValidationError`.
  - `PROPOSE_PLAN_NAME = "propose_plan"`; choice indices
    `PLAN_CHOICE_APPROVE`, `PLAN_CHOICE_APPROVE_AUTO`,
    `PLAN_CHOICE_REJECT`, `PLAN_CHOICE_FEEDBACK`.
  - `ProposePlanTool(BaseTool)`: parameters
    `{title, plan_markdown}`; description carries the plan guidelines
    (context, approach, ordered steps, files, risks, verification; concrete
    and actionable); `INTERACT`; `visible=False`;
    `in_default_registry=False`; `require_description_parameter=False`;
    `_prompt_title` "Approve this plan?"; `_prompt_content` =
    `plan.markdown`; `_prompt_description` = plan title; `_prompt_choices` =
    four `SINGLE` rows (Approve, Approve and run automatic, Reject, Tell
    Toddler what to change); `execute` returns a short ack telling the model
    to execute the approved plan in order, stop and explain if a step
    cannot be completed as written, and maintain a todo list with
    `todo_write`.
- New [tests/test_tools_propose_plan.py](tests/test_tools_propose_plan.py):
  schema, `from_arguments` validation, prompt rows/content, execute ack,
  `INTERACT`, hidden.

#### Commit 8 — `refactor(session): drive plan mode through the agent loop`

The core switch; deletes the `Planner`.

- [context/builder.py](toddler/context/builder.py):
  - `_PLAN_EXPLORING_INSTRUCTIONS`: research only, then call
    `propose_plan` with the complete markdown plan; do not write the plan
    as prose or stop to ask in text.
  - Delete `_PLAN_EXECUTING_INSTRUCTIONS` and its compact variant (and
    their map entries): approval switches the prompt to `execute`, so the
    plan-execution prompt has no caller.
- [session/manager.py](toddler/session/manager.py):
  - Remove `_planner`, the `planner` property, `_run_execution`,
    `terminal_event_seen`, and all `PlanState` / `plan_update` arming.
  - New fields `_proposed_plan: Plan | None`, `_plan_tool_id: str | None`.
  - `process_turn`: classify as today; on `EXECUTING` register
    `todo_write` before the run. When `is_plan_exploring`, set
    `PermissionMode.MANUAL` and `_activate_plan_exploration()` — register
    `ProposePlanTool`, with a symmetric `_deactivate_plan_exploration()`.
    Run **one** `_run_phase(..., mode_hint="plan_exploring")`; in the event
    loop:
    - `ToolCallStart(propose_plan)` →
      `_proposed_plan = Plan.from_arguments(event.input, plan_id=event.tool_id)`
      (store `None` on failure); remember the id.
    - `AgentPaused` whose `tool_id == _plan_tool_id` → yield
      `PlanProposed(plan=_proposed_plan)` instead of the pause. The pause
      is already answerable: `_pending` is set before `AgentPaused` is
      yielded ([loop.py:640-647](toddler/agent/loop.py#L640-L647)).
    - `ToolCallEnd(propose_plan)` with `result.success` → `_arm_execution()`
      (the approve facade normally got here first): transition
      `PLAN_EXPLORING → EXECUTING`, call `self._ctx.set_mode("execute")`,
      deregister `propose_plan`, register `todo_write`, clear
      `_plan_tool_id`. No `PlanState` activation.
    - `finally`: `_deactivate_execution_tools()` and
      `_deactivate_plan_exploration()` (both idempotent).
  - `approve_plan(*, plan_id, permission_mode=None)`: return `False` when
    `plan_id != _plan_tool_id`; otherwise
    `accepted = self.agent.answer_tool_call(plan_id, [PromptAnswer(choice=PLAN_CHOICE_APPROVE)])`;
    on success, apply `permission_mode` (no `await` between the answer and
    the gating flip).
  - `reject_plan(*, plan_id, feedback="")`: same staleness check; answer
    with `PLAN_CHOICE_FEEDBACK` + content, or `PLAN_CHOICE_REJECT`; clear
    `_plan_tool_id` / `_proposed_plan` on success.
- **Delete [planner.py](toddler/agent/planner.py)** and its export from
  [agent/__init__.py](toddler/agent/__init__.py); delete
  `PLAN_RESPONSE_FORMAT`, `plan_proposal_prompt` and
  `Plan.format_for_prompt`. Keep `PlanStep`, `Plan.from_json` and the
  structured fields for one more commit (commit 9 removes them).
- **Demo provider [llm/_async_openai.py](toddler/llm/_async_openai.py)**
  (both dummy classes):
  - the `"plan"` keyword emits an intro plus a `propose_plan` tool call
    with `{title, plan_markdown}`;
  - the approval result starts the execution beats with a `todo_write` call
    (two items, one in progress); subsequent `todo_write` results advance
    the canned list to completed and then the closing summary;
  - remove `_PLAN_PROPOSAL_MARKER`, `_PLAN_APPROVED_MARKER`,
    `_PLAN_STEP_LINE_RE`, `_PLAN_UPDATE_RE` and the `plan_update` beats;
  - update the `"edit"` demo's old/new strings to the new markdown
    `_PLAN_PROPOSAL` shape.
- **Rewrite the session/planner sections of
  [tests/test_plan_mode.py](tests/test_plan_mode.py):** proposal
  synthesis, approve/reject/stale, one-run counts, executing system prompt,
  `todo_write` progress, feedback re-proposal, invalid plan retry, cancel,
  title timing.

`plan_update` is now dead (no session registers it) but its code and unit
tests remain until commit 9.

### Phase D — cleanup

#### Commit 9 — `refactor(tools): retire plan_update and plan steps`

Cross-layer because removing an event must remove every consumer.

- Delete `PlanStep`, `PlanStepStatus`, `PlanState`, `PlanUpdateTool`,
  `PLAN_UPDATE_USAGE`; delete the `PlanStepUpdate` event and its
  `plan_step_update` frame.
- Simplify `Plan` to `{id, title, markdown}`; remove `steps`, `rationale`,
  `risks`, `estimated_files_touched`, `format_for_display`'s structured
  branch, and `from_json` / `to_json` if unused.
- [web](website/src): remove the `plan_step_update` reducer case, the
  plan block's `steps`, `PlanSnapshot.steps`, `PlanStep` / `PlanStepRow`
  types; the plan line stays title-only and the document uses `plan.markdown`.
  The todo block keeps rendering `todo_update` items.
- [cli/renderer.py](toddler/cli/renderer.py): remove the Plan panel's step
  branch and `on_plan_step_update`; the progress panel renders todos only.
  The plan text still prints in the output panel on `PlanProposed`.
- Remove the `plan_update` / `PlanState` / `PlanStep` tests; update web and
  renderer tests to the simplified shapes.

#### Commit 10 — `refactor(agent): collapse the plan modes to four`

- [state_machine.py](toddler/agent/state_machine.py): remove
  `PLAN_PROPOSING`, `PLAN_WAITING` and `PLAN_EXECUTING`; rename
  `PLAN_EXPLORING` → `PLAN`. Transitions become `PLAN → EXECUTING |
  FINISHED`; delete `is_plan_executing` and simplify `is_plan_related` /
  `display_label` / `get_mode_hint` (→ `"execute"` / `"plan"`); update the
  module diagram.
- [context/builder.py](toddler/context/builder.py): rename the instruction
  constant and map key `"plan_exploring"` → `"plan"` (`_PLAN_INSTRUCTIONS`);
  the `plan_executing` entry was deleted in commit 8.
- [session/manager.py](toddler/session/manager.py): classification targets
  `AgentMode.PLAN`; `mode_label` and gating checks read the renamed
  property. Approval already targets `EXECUTING` from commit 8.
- [runners.py:68](toddler/web/runners.py#L68) and
  [ws.py:347](toddler/web/ws.py#L347): gating is frozen only while the mode
  is `PLAN` — replace `is_plan_related and not is_plan_executing` with the
  plan check. After approval the pill shows the gating label.
- Update the transition test in
  [test_web_ws.py:2009](tests/test_web_ws.py#L2009) (PLAN → EXECUTING
  directly) and the state-machine tests.
- Refresh stale wording: [session/models.py:57](toddler/session/models.py#L57).

#### Commit 11 — `docs: document the tool-driven plan workflow`

- [CLAUDE.md](CLAUDE.md): drop "planner" from the `agent/` architecture
  line.
- [implement-plan-mode.md](docs/plans/implement-plan-mode.md): superseded
  note pointing here; update the state diagram in
  [plan.md](docs/plans/plan.md).

## Test matrix

| Area | Test |
|------|------|
| Plan model | `from_arguments`: title/markdown validation, unknown keys, id from the call |
| Plan tool | schema, prompt rows/content, execute ack, hidden, `INTERACT` |
| Todo tool | parse bounds/statuses/one-in-progress/empty-clear, state replace + snapshot |
| Session (todos) | `TodoUpdate` after each change; works in plain and plan turns; cleared on new conversation |
| Session (plan) | proposal synthesis, approve/reject/stale, one run per turn, `PLAN → EXECUTING` on approval, no `AgentPaused` forwarded |
| Execution | `EXECUTE` prompt after approval, `propose_plan` deregistered, `todo_write` swapped in, progress emitted |
| Modes | four-mode transitions; gating frozen only in `PLAN`; `todo_write` absent in `PLAN` |
| UI protocol | `PlanProposed` / `todo_update` frames; `hello.todos`; `plan_step_update` gone |
| Web | reconnect during the plan pause; todo checklist renders; plan doc uses markdown |
| CLI | progress panel shows todos; plan text prints on proposal |
| Cancellation | Escape at the plan pause → `AgentCancelled` + repair |
| Demo | `TEST=cli` `"plan"` runs the new flow end to end |

## Verification

```bash
# targeted, in order
.venv/bin/python -m pytest tests/test_tools_todo.py tests/test_tools_propose_plan.py \
    tests/test_agent_loop.py tests/test_context_mode.py -q
.venv/bin/python -m pytest tests/test_plan_mode.py tests/test_renderer.py \
    tests/test_web_events.py tests/test_web_ws.py -q
# full suite (~12 min)
.venv/bin/python -m pytest -q
# offline demo (dummy provider)
TEST=cli .venv/bin/tod "plan"
```

Manual matrix: `/plan` → explore → approve (manual), approve (auto),
reject-with-feedback, reject; Escape at the plan pause; web reconnect
mid-plan; todo checklist in a plain execute turn; plan document tab renders
markdown.

## Behavior changes to accept

- A plan turn can end **without a plan** if the model never calls
  `propose_plan`; today the proposal call is unconditional. The exploration
  prompt and tool description must make the call the required endpoint. Do
  not add a retry round.
- An invalid plan is a **retryable tool error** instead of
  `FatalAgentError`.
- Reject-outright reports `AgentFinished(reason="All tool calls were denied
  by the user.", stopped=True)`; the session can rewrite the reason if the
  wording matters.
- Proposal tokens now count toward context and session usage; they were
  dropped in `_generate_plan`.
- A mid-turn system-prompt rebuild invalidates the provider's prompt cache
  once per plan approval.
- The plan loses its machine-readable `files_affected` / `risks` fields;
  they live in the markdown only.
- Progress is model-authored and mutable — it is not an audit of the
  approved plan. The frozen markdown document remains the contract.
- The mode pill shows the gating label (`MANUAL` / `AUTO`) once a plan is
  approved; it no longer reads `PLAN` during execution.
- Approved-plan guidance lives in the approval tool result, not a
  plan-execution system prompt.
- `todo_write` is absent from the planning model's tool list; it appears on
  the first request after approval.

## Review checklist / open questions

1. **Todo lifecycle.** Decision: session-scoped in-memory, survives turns,
   cleared by `[]` and `new_conversation`, lost on resume. Confirm this is
   enough for v1.
2. **One `in_progress` max.** Decision: enforce it in `parse_todos` (the
   agent executes sequentially). Confirm rather than merely advising.
3. **Todo cap.** `MAX_TODOS = 20`. Confirm.
4. **`todo_write` is execute-only.** Decision: registered when a turn
   enters `EXECUTING`, swapped in at plan approval; the planning model never
   sees it. Confirm the swap timing — per-iteration schemas make it visible
   on the first post-approval request.
5. **Title + `plan_markdown`** rather than markdown alone — the tab and
   pill need a short label. Confirm the field naming (`plan_markdown`).
6. **Single progress panel vs two.** Decision: reuse the plan panel as a
   generic progress panel during migration (todos win when present), then
   drop the plan branch in commit 9. Confirm the CLI should not show two
   panels.
7. **Reconnect snapshot.** Decision: `hello.todos` from the session's
   `TodoState`; live frames update it. Confirm a reconnect should restore
   the todo list.
8. **`propose_plan` deregistration on approval.** Decision: yes, so the
   model cannot propose again mid-execution. A second call in the same
   message fails as "Unknown tool" — acceptable.
9. **Demo fixture.** `_chunks_for_edit` uses `_PLAN_PROPOSAL`'s serialized
   text; it must be rewritten for the markdown shape. Confirm the offline
   demo still matters.
10. **Not in scope:** persisting todos across restarts, replaying plans or
    todos as transcript blocks, re-planning mid-execution, a dedicated
    `PLAN` `ChoiceType`, changing the approval UI, and registry-level
    read-only enforcement.
11. **Mode pill after approval.** Decision: it shows the gating label
    (`MANUAL` / `AUTO`), not `PLAN`. If the pill should keep reading PLAN
    while the approved plan runs, add a presentation flag
    (`has_active_plan`) rather than a mode.
12. **`PLAN_EXPLORING` → `PLAN` rename.** Mechanically touches the enum,
    builder keys, session, web gating checks and tests. Confirm the naming.

## Definition of done

- Explore → propose → approve → execute → todo updates run in one
  `AgentLoop.run`; feedback re-proposes in the same turn.
- The plan is markdown; `propose_plan` takes `{title, plan_markdown}`.
- Four modes remain: `IDLE`, `EXECUTING`, `PLAN`, `FINISHED`. `todo_write`
  works in every `EXECUTING` turn — plain or after approval — renders in
  both frontends, and is absent during `PLAN`.
- `approve_plan` / `reject_plan`, `PlanProposed`, and the ws approval
  protocol are unchanged for both frontends.
- `rg "Planner|PlanStep|PlanState|plan_update|PlanStepUpdate|PLAN_PROPOSING|PLAN_WAITING|PLAN_EXECUTING|plan_proposal_prompt|PLAN_RESPONSE_FORMAT|format_for_prompt"`
  over `toddler/` is clean (the `_async_openai.py` edit fixture excepted).
- Full suite green; the offline demo exercises the new flow end to end.
