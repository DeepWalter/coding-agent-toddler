# Plan Mode Implementation Plan

## Context

Plan mode is designed to give Toddler a structured workflow for complex tasks:
**explore → propose plan → user approves/rejects → execute**. This prevents the
agent from jumping straight to implementation on multi-file changes.

The architecture was designed in Phase 10 of [plan.md](plan.md) and marked
complete, but only the **foundation** was built — the state machine, data model,
events, slash commands, and system prompts all exist. The critical
**orchestration bridge** that drives the full lifecycle was never implemented.
The result is dead code: entering `/plan` changes the system prompt but the
agent never proposes a plan, never asks for approval, and never tracks execution
progress.

### What's implemented vs what's missing

| Component | Status |
|-----------|--------|
| `AgentStateMachine` (7 modes, transitions, approve/reject) | ✅ Built |
| `Plan` / `PlanStep` dataclasses with JSON serialization | ✅ Built |
| `classify_complexity()` heuristic | ✅ Built |
| `/plan` slash command + `--plan` CLI flag | ✅ Wired |
| `PlanProposed` event type | ✅ Defined, **never yielded** |
| Mode-specific system prompts (explore, execute) | ✅ Built |
| `plan_proposal_prompt()` | ✅ Built, **never called** |
| `should_auto_approve_tool()` | ✅ Built, **never called** |
| `approve_plan()` / `reject_plan()` | ✅ Built, **never called** |
| Coordinator multi-phase orchestration | ❌ Missing |
| Agent loop plan mode awareness | ❌ Missing |
| Plan approval/rejection UI | ❌ Missing |
| Plan display (steps, risks, rationale) | ❌ Partial |
| Enforced read-only during exploration | ❌ Advisory only |
| Tests for plan mode | ❌ None |

---

## Design Decisions

### 1. Orchestration lives in `SessionCoordinator.process_turn()`

The coordinator owns both the state machine and the agent loop — it is the
natural place for multi-phase orchestration. `process_turn()` becomes a
state-machine-driven loop instead of a single pass through the agent.

### 2. Plan proposal uses a non-streaming LLM call

After exploration finishes, the coordinator sends `plan_proposal_prompt()` as a
follow-up message to the LLM (non-streaming, since the JSON response is small).
This avoids complicating the streaming agent loop with a special proposal phase.

### 3. Plan approval uses the existing confirmation UI

The `PlanProposed` handler in `CLIApp` is extended with a `confirm()` call
(approve/reject/reject-with-feedback), mirroring the existing `AgentPaused`
pattern. The in-alt-screen table navigation from
[in-alt-screen-confirmation.md](in-alt-screen-confirmation.md) is reused.

### 4. Tool gating consults the state machine

`AgentLoop._needs_confirmation()` is extended to consult
`AgentStateMachine.should_auto_approve_tool()` before falling back to
settings-based logic. This enforces read-only during `PLAN_EXPLORING`.

### 5. Plan injected as a user message during execution

Rather than modifying `SystemPromptBuilder`, we inject the approved plan as a
**user message** into the conversation history via `ctx.append()`. The
`_PLAN_EXECUTING_INSTRUCTIONS` system prompt already says "Follow the approved
plan steps in order" — the plan content lives in the conversation where the
agent can reference it naturally. This is simpler and avoids changing the
prompt builder interface.

### 6. Step tracking is agent-self-reported (not auto-parsed)

The agent in `PLAN_EXECUTING` mode self-reports progress via text. The
coordinator does not attempt to parse step completions from tool calls — this is
simpler and more robust for the initial implementation. The plan's
`format_for_prompt()` shows step status icons, and the agent can report "Step 2
complete" naturally.

---

## Implementation Steps

### Step 1: Fix the `PlanProposed` event type reference

**File**: `toddler/agent/events.py`

The `Plan` type on line 71 is referenced but never imported (the `# noqa: F821`
hides this). Add a `TYPE_CHECKING` import of `Plan` from
`toddler.agent.state_machine`.

### Step 2: Wire `should_auto_approve_tool()` into `AgentLoop`

**File**: `toddler/agent/loop.py`

- Accept an optional `state_machine: AgentStateMachine | None` parameter in
  `AgentLoop.__init__()`.
- In `_needs_confirmation()`, consult `state_machine.should_auto_approve_tool()`
  before falling back to the existing settings-based logic.
- Import `AgentStateMachine` (under `TYPE_CHECKING`).

This enforces the read-only guarantee during `PLAN_EXPLORING` — currently the
agent is only *instructed* not to mutate, not prevented.

### Step 3: Add non-streaming plan proposal helper to `SessionCoordinator`

**File**: `toddler/session/coordinator.py`

Add a private method `_generate_plan(user_request)` that:

1. Transitions the state machine to `PLAN_PROPOSING`.
2. Sends `plan_proposal_prompt(user_request)` as a user message via the LLM's
   non-streaming `generate()` method (no tools, `stream=False`).
3. Parses the JSON response into a `Plan` object via `Plan.from_json()`.
4. Calls `sm.set_plan(plan)` and transitions to `PLAN_WAITING`.
5. Returns the `Plan` (or raises on parse failure).

Error handling: if JSON parsing fails, retry once with a follow-up "Please
return ONLY valid JSON" prompt. If that also fails, yield `AgentError` and
finish.

### Step 4: Add multi-phase orchestration to `process_turn()`

**File**: `toddler/session/coordinator.py`

First, extract the current agent-run + persist logic into a reusable helper
`_run_phase(user_input, mode_hint)` to avoid duplication across explore and
execute phases. Refactor the existing `process_turn()` to use it for
`EXECUTING` mode — this verifies no behavior change for simple (non-plan) turns.

Then rewrite `process_turn()` as a state-machine-driven while-loop:

```
reset sm → classify → while sm.mode != FINISHED:
  if EXECUTING:            _run_phase(input, "execute") → mark finished
  elif PLAN_EXPLORING:     _run_phase(input, "plan_exploring") →
                           transition to PLAN_PROPOSING
  elif PLAN_PROPOSING:     _generate_plan() → set_plan() → transition to
                           PLAN_WAITING
  elif PLAN_WAITING:       yield PlanProposed, await approval event
                           approve → inject plan msg into ctx, loop to EXECUTING
                           feedback → inject feedback msg into ctx, loop to EXPLORING
                           reject → mark finished
  elif PLAN_EXECUTING:     _run_phase(input, "plan_executing") → mark finished
```

New public methods on `SessionCoordinator`:
- `approve_plan()` — calls `sm.approve_plan()`, signals the wait event
- `reject_plan(feedback="")` — calls `sm.reject_plan(feedback=feedback)`,
  signals the wait event

New private helper:
- `_run_phase(user_input, mode_hint)` — runs one agent loop phase, persists
  context afterward

The asyncio.Event pattern mirrors the existing `AgentPaused`/tool-approval
mechanism in `AgentLoop` — proven and already working.

### Step 5: Add plan approval UI to `CLIApp`

**File**: `toddler/cli/app.py`

Replace the current passive `PlanProposed` handler with one that collects a user
decision, mirroring the `AgentPaused` pattern:

```python
case PlanProposed():
    self._renderer.pause()
    self._renderer.on_plan_proposed(event)
    result = await self._renderer.confirm(
        prompt="Approve this plan?",
        choices=["approve", "reject"],
        allow_feedback=True,
    )
    if result.decision == "approve":
        await self._coordinator.approve_plan()
    elif result.decision == "feedback":
        await self._coordinator.reject_plan(
            feedback=result.feedback or ""
        )
    else:
        await self._coordinator.reject_plan()
    self._renderer.resume()
```

### Step 6: Enhance plan display in `Renderer`

**File**: `toddler/cli/renderer.py`

Replace the minimal `on_plan_proposed()` implementation with one that renders
the full plan using `Plan.format_for_display()` — showing title, summary, steps
(with status icons), dependencies, rationale, risks, and estimated files
touched. Render using Rich markup:

- Steps as a numbered table with status, description, files, and dependencies
- Risks as a bullet list with warning styling
- Rationale as a dimmed blockquote

### Step 7: Inject plan as a user message during execution

**File**: `toddler/session/coordinator.py`

When the user approves a plan in the `PLAN_WAITING` phase, inject the plan
content into the conversation as a user message via `ctx.append()`:

```python
plan_text = self._sm.current_plan.format_for_prompt()
plan_msg = (
    f"I have reviewed and approved the following plan. "
    f"Execute it step by step, reporting progress after "
    f"each step:\n\n{plan_text}"
)
self._ctx.append(Message.user(plan_msg))
current_input = "Begin executing the approved plan now."
```

The `_PLAN_EXECUTING_INSTRUCTIONS` system prompt already says "Follow the
approved plan steps in order" — the plan details live in conversation history
where the agent can reference them. No changes to `SystemPromptBuilder` needed.

### Step 8: Add tests

**File**: `tests/test_plan_mode.py` (new)

Test cases:
- `classify_complexity()` — keywords, word count, multi-file indicators, simple
  inputs
- `AgentStateMachine` transitions — valid transitions, invalid transitions,
  approve/reject flow
- `Plan.from_json()` / `Plan.to_json()` — round-trip serialization
- `plan_proposal_prompt()` — output format includes expected fields
- `should_auto_approve_tool()` — blocks WRITE in PLAN_EXPLORING, allows READ
- `SessionCoordinator` plan workflow — mock LLM returns a plan JSON, verify
  `PlanProposed` is yielded, approve flows to `PLAN_EXECUTING`
- `CLIApp` plan approval — verify confirm() is called with correct choices

---

## Files Changed

| File | Change |
|------|--------|
| `toddler/agent/events.py` | Add `TYPE_CHECKING` import of `Plan` |
| `toddler/agent/loop.py` | Accept `state_machine`, consult in `_needs_confirmation()` |
| `toddler/session/coordinator.py` | Multi-phase orchestration, `_run_phase()`, `_generate_plan()`, approve/reject API |
| `toddler/cli/app.py` | Plan approval/rejection interaction in `PlanProposed` handler |
| `toddler/cli/renderer.py` | Enhanced `on_plan_proposed()` with full plan display |
| `tests/test_plan_mode.py` | New test file covering all plan mode components |

## Verification

1. **Unit tests**: `pytest tests/test_plan_mode.py -v`
2. **Manual test — auto-trigger**: Run
   `tod "refactor the CLI app to use a plugin system"` and verify it enters plan
   mode, explores, proposes a plan, and waits for approval.
3. **Manual test — `/plan` command**: In REPL, type `/plan` then a complex
   request, verify the same flow.
4. **Manual test — approval/rejection**: Approve a plan and verify it executes;
   reject with feedback and verify it re-explores; reject outright and verify it
   finishes.
5. **Manual test — read-only enforcement**: During exploration, verify that
   write/edit/shell-dangerous tools require confirmation even when
   `auto_approve_read` is enabled.
6. **Manual test — `--plan` flag**: `tod --plan "add logging"` in one-shot mode
   verifies the full flow.
7. **Regression**: `pytest tests/ -v` to verify existing agent loop and stop
   condition tests still pass.
