# Parallel Tool Execution

## Motivation

When the model emits several `tool_use` blocks in one assistant message, it is
using the parallel-tool-use pattern — it judged the calls independent and
issued them together.  Toddler currently throws that independence away:
`AgentLoop._execute_tool_calls` runs every call strictly sequentially, awaiting
each to completion before starting the next
([loop.py:358-407](toddler/agent/loop.py#L358-L407)), so an LLM round that
returns five read calls takes the sum of their latencies instead of their max.

Everything downstream of the loop already tolerates concurrent, unordered
completion — only the loop is serial:

- **Tool bodies are genuinely async.**  Shell and git run through
  `asyncio.create_subprocess_*` ([shell.py:306-325](toddler/tools/shell.py#L306-L325),
  [git.py:16-31](toddler/tools/git.py#L16-L31)) — I/O-bound and non-blocking, so
  `asyncio.gather` yields real overlap, not interleaved no-ops.
- **The message model is order-independent.**  Each `tool_result` block carries
  its `tool_id`; the provider fans blocks back out per id on the wire
  ([provider.py:253-264](toddler/llm/provider.py#L253-L264)) and the history is
  one `Message.tool(...)` appended only after every result lands
  ([loop.py:266](toddler/agent/loop.py#L266)).
- **Both UIs key tool state by `tool_id`.**  TUI keeps
  `_tools: dict[str, _ToolRow]` and looks up by id on end
  ([renderer.py:520](toddler/cli/renderer.py#L520),
  [renderer.py:960-964](toddler/cli/renderer.py#L960-L964)); the web reducer
  upserts by `tool_id` (`findTool`, useConsole.ts).  Out-of-order
  `ToolCallEnd` frames just complete cards as results arrive.

So parallel execution is a surgical change to one method — gated by two
constraints that keep it honest (see Key design decisions).

## Key design decisions

**1. Parallel only when nothing in the round needs user confirmation.**
Permission gating is inherently serial: each gated call yields `AgentPaused`
and the CLI blocks the whole event stream on the user's one-at-a-time answer
([app.py:212-220](toddler/cli/app.py#L212-L220)).  Trying to run other calls
while one waits for approval would change the confirm UX (deny semantics,
unanswered prompts mid-flight) for no speedup.  Rounds containing any gated
call keep today's exact sequential path.

**2. Parallel only when every call in the round is non-mutating.**  The LLM's
"these are independent" claim is a belief, not a guarantee.  Two concurrent
`Write` calls can target overlapping files; concurrent git commands contend on
the index.  The tool registry already classifies every call — reuse it: a round
with any `WRITE` / `SHELL_DANGEROUS` permission (the same set as
`ToolExecutor._is_mutating`, [executor.py:205-208](toddler/tools/executor.py#L205-L208))
runs sequentially.  Reads of the same file are safe; only mutations race.

**3. Event order stays deterministic; only wall-clock overlaps.**  All
`ToolCallStart` frames are emitted first (input order), then the gather, then
`ToolCallEnd` + result blocks in input order mapped back by `tool_id`.  The
live UI paints all cards up front and transcripts/history are byte-identical
to a sequential run — concurrency is invisible to every consumer except the
clock.

**4. Cancel keeps today's semantics.**  `asyncio.CancelledError` propagates out
of the gather (default, no `return_exceptions`) exactly as it does out of the
single awaited call today.  The assistant `tool_use` message was already
appended before execution; the session layer's cancel repair
([manager.py:438-452](toddler/context/manager.py#L438-L452)) answers any
dangling calls, so a cancel mid-gather degrades to the same "marked cancelled"
outcome as a cancel mid-tool today.  In-flight results are dropped — same as
now.

## Phase 1 — Execution core (`toddler/agent/loop.py`)

Rework `_execute_tool_calls` (:345-407), the only production change:

1. **First pass — classify every call once** (no duplicate `get_permission`
   lookups): for each call resolve the tool and permission level and record
   `needs_confirm = self._needs_confirmation_for(call)` and
   `mutating = perm in (WRITE, SHELL_DANGEROUS)`.  Unknown tools are
   non-mutating and un-gated (the executor produces an error result for them;
   harmless in either mode).
2. **Decision**: parallel iff no call needs confirmation **and** no call is
   mutating; otherwise the existing sequential loop body, byte-for-byte.
3. **Parallel path**:
   - Emit `ToolCallStart(tool_id, tool_name, partial_input=parameters)` for
     every call, input order (mirrors the execution-phase start today; both
     UIs dedupe by `tool_id` against the stream-phase start).
   - `results = await asyncio.gather(*(self._execute_with_gating(c) for c in calls))`
     — gating inside is inert here (`needs_confirmation` was False for all, so
     `_execute_with_gating` short-circuits to `self._executor.execute`,
     :437-438).
   - Reorder results back to input order by `tool_id`, then per result emit
     `ToolCallEnd` and append its `tool_result_block` — the tail of the
     current loop body reused verbatim.
4. Update the docstring: parallel execution is safe only when the round is
   un-gated and non-mutating.

No changes to `loop.run`, the message model, the provider, storage, or either
UI.  No concurrency cap: model rounds are small (typically 2-8 calls); add an
`asyncio.Semaphore` only if a real workload shows a need.

## Phase 2 — Tests

New `tests/test_parallel_tool_execution.py`, mirroring the `AgentLoop` fixture
pattern in [tests/test_agent_loop.py](tests/test_agent_loop.py) (local
`MockLLMProvider` + a tool registry with async tool fakes):

- **Deterministic overlap proof** — no sleeps.  Two gate-free read tools whose
  `execute` each set a shared `entered` event then await a shared `go` event.
  Run the turn as a task; `await wait_for(entered_events.wait(), 1)`; release
  `go`.  A sequential implementation never enters the second tool before `go`
  fires → the wait times out and the test fails.  The event handshake makes
  the assertion immune to scheduling jitter.
- **Result order**: two tools with unequal latencies complete in either order
  → the persisted `Message.tool` blocks appear in original input order, each
  with the correct `tool_id` and content.
- **Gated round stays sequential**: one `WRITE`-permission call (confirms in
  MANUAL mode) → record the event sequence; no second `ToolCallStart` may
  appear before the `AgentPaused` is answered.
- **Mutating round stays sequential**: mixed round containing one `WRITE` call
  → tools never overlap (same `entered`-event technique proves the second tool
  does not start while the first is blocked).
- **Decision table**: rounds of (all-read, read+write, gated read, unknown
  tool) map to the expected parallel/sequential outcome.
- **Cancel mid-gather**: cancel the turn task while two long-running tools are
  executing → no hang, no orphaned state (reuses the existing cancel-repair
  path; assert the repair tool message is present).

## Out of scope

- Batched multi-call confirmation UX (approve N at once) — gated rounds stay
  sequential until someone wants it.
- Dependency analysis beyond the mutating / non-mutating permission classes.
- True thread/process parallelism — unnecessary while tools are async and
  I/O-bound.
- Concurrency caps, checkpoint-stub interplay (mutating rounds stay
  sequential, so the checkpoint hook is never concurrent).

## Risks

1. **Residual independence risk inside the non-mutating class** — two
   `SHELL_SAFE` commands could still share state (e.g. a temp dir).  Accepted:
   the fallback on any mutation covers the realistic races, and read-only
   overlap is idempotent by definition.
2. **Timing-dependent tests** — mitigated by the event-handshake pattern; no
   `asyncio.sleep`-based assertions anywhere in Phase 2.
3. **Live event-order consumers** — transcript/history are unaffected (blocks
   appended post-hoc in input order); the two UIs key by `tool_id` (verified).
4. **Cancel propagation differences** — `gather` re-raises after children
   cancel; the existing repair covers dangling calls.  Covered by the Phase 2
   cancel test.

## Verification

Python suite (from repo root, `.venv/bin/python`):

```
.venv/bin/python -m pytest tests/test_parallel_tool_execution.py \
  tests/test_agent_loop.py tests/test_web_ws.py tests/test_renderer.py -q
```

Manual, real DeepSeek: a turn asking for several independent reads (e.g.
"read A and B and C") — in MANUAL mode reads are auto-approved, so the round
takes the max latency instead of the sum; two tool cards open together and
complete near-simultaneously in the TUI, then in the web console at
`tod serve`.  A turn with a write plus reads stays visibly sequential.
