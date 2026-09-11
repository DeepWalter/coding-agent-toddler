# Capture, Persist & Display DeepSeek `reasoning_content`

## Motivation

Toddler's default model is DeepSeek v4 (`deepseek-v4-pro`, `toddler/config/defaults.py`),
which runs in "thinking mode" by default: the model's chain of thought is returned in
`reasoning_content` — a sibling of `content` on the assistant message (non-streaming) and
as `delta.reasoning_content`, streamed *before* the answer text (streaming).  Reasoning
tokens are itemized separately in `usage.completion_tokens_details.reasoning_tokens`.

Toddler currently **drops reasoning entirely**: the provider only reads `content` and
`tool_calls`.  Two forces make capture necessary rather than nice-to-have:

1. **API echo-back requirement.**  Between two user messages, when tool calls are in
   play, DeepSeek requires the tool-round assistant messages' `reasoning_content` to be
   passed back on *every* subsequent request, or the API returns HTTP 400
   (`reasoning_content in thinking mode must be passed back to the API`).  Toddler is a
   tool-calling loop, so missing the echo fails mid-turn.  Reasoning from *completed*
   rounds (after a later user message) is ignored by the API, so old sessions stored
   without reasoning stay valid — there is no backfill obligation.

2. **User visibility.**  Reasoning should appear in the transcript as a
   collapsed/expandable "thinking" disclosure (Claude Code-style), in **both**
   frontends: the terminal TUI (live streaming view) and the web console (live stream
   + transcript replay).  Decisions taken with the user: both surfaces; capture must be
   lossless / verbatim.

Two related defects surfaced during exploration and are in scope:

- **Streaming loses usage.**  The final usage-bearing chunk of a DeepSeek stream has an
  empty `choices` array and is skipped before its `usage` is read.  Until fixed,
  `reasoning_tokens` can never reach the code on the streaming path (and per-call usage
  is already being dropped today).
- **`max_tokens` budget.**  Reasoning tokens consume the `max_tokens` budget (default
  **8192**, `toddler/config/defaults.py`).  A thinking-heavy run can exhaust it and
  return `finish_reason="length"` with empty `content`.  Handled by documentation only —
  the code cannot know before a request whether the model will reason, and raising the
  global default penalizes non-thinking requests.

## Key design decisions

1. **The message model mirrors the DeepSeek assistant message.**  The API's fields are
   `content` / `reasoning_content` / `tool_calls`; the internal model names the same
   things the same way (adopted with the user, 2026-09-07, on code-clarity grounds —
   refined 2026-09-08: the block *payload slot* keeps the container name `text`; the
   concept words `content`/`reasoning` hold at kind, property, event-type, and
   frame/replay-key level; delta *payload keys* follow the slot rule one level down —
   named after the payload slot they feed (`text_delta`, mirroring how
   `ToolCallDelta.input_delta` feeds the `tool_input` slot), never after the concept —
   so nothing translates text → content between stream chunk, stored row, and join
   property).  Concretely, a
   codebase-wide *rename baseline* precedes the reasoning work: the block class
   `ContentBlock` → `MessageBlock`; the content list field `Message.content` →
   `Message.blocks`; the block kind `"text"` → `"content"` — its payload field is *not*
   renamed (decision 2: it becomes the shared prose slot); the `Message.text` property
   → `Message.content` (answer join) plus a new `Message.reasoning` (reasoning join);
   the streaming layer `StreamEvent "text_delta"` / `AgentEvent TextDelta(text)` →
   `"content_delta"` / `ContentDelta(text_delta)` — the event *type* takes the concept
   word, the payload *key* takes the shared slot name plus the delta suffix (mirroring
   `input_delta`); the WS frame type and TS delta-frame member follow the same shape.
   Reasoning then plugs in as the symmetric sibling.  The only
   vocabulary that stays put is UI anatomy — TS console block kinds
   (`assistant`/`tool`/`thinking`) and terminal view names — which are component-level,
   not message-level.

2. **A new kind `"reasoning"` sharing the `text` payload slot** — the kind is the
   discriminator, `text` is the container: `content` and `reasoning` blocks both carry
   their prose in `text`, the same way a `tool_use` block's payload is
   `tool_id`/`tool_name`/`tool_input` rather than `tool_use`.  The message-level name
   `content` stays reserved for the answer concept at kind, property, event, and wire
   level.  Reasoning blocks are stored **first** in `Message.blocks` (the model emits
   reasoning before answer text).  Every answer-only consumer is then protected *by
   construction*: `Message.content` joins only `type == "content"` blocks, so
   compaction summaries, web transcript `content`, and `/view` flush files stay
   reasoning-free without any per-consumer filtering.  Because the payload key never
   renames, the storage alias of decision 4 stays type-only.

3. **Echo-back is self-scoping.**  `_messages_to_openai` attaches a `reasoning_content`
   key to assistant request dicts *whenever a reasoning block is present*, without a
   model/base-url guard.  Only history produced by a DeepSeek endpoint can carry the
   block, so only DeepSeek traffic ever receives the key; a name/URL guard would break
   renamed self-hosted DeepSeek servers (missing echo → guaranteed 400).  Assistant
   request messages are plain dicts, so the extra key rides through the OpenAI SDK
   unvalidated.  Residual risk: a strict third-party validator rejecting unknown keys
   — accepted, with a code comment.

4. **Storage: no schema change, one read-time alias.**  Persistence stores per-block
   whitelist dicts in the existing `content_json` TEXT column; reasoning blocks
   serialize under the same `text` key as content blocks, so the serializer needs no
   per-kind branch.  The kind-value rename is the one wrinkle: rows written before the
   rename carry `"type": "text"` — the deserializer maps that legacy value to the
   `content` kind.  Because the payload key never renamed, the alias is type-only: a
   legacy row's `text` payload reads back verbatim, with no second alias for the key
   (a permanent one-line alias plus a regression test; no migration, no schema
   change).

5. **Compaction needs no change.**  Compaction only summarizes *old completed turns*
   and keeps recent messages verbatim (tool_use/tool_result pairing already requires
   it), so in-flight reasoning rides along; `Message.content`-driven summarizers exclude
   it automatically.  Cancellation capture is free the same way: `get_partial_content`
   routes through the shared block assembler.

## Shared vocabulary

One word per concept at every boundary, from wire chunk to stored row to rendered
bubble — with one deliberate exception that reaches into the event layer: payload
*slots* are container-named, never concept-named.  The two prose kinds share the
single block slot `text`, so their deltas likewise share the single increment key
`text_delta` (named after the slot it feeds, exactly as `tool_use_delta` carries
`input_delta` for the `tool_input` slot), while the concept words
(`content`/`reasoning`) hold at kind, property, event type, frame type, and UI level.
Columns are the final names; "(was …)" marks the rename baseline from decision 1.

| Layer | Answer (rename baseline) | Reasoning (new) |
| --- | --- | --- |
| block class | `ContentBlock` → **`MessageBlock`**; kind `"content"` (was `"text"`), payload `text` — unchanged, now the shared prose slot | kind `"reasoning"`, payload `text` |
| `Message` | field `blocks` (was `content`); property `content` = join of kind `content` reading the shared payload (was property `text`, [messages.py:119-124](toddler/llm/messages.py#L119-L124)) | property `reasoning` = join of kind `reasoning` |
| `StreamEvent` | `content_delta`, `data={"text_delta": ...}` (was `text_delta`/`"text"`) | `reasoning_delta`, `data={"text_delta": ...}` (same shared key) |
| `AgentEvent` | `ContentDelta(text_delta: str)` (was `TextDelta(text)`) | `ReasoningDelta(text_delta: str)`, beside it ([events.py:31](toddler/agent/events.py#L31)) |
| WS frame / TS Frame | `{"type": "content_delta", "text_delta": ...}` (was `text_delta`/`text`) | `{"type": "reasoning_delta", "text_delta": ...}`, new union member |
| ReplayMessage | `content: string` (unchanged) | `reasoning?: string`, attached to the assistant entry |
| Console block kind | `'assistant'`, payload `content` (was `text`) | `'thinking'`, payload `reasoning`, `{id, kind, reasoning, open}` — **new kind**; do *not* reuse `'fold'` (that is the non-expandable muted marker line with its own label machinery) |
| Vue component | assistant bubble unchanged | `ThinkingCard.vue` (body = `block.reasoning`), copies ToolCard's collapse anatomy |
| Terminal | renderer buffer `_content_buf` (was `_text_buf`) | `_thinking` accumulator + dismiss "thinking" view |

Factories follow the kinds and take the payload slot's name: `content_block(text)`
(was `text_block(text)`), `reasoning_block(text)`.  `TokenUsage` gains
`reasoning_tokens: int = 0` (propagated in `__add__`; `total` unchanged — reasoning
tokens are already inside `output_tokens`).  Screen labels (`💭 Thought`, "Thinking…")
are UI strings, not vocabulary.

## Phase 1 — Data model (rename baseline + reasoning) ✅

- `toddler/llm/messages.py` — **rename baseline first**: class `ContentBlock` →
  `MessageBlock`; field `Message.content` → `Message.blocks`; kind `"text"` →
  `"content"` — the payload field keeps its name and becomes the shared prose slot
  (factory `text_block` → `content_block`, parameter unchanged); property `Message.text`
  → `Message.content` (`"".join(b.text for b in self.blocks if b.type == "content"
  and b.text)`).  Then the extension: add `"reasoning"` to the type Literal, reusing
  the `text` payload field (no new field); factory `reasoning_block(text: str)`;
  property `Message.reasoning` (join of kind `reasoning`, same payload read) for the
  echo and the web layer.  Update the type→payload docstring table — the `content` and
  `reasoning` rows share the `text` slot.  Existing callers of the renamed symbols are
  updated in the same pass —
  provider joins, token counter, summarizer, storage serializers, cli app/renderer, web
  serializer, tests/mocks, `DummyAsyncOpenAI`; the phases below name each as it is
  reached.
- `toddler/llm/responses.py`: `StreamEvent` type Literal `"text_delta"` →
  `"content_delta"` plus new `"reasoning_delta"`; both kinds carry their increment
  under the shared slot-derived key `"text_delta"` (docstring table: the old `"text"`
  key gains the delta suffix);
  `TokenUsage.reasoning_tokens: int = 0` (+ `__add__`).

## Phase 2 — Provider (`toddler/llm/provider.py`) ✅

- **`_generate_streaming` — reasoning deltas + usage-trailer fix.**  Restructure the
  chunk loop: read `usage` from *every* chunk (keep the last non-zero as `last_usage`);
  `continue` on empty `choices` only *after* usage capture (fixes the dropped trailer);
  yield a `reasoning_delta` StreamEvent *before* `content_delta` when
  `getattr(delta, "reasoning_content", None)` is present (a chunk may carry both; both
  kinds build `data={"text_delta": ...}` — the shared slot key);
  emit `message_stop` at the finish chunk with `last_usage`, plus an after-loop
  exact-once fallback stop for endpoints that send usage only on a post-finish trailer.
- **`_extract_usage`**: read `completion_tokens_details.reasoning_tokens` defensively
  (absent on other providers' responses and the Dummy's chunks).
- **`_openai_message_to_internal`**: the answer string becomes a kind-`content` block;
  if `getattr(oa_msg, "reasoning_content", "")` is non-empty, `insert(0, ...)` a
  reasoning block — matching the streaming assembly order.
- **`_messages_to_openai`**: on the assistant branch, the wire `content` comes from the
  `Message.content` property; join reasoning blocks (via `Message.reasoning`) and, when
  non-empty, set `openai_msg["reasoning_content"]` (applies to both the tool-call and
  plain sub-cases).  The `role == "tool"` fan-out branch is untouched.  Comment citing
  the DeepSeek echo requirement.
- **`max_tokens`**: documentation only — module note in `provider.py` + a README
  troubleshooting line (thinking consumes the budget; `finish_reason="length"` with
  empty `content` → raise `TODDLER_MAX_TOKENS`; `reasoning_tokens` in usage shows the
  split).

## Phase 3 — Events and handlers ✅

- `toddler/agent/events.py`: rename `TextDelta(text)` → `ContentDelta(text_delta)`; add
  `ReasoningDelta(text_delta: str)` beside it — the field is named after the shared
  `text` slot the fragment feeds, like `ToolCallDelta.input_delta`; the kind lives in
  the class name.
- `toddler/agent/handler.py` `StreamHandler`: rename `_text_buf` → `_content_buf`; new
  `_reasoning_buf` (init + `clear`); case `"content_delta"` (renamed) appends
  `data["text_delta"]` to the content buffer and yields `ContentDelta(text_delta=...)`;
  new `case "reasoning_delta"` appends `data["text_delta"]` to the reasoning buffer and
  yields `ReasoningDelta(text_delta=...)`;
  `_content_blocks` prepends a reasoning block when the reasoning buffer is non-empty
  (final order `[reasoning?, content?, tool_use...]`).  Both `_assemble_message` and
  `get_partial_content` route through `_content_blocks`, so a cancelled turn persists
  its partial reasoning and the next request echoes it — exactly what DeepSeek
  requires.
- `NonStreamHandler.process`: yield one joined `ReasoningDelta(text_delta=...)` *before*
  the `ContentDelta(text_delta=...)` when the response message carries reasoning
  (non-streaming arrives whole; mirrors stream order).
- No changes downstream: `AgentLoop.run`, `SessionManager.process_turn`, and the
  planner re-yield agent events verbatim; the renamed event classes flow through the
  existing dispatch (Phases 5-6 update the case arms); compaction and the cancel-repair
  path need nothing (see design decision 5).

## Phase 4 — Token counting and storage ✅

- `toddler/context/token_counter.py` `_count_block`: kind `content` (renamed from
  `text`); because both prose kinds share the `text` payload, one branch counts them
  together (`if b.type in ("content", "reasoning") and b.text` — reasoning is re-sent
  on every request of the round, so it must be charged to the context window); unknown
  types still return 0.
- `toddler/session/storage.py`: the serializer whitelist key is unchanged —
  `d["text"] = b.text` — and now covers both `content` and `reasoning` kinds; the
  deserializer passes `text=...` through for both and maps a stored `"type": "text"`
  (rows predating the rename baseline) to the `content` kind — the permanent read-time
  alias of design decision 4.  The payload key never renamed, so legacy rows need no
  second alias.  No schema change.

## Phase 5 — Web backend (`toddler/web/events.py`) ✅

- `serialize_event`: rename the `TextDelta` arm → `ContentDelta` →
  `{"type": "content_delta", "text_delta": ...}`; map `ReasoningDelta` →
  `{"type": "reasoning_delta", "text_delta": ...}`.  (The existing `AgentEvent`
  catch-all is the forward-compat safety net.)
- `serialize_transcript`, assistant branch: `entry["content"] = msg.content` (property
  — was `msg.text`) and attach `entry["reasoning"] = msg.reasoning` (joined verbatim);
  **emit the entry when `msg.content or msg.reasoning`** — today's `if msg.text:`
  swallows thought-only assistant messages (reasoning then a tool call), which must
  replay as a thinking card.  Entry order stays `[thinking, content, tool_use...]`,
  matching the live stream.  No changes needed in `ws.py` or `api.py` (both call this).
- `serialize_token_usage`: add `reasoning_tokens`.

## Phase 6 — Terminal TUI (live view) ✅

- `toddler/cli/app.py` `_run_agent_turn` dispatch: rename the `ContentDelta` case →
  `self._renderer.on_content_delta(event)`; add `case ReasoningDelta():` →
  `self._renderer.on_reasoning_delta(event)` (without the arm the catch-all silently
  drops thinking).
- `toddler/cli/renderer.py` `StreamingRenderer`:
  - State: `_thinking = ""` and `_dismiss_view: Literal["output", "thinking"]`, both
    reset in `start()`.
  - `on_content_delta` (renamed from `on_text_delta`): accumulate `event.text_delta`
    (the fragment field, formerly `event.text`); `on_reasoning_delta`: accumulate
    `event.text_delta` + `_refresh()`.
  - `_build_renderable`: in the `"thinking"` dismiss view (non-empty `_thinking`) the
    scrollable panel shows `Markdown(self._thinking)` titled "Thinking" instead of the
    Output panel — reusing the existing clip/scroll machinery; otherwise, when
    `_thinking` is non-empty, a dim collapsed line above the Output panel:
    `💭 Thought — press t to review` (dismiss) / `💭 Thinking…` (live streaming).
  - `_wait_for_dismiss`: enter the raw key loop when scrollable **or**
    (`_thinking` and stdin is a tty — the termios path crashes on piped stdin);
    `_wait_for_dismiss_scrollable` gains `t`/`T` → swap `_dismiss_view` +
    `_refresh(force=True)` (no-op when no thinking).  Existing keys are ↑/↓/PgUp/PgDn/
    Enter/q/Q/Ctrl+C — `t` is free.  Expanded-while-streaming is deliberately not
    supported: keys only exist at the dismiss screen.
  - `flush_to_console`: normal output plus a single dim `💭 Thought — N chars`
    summary line in the scrollback (no interaction after the alt screen; the full text
    is lossless in the session DB).  Reset `_dismiss_view` in `stop()`.
- `NonStreamingRenderer`: plain scrollback cannot collapse — print dim
  `💭 Thought:` then dim-italic text, once per message (NonStreamHandler yields one
  ReasoningDelta).

## Phase 7 — Web frontend (`website/src`) ✅

- `types.ts`: `TokenUsage.reasoning_tokens`; `ReplayMessage.reasoning?: string`;
  Frame members `{ type: 'content_delta'; text_delta: string }` (renamed from
  `text_delta`/`text`) and `| { type: 'reasoning_delta'; text_delta: string }`;
  the `'assistant'` block payload renames `text` → `content`; Block union member
  `| { id, kind: 'thinking', reasoning, open }` — `open` means still streaming
  (tool-block semantics); the UI's expanded/collapsed state stays local to the
  component.
- `useConsole.ts` reducer, with a `closeThinking` helper mirroring `closeAssistant`:
  - `content_delta` (renamed): `closeThinking` *first* (reasoning precedes its text;
    thought-only replies close before execution events), then merge
    `frame.text_delta` into the last open assistant block's `content` or push one.
  - `reasoning_delta`: append `frame.text_delta` to the last *open* thinking block's
    `reasoning`, else push one open.
  - `tool_call_start`: `closeThinking` first.
  - `agent_finished`, `fatal_error`, `turn_cancelled`: close thinking alongside
    assistant/tool close calls (mid-cancel partial reasoning closes; the persisted
    partial message replays the same card).
  - `hello` replay: reorder so the `if (!msg.content) continue` guard no longer
    swallows thought-only entries — push a thinking block (`open: false`) when
    `msg.reasoning` is present, then the assistant bubble when `msg.content` exists.
    Parity is structural: a replayed card is closed exactly like a live card after
    `agent_finished`.
- New `ThinkingCard.vue` (copy ToolCard's collapse anatomy): `💭` + header label
  `Thinking…` while `block.open`, else `Thought`; chevron `▾/▸`; body `v-if` on a local
  `expanded` ref, rendered as a `<pre>` with `white-space: pre-wrap` **plain text**
  (CoT is freeform prose that markdown would mangle) showing `block.reasoning`, dim
  styling, full text on expand (bounded by `max_tokens` server-side).  Wire into
  `ConsolePane.vue` between the assistant bubble and tool-card branches; add
  `.thinking-card*` styles mirroring `.tool-card`.

## Phase 8 — Test mocks ✅

`tests/mocks.py`: `MockLLMProvider._stream`'s per-type branches emit the renamed
`content_delta` events (data `{"text_delta": ...}`) for kind `content`, plus a new
`reasoning` branch chunking the text into multiple `reasoning_delta` events (same
`text_delta` key, so accumulation is exercised end-to-end); new factory `reasoning_response(reasoning, content, *, ...,
reasoning_tokens=40)` producing `[reasoning_block, content_block]` with usage carrying
`reasoning_tokens`.

## Phase 9 — Tests ✅

- New `tests/test_reasoning.py` (mirror `TestHandlerPartialContent`):
  reasoning_delta chunks → yields ReasoningDelta; mid-stream `get_partial_content`
  shows a reasoning block (cancel path); a full stream assembles content types
  `["reasoning", "content", "tool_use"]` verbatim; `clear()` resets; NonStreamHandler
  yields ReasoningDelta before ContentDelta.
- Storage round-trip: `_serialize_content` / `_deserialize_content` across all four
  block kinds → deep-equal (no dedicated round-trip test exists today), **plus** a
  legacy row carrying `"type": "text"` deserializing as kind `content` with its `text`
  payload verbatim (the read-time alias of design decision 4 — asserts the alias is
  type-only).
- Token counter: a reasoning block counts like content.
- `TokenUsage.__add__` preserves `reasoning_tokens`.
- New `tests/test_provider_reasoning.py`: `_messages_to_openai` emits verbatim
  `reasoning_content` for `[reasoning, content]` and `[reasoning, tool_use]` messages
  and omits it otherwise; tool fan-out untouched; `_openai_message_to_internal`
  ordering; streaming regression test with a stubbed client whose chunk sequence ends
  in a usage-only empty-`choices` trailer → `message_stop` carries `reasoning_tokens`
  (the only testable net for the usage-trailer fix).
- `tests/test_web_events.py`: content_delta (renamed) and reasoning_delta frames;
  transcript attach / thought-only entry / key absence / ordering; usage serializer.
- `tests/test_web_ws.py`: live reasoning_delta frames → `agent_finished` usage
  reasoning_tokens; a second connection's `hello` replays the assistant entry with
  `reasoning` (real pipeline round-trip); echo-back: a two-response turn asserts the
  second LLM call's message history carries the prior reasoning block.
- `tests/test_renderer.py`: `on_content_delta` (renamed) and `on_reasoning_delta`
  accumulate; the renderable shows the collapsed line, and the `"thinking"` dismiss
  view renders the Thinking panel; NonStreaming prints dim text.  Existing
  height-budget tests stay green untouched.
- `toddler/llm/_async_openai.py` `DummyAsyncOpenAI` (manual `TEST=cli` path): renamed
  chunk kinds (`"content"` → `delta.content`, `"reasoning"` → `delta.reasoning_content`);
  a canned `_chunks_for_reasoning` script (short CoT → answer text → finish); dispatch
  on content `"reason"`.

## Phase 10 — UI harness (`website/scripts`) ✅

- `mock-server.mjs`: answer chunks emit `content_delta` frames carrying `text_delta`
  (renamed type and key); one seeded assistant message carries `reasoning`; a
  module-level `turnLog` (patterned on
  `cancelMarkers`) so completed "think" turns replay after reload; turns whose input
  starts with `think` stream `reasoning_delta` frames (same `text_delta` key) before
  `content_delta` and push `{role: 'assistant', content, reasoning}` on finish.
  Non-think turns change only in the renamed frame type and key — byte-identical
  otherwise.
- `ui-test.mjs`: seeded thinking card collapsed by default (`Thought` + `▸`), click
  expands to verbatim body, click collapses; a live `think:` turn produces a second
  card *before* its message bubble reading `Thinking…` while streaming and `Thought`
  after `agent_finished`; after `page.reload()` the card count and body text are
  byte-identical and the card sits above its assistant bubble (live/reload parity).

## Out of scope

Per-call thinking on/off (`extra_body` plumbing); reasoning-cost UI beyond the
`TokenUsage` field; `/view` and flush truncation files stay answer-text-only (the DB is
the lossless store); compaction changes; expanding the thinking section *during* live
streaming in the terminal (keys only exist at the dismiss screen).

## Risks

1. The `message_stop`/usage restructure touches every provider's stream termination —
   exact-once semantics + the empty-`choices` regression test; the full web WS suite
   re-verifies the happy path.
2. The TUI dismiss loop rework has no stdin-driven unit tests — `isatty()` guard,
   state-level tests, manual `TEST=cli` verification.
3. Unconditional echo key — a strict third-party validator could reject it; accepted
   trade-off (self-scoping argument), documented in a code comment.
4. Live/replay parity of the thinking card depends on the reducer close rules; a chunk
   carrying reasoning + content interleaved could split cards live vs one in replay —
   cosmetic, never content loss.
5. The rename baseline's read-time alias (legacy `"type": "text"` rows) must outlive
   the sessions that wrote them — type-only, since the payload key never renamed;
   covered by the storage round-trip test; no migration needed.
6. Long CoT rendering grows the DOM on expand — bounded by `max_tokens`; full render
   is deliberate (verbatim requirement).

## Verification

Python (repo root, always the project venv):

```
.venv/bin/python -m pytest tests/test_reasoning.py tests/test_provider_reasoning.py \
  tests/test_agent_loop.py tests/test_web_events.py tests/test_web_ws.py \
  tests/test_renderer.py tests/test_token_count_calibration.py \
  tests/test_conversation_token_persistence.py -q
```

All pre-existing suites staying green doubles as the regression net for the
"reasoning never leaks into answer text" invariant.

Manual terminal streaming through the real provider path:

```
printf 'reason\n/quit\n' | TEST=cli .venv/bin/python -m toddler
```

Expect: dim `💭 Thinking…` line while streaming; at the dismiss screen press `t` to
flip to the scrollable "Thinking" panel; Enter → scrollback shows output plus
`💭 Thought — N chars`.  `text` turns show no thinking UI; `read` (tool) turns show
thinking before the tool row.

Web harness (Playwright against the mock — no Python backend needed; `npm run build`
runs `vue-tsc`):

```
cd website && npm run build
node website/scripts/mock-server.mjs            # :8100
cd website && npx vite --config mock-vite.config.ts   # :5199, proxies to the mock
node website/scripts/ui-test.mjs                # per-phase assertions, exit 1 on failure
```

Real DeepSeek (needs `DEEPSEEK_API_KEY`): a tool-using task completes its second LLM
call (a missing echo manifests as an API 400 mid-turn); a second user message and a
`/compact`-followed message both succeed (recent reasoning-bearing messages are kept
verbatim by compaction); `sqlite3 ~/.toddler/sessions.db` shows `"type": "reasoning"`
and `"type": "content"` blocks verbatim in `content_json` (and legacy `"type": "text"`
rows still load); `tod serve` renders expandable cards live and identical ones after a
page reload.
