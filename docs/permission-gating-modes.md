# Plan: Permission Gating Modes for Toddler

## Context

The toddler CLI agent currently has hardcoded permission gating: READ and
SHELL_SAFE auto-approve; WRITE and SHELL_DANGEROUS always require confirmation.
The `/mode` command toggles between `plan` and `execute` workflow modes.  The
plan confirmation UI offers a single "approve" choice that proceeds with default
gating.

We're adding configurable permission gating with two modes — **manual**
(default, same as current behavior) and **auto** (WRITE auto-approved, only
SHELL_DANGEROUS confirms).  The `/mode execute` subcommand is removed and
replaced with `/mode manual` and `/mode auto`.  Plan approval is split into
"approve with manual accept" and "approve with auto accept" so the user can
choose their gating level when approving a plan.

## Changes

### 1. `toddler/tools/base.py` — new `PermissionMode` enum + shared `needs_confirmation()`

Add after the `Permission` enum:

```python
class PermissionMode(Enum):
    MANUAL = "manual"   # confirm WRITE + SHELL_DANGEROUS (default)
    AUTO = "auto"       # confirm only SHELL_DANGEROUS; WRITE auto-approved


def needs_confirmation(
    perm: Permission,
    mode: PermissionMode = PermissionMode.MANUAL,
) -> bool:
    """Return ``True`` if *perm* requires user confirmation under *mode*.

    This is the **single source of truth** for the permission gating
    policy.  Both ``AgentLoop`` and ``ToolExecutor`` delegate to this
    function — no mirrored logic to keep in sync.
    """
    if perm in (Permission.READ, Permission.SHELL_SAFE):
        return False
    if perm is Permission.WRITE:
        return mode is not PermissionMode.AUTO
    # SHELL_DANGEROUS and unknown — always confirm
    return True
```

### 2. `toddler/agent/state_machine.py` — persistent mode, plan-entry default, remove `_force_direct`

- Import `PermissionMode` from `toddler.tools.base`.
- `__init__`: add `initial_permission_mode: PermissionMode = PermissionMode.MANUAL` kwarg; store `self._permission_mode`.  Remove `self._force_direct`.
- Add `permission_mode` property and `set_permission_mode()` method.
- `classify_and_transition()`: delete the `_force_direct` short-circuit block. After computing `target`, add: if entering `PLAN_EXPLORING`, reset `_permission_mode = PermissionMode.MANUAL`.  This ensures every plan cycle starts from the safe baseline so the approval-time choice is meaningful.
- `reset()`: do NOT clear `_permission_mode` — it persists across turns. Add a comment.
- Delete `flag_direct_execute()`, `force_direct` property. In `flag_plan_pending()`, remove `self._force_direct = False`.

### 3. `toddler/tools/executor.py` — use shared `needs_confirmation`

- Import `PermissionMode` and `needs_confirmation` from `toddler.tools.base`.
- `__init__`: add `permission_mode: PermissionMode = PermissionMode.MANUAL` kwarg; store `self._permission_mode`.
- Add `set_permission_mode(mode)` setter.
- `_check_permission()`: delegate to `needs_confirmation(perm, self._permission_mode)`. If confirmation is needed, fall through to `_confirm()`; otherwise return `True`.
- Remove the inline hardcoded `Permission.READ`/`SHELL_SAFE`/`WRITE`/`SHELL_DANGEROUS` branches — the shared function owns that policy.

### 4. `toddler/agent/loop.py` — use shared `needs_confirmation`

- Import `PermissionMode` and `needs_confirmation` from `toddler.tools.base`.
- `__init__`: add `state_machine: AgentStateMachine | None = None` kwarg; store `self._sm`.
- `_needs_confirmation()`: replace the inline logic with `return needs_confirmation(perm, self._permission_mode)`.
- Add `_permission_mode` property: reads live from `self._sm.permission_mode`, falls back to MANUAL when no state machine (test compatibility).

### 5. `toddler/session/coordinator.py` — sync point, approval mode, display

- Import `PermissionMode`.
- `__init__`: create state machine before executor; pass `permission_mode=self._sm.permission_mode` to `ToolExecutor(...)`.
- Add `set_permission_mode(mode)`: updates both `self._sm` and `self._executor`.
- `approve_plan()`: add `permission_mode: PermissionMode | None = None` kwarg. When set, calls `self.set_permission_mode(permission_mode)` before delegating to planner.
- `mode_label`: drop the `force_direct` branch.
- Add `permission_label` property: returns `"MANUAL"` or `"AUTO"`.
- `agent` property: pass `state_machine=self._sm` to `AgentLoop(...)`.

### 6. `toddler/cli/commands.py` — `/mode plan|manual|auto`

- Import `PermissionMode`.
- Rewrite `_cmd_mode()`:
  - No args: show workflow mode AND permission gating mode.
  - `plan`/`p`: unchanged (sets plan pending).
  - `manual`/`m`: set permission mode to MANUAL via coordinator.
  - `auto`/`a`: set permission mode to AUTO via coordinator.
  - Unknown: updated error message listing `plan, manual, auto`.
- Add `_set_permission_mode(mode)` helper: goes through coordinator if available, falls back to state machine.
- Update `HELP_TEXT`: `/mode [plan / manual / auto]`.

### 7. `toddler/cli/renderer.py` — new ConfirmResult decisions

- `ConfirmResult.decision` type: add `"approve_with_manual"` and `"approve_with_auto"`.
- `StreamingRenderer.confirm()` result mapping: add explicit branches for the new choice strings before the generic approve check.
- `NonStreamingRenderer.confirm()`: same — add branches for the new choices.  Note: single-letter `a` picks the first match ("approve with manual accept").
- `prompt_header()`: add optional `permission_label` kwarg rendered after the mode in dim style.

### 8. `toddler/cli/app.py` — split plan approval

- Import `PermissionMode`.
- `PlanProposed` handler: change choices to `["approve with manual accept", "approve with auto accept", "deny", "feedback"]`.  Match `"approve_with_manual"` → `coordinator.approve_plan(permission_mode=PermissionMode.MANUAL)`; `"approve_with_auto"` → `coordinator.approve_plan(permission_mode=PermissionMode.AUTO)`.
- `run_repl` header: pass `permission_label=self._coordinator.permission_label` to `prompt_header`.

### 9. `toddler/cli/input_handler.py` — autocomplete

- `_SLASH_COMMANDS["/mode"]`: update description to `"/mode [plan|manual|auto]"`.
- `_SUB_OPTIONS["/mode"]`: replace `("execute", ...)` with `("manual", ...)` and `("auto", ...)` entries.

### 10. Tests

- `tests/test_agent_loop.py`: Add `DangerousTool` mock. Add tests:
  - WRITE auto-approves in AUTO mode (no `AgentPaused`)
  - SHELL_DANGEROUS still pauses in AUTO mode
  - WRITE pauses in MANUAL mode
  - Loop without state machine defaults to MANUAL
- `tests/test_plan_mode.py`: Add `TestPermissionMode` class:
  - Default is MANUAL; `set_permission_mode` changes it
  - `reset()` preserves the mode
  - Plan entry resets to MANUAL
  - Simple (EXECUTING) path does NOT reset an explicitly-set AUTO
  - Coordinator: `approve_plan(permission_mode=AUTO)` sets the mode on state machine
- Run full suite: `.venv/bin/python -m pytest tests/ -x`

## Key Design Decisions

- **Permission mode stored on `AgentStateMachine`** — persists across turns (not per-turn like `_plan_pending`), survives `reset()`, and the AgentLoop reads it live via a reference (no loop rebuild on mode change).
- **Plan entry always resets to MANUAL** — the approval UI then lets the user explicitly choose AUTO.  The next plan cycle resets again.
- **`_force_direct` deleted** — dead code after `/mode execute` removal.  Users skip plan mode by typing simple requests (complexity heuristic).
- **Shared `needs_confirmation()` in `tools/base.py`** — eliminates the mirrored logic between `AgentLoop` and `ToolExecutor`. Both sites delegate to this single function, so the policy is defined in one place.

## Verification

1. Run the REPL: `.venv/bin/python -m toddler` — verify `/mode` shows PLAN + MANUAL
2. `/mode auto` → verify header shows AUTO
3. `/mode manual` → verify header shows MANUAL
4. `/plan` → type a complex request → verify plan approval shows two approve choices
5. Select "approve with auto accept" → verify execution proceeds and subsequent turns show AUTO
6. Run full test suite: `.venv/bin/python -m pytest tests/ -v`
