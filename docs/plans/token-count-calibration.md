# Token Count Calibration: API-Aware Context Window

## Motivation

`ContextWindowManager` currently estimates token usage purely via `tiktoken`
before every LLM call.  The API response includes **actual** `prompt_tokens` /
`completion_tokens` counts — the billing-grade ground truth — but we discard
them after feeding them to `StopConditionChecker` for budget enforcement.

The two sources can diverge (different tokenizer, tool-definition overhead,
framing bytes), and `tiktoken` tends to *underestimate* for non-OpenAI
providers like DeepSeek.  Because compaction/truncation decisions are made on
the estimate, underestimation means we run closer to the real context limit
than intended — risking `context_length_exceeded` errors.

## Key insight

We only need to **estimate the delta** — the messages added since the last API
call.  Everything older was already counted by a previous API response.

The API gives two exact numbers per call:

| Field | Covers | Source |
| --- | --- | --- |
| `usage.input_tokens` | All messages sent *to* the API | **Exact** |
| `usage.output_tokens` | The assistant's response | **Exact** |

Together, `usage.total` (input + output) is the exact token count for the
entire message list **after** the assistant response is appended.
The **only** content that ever needs tiktoken estimation is what's added
between API calls:

1. **New user input** — just typed, never sent to an API
2. **New tool results** — just executed, never sent to an API

```
After API call:  [sys, user1, assistant1]  ← usage.total = 600 (exact)
                                                         │
Next user turn:  [sys, user1, assistant1, user2]         │
                 |----- 600 exact ------|  |-- est ----|  ← only user2 needs tiktoken

With tool calls: [sys, user1, assistant1, tool_result, tool_result, user2]
                 |----- 600 exact ------|  |-------- est ----------|
```

`ContextManager` already tracks `_baseline_count` (message index) for
persistence.  We add a parallel `_baseline_total_tokens` for token tracking,
using the same acknowledge/reset pattern.

## Design

### Data flow

```
AgentLoop._call_llm()
    → provider.generate(messages)
        → API returns usage.input_tokens + usage.output_tokens
    → llm_result["usage"] = TokenUsage(...)

AgentLoop.run()
    → stop_checker.add_tokens(usage)         # existing — budget enforcement
    → self._ctx.append(assistant_msg)        # existing — save to history
    → self._ctx.record_usage(usage)          # NEW — after append, so baseline
                                             #   covers assistant response too
        → ContextManager.record_usage()
            → stores _baseline_total_tokens = usage.total
            → stores _baseline_message_count = len(messages)  # incl. assistant

ContextWindowManager.count_tokens()          # MODIFIED
    baseline = _baseline_total_tokens        # from last API call (0 if first)
    new_msgs = messages[_baseline_message_count:]   # user input + tool results
    delta   = tiktoken_estimate(new_msgs)           # only these need estimation
    return baseline + delta
```

### Changes

#### 1. `ContextWindowManager` — track baseline + estimate delta

`ContextWindowManager` now owns a `TokenCounter` directly instead of going
through ``self._llm.count_tokens()`` (see §1a).  Two new methods and a
modified `count_tokens()`:

```python
class ContextWindowManager:
    def __init__(self, llm_provider: BaseLLMProvider, *, ...):
        ...
        self._token_counter = TokenCounter(model=llm_provider.model)
        # New fields
        self._baseline_total_tokens: int = 0
        self._baseline_message_count: int = 0

    def set_baseline(self, *, total_tokens: int, message_count: int) -> None:
        """Set the authoritative baseline after an API call.

        *total_tokens* is ``usage.total`` from the response (input + output).
        *message_count* is ``len(messages)`` AFTER the assistant is appended.
        """
        self._baseline_total_tokens = total_tokens
        self._baseline_message_count = message_count

    def reset_baseline(self) -> None:
        """Invalidate the baseline.

        Called when the message list changes structurally — new conversation
        (``load``), compaction, or truncation.  Subsequent ``count_tokens``
        calls will estimate everything with tiktoken until the next API
        response provides a fresh baseline.
        """
        self._baseline_total_tokens = 0
        self._baseline_message_count = 0

    # Existing method — MODIFIED:
    def count_tokens(self, messages: list[Message]) -> int:
        """Count tokens: actual baseline + estimated delta."""
        if self._baseline_total_tokens == 0:
            # No baseline yet — estimate everything
            return self._token_counter.count_messages(messages)

        already_counted = self._baseline_message_count
        if len(messages) <= already_counted:
            # Shouldn't happen (reset_baseline is always called on structural
            # changes), but guard defensively.
            return self._token_counter.count_messages(messages)

        new_msgs = messages[already_counted:]   # only user input + tool results
        delta = self._token_counter.count_messages(new_msgs)
        return self._baseline_total_tokens + delta
```

#### 1a. Move `TokenCounter` from `llm/` to `context/`

`count_tokens()` on the provider is only called by `ContextWindowManager`.
It's a one-line delegate with no other consumers — token counting is a
context-management concern, not an LLM provider concern.

| File | Change |
| --- | --- |
| `toddler/llm/token_counter.py` | **Delete** — move to `toddler/context/token_counter.py` |
| `toddler/llm/base.py` | Remove `count_tokens()` abstract method |
| `toddler/llm/provider.py` | Remove `count_tokens()` method and `TokenCounter` import |
| `toddler/llm/__init__.py` | Drop `TokenCounter` from exports |
| `toddler/context/__init__.py` | Add `TokenCounter` to exports |
| `toddler/context/window.py` | Import `TokenCounter` from `toddler.context.token_counter` directly |

After this, ``BaseLLMProvider`` no longer mentions token counting — the
context layer owns it entirely.

#### 2. `ContextManager` — expose `record_usage()`, reset on structural changes

```python
class ContextManager:
    def record_usage(self, usage: TokenUsage) -> None:
        """Feed API-reported token counts back into the window manager.

        Must be called AFTER the assistant message is appended so that
        ``usage.total`` covers every message currently in the buffer.
        """
        if self._window_mgr is not None and usage.total > 0:
            self._window_mgr.set_baseline(
                total_tokens=usage.total,
                message_count=len(self._messages),
            )

    def load(self, messages: list[Message]) -> None:
        """Replace buffer with pre-loaded messages.  Resets the token
        baseline because these messages came from storage, not an API call.
        """
        self._messages = list(messages)
        self._baseline_count = len(messages)
        self._has_compacted = False
        self._last_compaction = None
        if self._window_mgr is not None:
            self._window_mgr.reset_baseline()

    async def _auto_compact(self) -> CompactionResult | None:
        ...
        if <compaction triggered>:
            compacted = await self._compactor.compact(self._messages)
            ...
            self._messages.clear()
            self._messages.extend(compacted)
            self._baseline_count = len(self._messages)
            if self._window_mgr is not None:
                self._window_mgr.reset_baseline()  # ← NEW

        if <truncation triggered>:
            truncated = self._window_mgr.truncate(self._messages)
            self._messages.clear()
            self._messages.extend(truncated)
            self._baseline_count = len(self._messages)
            if self._window_mgr is not None:
                self._window_mgr.reset_baseline()  # ← NEW
```

#### 3. `AgentLoop.run()` — one extra call

```python
# After appending assistant message — currently after line 222
if assistant_msg.content:
    self._ctx.append(assistant_msg)

# NEW: record after append so baseline covers the assistant response too
self._ctx.record_usage(usage)
```

`record_usage()` reads `len(self._messages)` internally — the assistant
message is already appended, so `usage.total` covers the full buffer.

### Edge cases

| Scenario | Handling |
|---|---|
| **First turn (no baseline)** | `baseline == 0` → fall back to full tiktoken estimate |
| **Compaction occurred** | `reset_baseline()` called → next `count_tokens()` estimates the compacted list from scratch |
| **Truncation occurred** | `reset_baseline()` called — same as compaction |
| **New conversation (`load()`)** | `reset_baseline()` called |
| **API call failed (usage=0)** | `set_baseline` skipped (guards on `usage.total > 0`) |
| **Messages shrink below baseline** | Cannot happen — `reset_baseline()` always called on structural changes |

### What we DON'T need

- **No `token_count` field on `Message`** — the data model stays clean
- **No drift-ratio tracking** — the incremental approach makes it unnecessary; only new messages are estimated, so error is bounded to the delta, not the whole conversation

### Accuracy

| Approach | Error scope |
|---|---|
| **Before**: estimate all messages every time | Error scales with conversation length |
| **After**: estimate only new messages | Error scales with single-turn delta (~100–1000 tokens) |

A 10% tiktoken error on a 50k-token conversation → 5k drift before, vs 50–100
tokens after.

## Files touched

| File | Change |
|---|---|
| `toddler/context/window.py` | Own `TokenCounter` directly; add `set_baseline()`, `reset_baseline()`; modify `count_tokens()` |
| `toddler/context/manager.py` | Add `record_usage()` facade, reset baseline in `load()` and `_auto_compact()` |
| `toddler/agent/loop.py` | One line: `self._ctx.record_usage(usage)` after assistant append |
| `toddler/llm/token_counter.py` → `toddler/context/token_counter.py` | **Move** — token counting belongs to context, not LLM |
| `toddler/llm/base.py` | Remove `count_tokens()` abstract method |
| `toddler/llm/provider.py` | Remove `count_tokens()` method and `TokenCounter` import |
| `toddler/llm/__init__.py` | Drop `TokenCounter` from exports |
| `toddler/context/__init__.py` | Add `TokenCounter` to exports |

## Tests

1. **Unit: `count_tokens` with baseline** — verify it only estimates new messages
2. **Unit: `count_tokens` without baseline** — falls back to full tiktoken estimate
3. **Unit: `record_usage` then `reset_baseline`** — baseline clears correctly
4. **Unit: truncation edge case** — when messages shrink below baseline, falls back
5. **Integration: agent loop passes usage to context** — mock LLM, verify `record_usage` is called
