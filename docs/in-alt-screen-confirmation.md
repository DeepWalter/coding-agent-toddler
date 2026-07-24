# In-Alt-Screen Confirmation

## Problem

During streaming, when the agent needs user confirmation (tool call approval, plan
approval), `CLIApp` calls `renderer.pause()`, which stops Rich's `Live` and exits
the alternate screen buffer. The confirmation prompt then appears in the main
console. After the user responds, `renderer.resume()` re-enters the alternate
screen. This context switch is jarring — the user's gaze jumps from the
streaming output panel to a plain console prompt and back.

Meanwhile, `StreamingRenderer.stop()` already handles the **dismiss prompt**
entirely within the alternate screen via raw termios I/O, proving the pattern is
viable. The goal is to extend this same approach to tool and plan confirmation
so that ALL user interaction during streaming stays in the alternate screen.

## Current state

| Interaction       | Input                         | Screen               | Mechanism                              |
|-------------------|-------------------------------|----------------------|----------------------------------------|
| REPL prompt       | prompt_toolkit `PromptSession` | Main console         | `InputHandler.prompt()`               |
| Tool confirmation | prompt_toolkit `PromptSession` | Main console         | `pause()` → `_confirm()` → `resume()` |
| Plan proposed     | *Never reached (dead code)*   | Main console         | `pause()` → `on_plan_proposed()` → `resume()` |
| Dismiss prompt    | Raw termios (Enter/arrows)    | **Alternate screen** | `_wait_for_dismiss()`                 |

## Goals

1. **Unify confirmation paths** — tool confirmation and plan confirmation use
   the same code path.

2. **Stay in the alternate screen** — during streaming, confirmation prompts
   are rendered inside the Live display. No pause/resume cycle.

3. **Interactive row selection** — a Rich Table with Tab/Shift+Tab to navigate
   rows, Enter to confirm. Visual highlight on the focused row.

4. **Three plan responses** — approve, deny (plain reject), and reject with
   feedback (free-text input).

5. **Graceful fallback** — non-streaming mode is unchanged. Single-key
   shortcuts (`a`/`d`/`f`) still work as a fast path for keyboard users.

## Design

### Interactive confirmation table

Instead of a plain text prompt, `_build_renderable()` appends a Rich `Table`
when `_confirming` is set. The table has one row per choice, with the
selected row visually highlighted.

Tool confirmation looks like:

```
┌─────────────────────────────────────────┐
│ Confirm                                 │
│                                         │
│  ▸ Approve                              │  ← highlighted row
│    Deny                                 │
│                                         │
│  Tab to move · Enter to select          │
└─────────────────────────────────────────┘
```

Plan confirmation adds a third row:

```
┌─────────────────────────────────────────┐
│ Confirm Plan                            │
│                                         │
│  ▸ Approve                              │
│    Deny                                 │
│    Feedback                             │
│                                         │
│  Tab to move · Enter to select          │
└─────────────────────────────────────────┘
```

#### Row highlighting strategy

Each row is built as a `Text` object. The focused row gets `reverse` style
(swapped foreground/background). The cursor indicator `▸` is prepended to the
focused row and hidden on others.

```python
def _build_confirm_row(label: str, focused: bool) -> Text:
    cursor = "▸ " if focused else "  "
    style = "reverse" if focused else ""
    return Text(cursor + label, style=style)
```

Alternatively, use `Panel` with a colored border on the selected row, or lean on
Rich's `Table` with per-row styles — we'll iterate on the visual during
implementation.

### Input handling

The read loop extends `_wait_for_dismiss_scrollable()` (renderer.py:795-817).
Non-canonical mode, no echo — every keystroke arrives as raw bytes.

**Two sub-modes within the loop:**

1. **Navigation mode** — Tab/arrows move the highlight. Enter confirms the
   selected row. Navigating to the Feedback row and pressing any printable
   key transitions to input mode.

2. **Input mode** (Feedback row only) — printable characters accumulate into
   a text buffer. Backspace deletes. Escape clears the buffer and returns to
   navigation mode. **Tab/arrows always navigate** — the typed text is saved
   to a buffer and restored when the Feedback row regains focus. Enter submits
   the feedback.

| Key            | Bytes              | Action                                                   |
|----------------|--------------------|----------------------------------------------------------|
| Tab            | `b"\t"`            | Move to next row (wrap). Saves/restores feedback buffer. |
| Shift+Tab      | `b"\x1b[Z"`        | Move to previous row (wrap). Saves/restores feedback buffer. |
| Up             | `b"\x1b[A"`        | Move to previous row (clamp)                              |
| Down           | `b"\x1b[B"`        | Move to next row (clamp)                                  |
| Enter          | `b"\r"` / `b"\n"`  | Confirm selected row / submit feedback text               |
| Backspace      | `b"\x7f"`          | Delete last character (input mode only)                   |
| Escape         | `b"\x1b"` (lone)   | If input mode: clear buffer, return to nav mode. Else: no-op. |
| Ctrl+C         | `b"\x03"`          | Cancel → deny                                            |
| Printable      | `0x20`–`0x7e`      | If Feedback row selected → enter input mode, append char. Otherwise: no-op. |

#### Escape vs arrow-key disambiguation

When `\x1b` arrives, we must distinguish a lone Escape from an escape sequence
(arrow keys, Shift+Tab). Same technique as `_wait_for_dismiss_scrollable`:

```python
if ch == b"\x1b":
    # Peek: is more data available immediately?
    r2, _, _ = select.select([sys.stdin], [], [], 0.01)
    if r2:
        seq = os.read(fd, 2)   # read the rest of the CSI sequence
        # handle arrow keys / Shift+Tab
    else:
        # Lone Escape — cancel input mode
```

#### Input loop structure

```python
def _confirm_read_loop(self, fd: int, choices: list[str]) -> ConfirmResult:
    buf: str = ""               # feedback text buffer
    input_mode: bool = False    # True when actively typing feedback
    saved_buf: str = ""         # buffer persisted across Tab navigation

    def _navigate(self, new_index: int) -> None:
        """Move selection, saving/restoring feedback buffer as needed."""
        prev = self._confirm_selection
        # Save buffer when leaving Feedback row
        if prev == feedback_index and buf:
            saved_buf = buf
        elif prev != feedback_index:
            saved_buf = ""
        self._confirm_selection = new_index
        # Restore buffer when arriving at Feedback row
        if new_index == feedback_index:
            buf = saved_buf
            input_mode = bool(buf)
        else:
            buf = ""
            input_mode = False

    while True:
        self._refresh(force=True)
        r, _, _ = select.select([sys.stdin], [], [], 0.1)
        if not r:
            continue

        ch = os.read(fd, 1)

        # ── Escape sequences ──────────────────────────
        if ch == b"\x1b":
            r2, _, _ = select.select([sys.stdin], [], [], 0.01)
            if r2:
                seq = os.read(fd, 2)
                if seq == b"[A":           # Up
                    _navigate(max(0, self._confirm_selection - 1))
                elif seq == b"[B":         # Down
                    _navigate(min(len(choices) - 1, self._confirm_selection + 1))
                elif seq == b"[Z":         # Shift+Tab
                    _navigate((self._confirm_selection - 1) % len(choices))
            else:
                # Lone Escape — clear buffer, stay on Feedback
                if input_mode:
                    input_mode = False
                    buf = ""
                    saved_buf = ""
            continue

        # ── Tab ───────────────────────────────────────
        if ch == b"\t":
            _navigate((self._confirm_selection + 1) % len(choices))
            continue

        # ── Enter ─────────────────────────────────────
        if ch in (b"\r", b"\n"):
            break

        # ── Backspace ─────────────────────────────────
        if ch == b"\x7f":
            if input_mode and buf:
                buf = buf[:-1]
                saved_buf = buf
            continue

        # ── Ctrl+C ────────────────────────────────────
        if ch == b"\x03":
            self._confirm_selection = deny_index
            buf = ""
            break

        # ── Printable characters ──────────────────────
        if 0x20 <= ord(ch) <= 0x7e:
            if self._confirm_selection == feedback_index:
                if not input_mode:
                    input_mode = True
                ch_str = ch.decode("ascii", errors="replace")
                buf += ch_str
                saved_buf = buf
```

### In-place feedback rendering

When the user is on the Feedback row, `_build_renderable()` renders it as an
inline text field instead of a plain label:

**Navigation mode, Feedback row selected but no input started:**

```
  ▸ Feedback: ▏
```

**Input mode, user has typed some text:**

```
  ▸ Feedback: use async instead of threads▏
```

The cursor `▏` (U+258F) is appended to the text buffer during rendering. It's
not part of the buffer itself — just a visual indicator.

The row-building logic:

```python
def _build_confirm_row(
    label: str, index: int, selected: int, buf: str, input_mode: bool,
) -> Text:
    focused = (index == selected)
    style = "reverse" if focused else ""

    if label.lower() == "feedback":
        if focused and input_mode:
            # Focused + actively typing: show cursor
            return Text("▸ Feedback: " + buf + "▏", style=style)
        elif buf:
            # Has saved text but not focused: show buffer without cursor
            return Text("  Feedback: " + buf, style=style)
        elif focused:
            # Focused, no text yet: show prompt cursor
            return Text("▸ Feedback: ▏", style=style)
        else:
            # Not focused, no text: plain label
            return Text("  Feedback: ▏", style=style)
    else:
        cursor = "▸ " if focused else "  "
        return Text(cursor + label, style=style)
```

### Full visual flow

```
Step 1: Navigate to Feedback, start typing
┌──────────────────────────────────────┐
│ Confirm Plan                         │
│                                      │
│    Approve                           │
│    Deny                              │
│  ▸ Feedback: use async▏              │  ← focused, actively typing
│                                      │
│  Enter to submit · Esc to cancel     │
└──────────────────────────────────────┘

Step 2: Press Tab — buffer stays visible, highlight moves
┌──────────────────────────────────────┐
│ Confirm Plan                         │
│                                      │
│  ▸ Approve                           │  ← now focused
│    Deny                              │
│    Feedback: use async               │  ← buffer visible but not focused
│                                      │
│  Tab to move · Enter to select       │
└──────────────────────────────────────┘

Step 3: Press Shift+Tab — back to Feedback, cursor restored
┌──────────────────────────────────────┐
│ Confirm Plan                         │
│                                      │
│    Approve                           │
│    Deny                              │
│  ▸ Feedback: use async▏              │  ← focused again, ready to continue
│                                      │
│  Enter to submit · Esc to cancel     │
└──────────────────────────────────────┘

Step 4: Type more, then Enter → submitted
Returns ConfirmResult(decision="feedback", feedback="use async instead")
```

Escape clears the buffer back to `▸ Feedback: ▏`.

### Renderer changes (`renderer.py`)

New type:

```python
@dataclass
class ConfirmResult:
    decision: Literal["approve", "deny", "feedback"]
    feedback: str | None = None
```

New abstract method on `Renderer`:

```python
@abstractmethod
async def confirm(
    self,
    prompt: str,
    choices: list[str],
    *,
    allow_feedback: bool = False,
) -> ConfirmResult:
    ...
```

New per-turn state on `StreamingRenderer`:

```python
# Confirmation state (set when confirm() is active)
self._confirming: bool = False
self._confirm_prompt: str = ""
self._confirm_choices: list[str] = []
self._confirm_selection: int = 0
self._confirm_allow_feedback: bool = False
```

**`StreamingRenderer.confirm()`**:

1. Store prompt + choices + selection into instance state.
2. Set `_confirming = True`.
3. Set up raw termios (disable ECHO, disable ICANON).
4. Run the input loop described above.
5. If feedback was requested, switch to line-input mode and collect text.
6. Restore termios in `finally`.
7. Clear `_confirming`, refresh one last time.
8. Return `ConfirmResult`.

**`NonStreamingRenderer.confirm()`**:

Keeps using `InputHandler` (prompt_toolkit). No alternate screen to stay in, so
the full prompt_toolkit experience (history, line editing) makes sense. The
choices are rendered as a text prompt with key hints.

#### `_build_renderable()` addition

When `_confirming` is True, append a confirmation table below the tools panel
and above any dismiss prompt:

```python
if self._confirming:
    confirm_table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    confirm_table.add_column(width=30)
    for i, choice in enumerate(self._confirm_choices):
        label = choice.capitalize()
        if i == self._confirm_selection:
            confirm_table.add_row(Text("▸ " + label, style="reverse"))
        else:
            confirm_table.add_row(Text("  " + label))
    elements.append(Panel(
        Group(confirm_table, Text("Tab to move · Enter to select", style="dim italic")),
        title=self._confirm_prompt,
        title_align="left",
        border_style="yellow",
    ))
```

### CLI changes (`app.py`)

The `AgentPaused` handler changes from:

```python
# Before
case AgentPaused():
    self._renderer.pause()
    self._renderer.on_agent_paused(event)
    approved = await self._confirm(event)
    if approved:
        await self._coordinator.agent.approve_tool_call()
    else:
        await self._coordinator.agent.deny_tool_call()
    self._renderer.resume()
```

To:

```python
# After
case AgentPaused():
    result = await self._renderer.confirm(
        prompt=event.prompt,
        choices=event.choices or ["approve", "deny"],
    )
    if result.decision == "approve":
        await self._coordinator.agent.approve_tool_call()
    else:
        await self._coordinator.agent.deny_tool_call()
```

The `_confirm()` method on `CLIApp` is removed. Confirmation is fully the
renderer's responsibility.

### Plan confirmation (future)

When the plan orchestration loop is implemented, `PlanProposed` uses the same
`confirm()` with `allow_feedback=True`:

```python
case PlanProposed():
    self._renderer.on_plan_proposed(event)
    result = await self._renderer.confirm(
        prompt="Approve this plan?",
        choices=["approve", "deny", "feedback"],
        allow_feedback=True,
    )
    match result.decision:
        case "approve":
            await self._coordinator.agent.approve_plan()
        case "feedback":
            await self._coordinator.agent.reject_plan(feedback=result.feedback)
        case "deny":
            await self._coordinator.agent.reject_plan()
```

## Raw termios details

Two modes, switched on the fly:

| Mode          | ICANON | ECHO | When                           |
|---------------|--------|------|--------------------------------|
| Non-canonical | off    | off  | Table navigation (Tab, arrows) |
| Canonical     | on     | on   | Feedback text entry            |

`_wait_for_dismiss()` already handles the non-canonical setup (renderer.py:779-793).
The feedback entry temporarily restores canonical + echo, then switches back.

## Comparison with alternatives

| Approach                       | Pros                                                       | Cons                                                   |
|--------------------------------|------------------------------------------------------------|--------------------------------------------------------|
| **Rich Table + raw termios**   | No new deps, consistent with existing patterns, full visual control | Manual key handling, no mouse support        |
| prompt_toolkit `Application`   | Feature-rich (mouse, clipboard, compositing)              | Wants to own the terminal — conflicts with Rich Live |
| textual                        | Full TUI framework, mature                                | Heavy dependency (~1MB), rewrites all rendering   |

The Rich Table approach is the right fit: minimal, consistent with the existing
codebase, and fully adequate for a 2–3 row selection menu.

## Files touched

| File                          | Change                                                                                                                                      |
|-------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------|
| `toddler/cli/renderer.py`     | Add `ConfirmResult`, `confirm()` methods, `_build_confirm_table()`, confirmation read loop, update `_build_renderable()`                    |
| `toddler/cli/app.py`          | Replace `pause()`/`_confirm()`/`resume()` with `renderer.confirm()`, remove `_confirm()`                                                    |
| `toddler/cli/input_handler.py`| No changes (still used by REPL and non-streaming confirm)                                                                                   |

## Non-goals

- The plan orchestration loop (coordinator yielding `PlanProposed`) is out of
  scope.
- Mouse support is out of scope — keyboard navigation covers the use case.
- Multi-line feedback input is out of scope — one line is sufficient for
  "Don't use asyncio, use threads instead."
