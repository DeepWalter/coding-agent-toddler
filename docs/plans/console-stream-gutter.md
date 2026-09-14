# The console's status gutter

## Motivation

The web console renders a linear transcript of blocks — user input, assistant
content, thinking, tool calls, plan cards, and stream lines. Every block sat
flush against the pane's left edge, and the only running/settled reading in the
whole transcript lived inside a tool card's header, in a `state` computed
private to `ToolCard.vue`. So a streaming answer, a live thought, and a settled
one all rendered identically: the sole cue that anything was in flight was text
growing.

The change gives the transcript a scan rail, in two halves:

1. **Indent every output block**, leaving user input flush left — the left edge
   then separates "you" from "the agent", and the user's box lines up with the
   compose field below it.
2. **One animated status mark per output block**, outdented into the gutter the
   indent creates: a spinner while running, then `✓` / `✗` / `·` once settled.

Decided with the user, 2026-09-14:

- **Gutter rail, not inline.** The mark sits outdented in the gutter, aligned
  with the block's first line, rather than leading the block's own first line.
  The indent is what pays for it.
- **Scope is every non-user block** — thinking, content, tools, plan cards, and
  the error/notice/fold stream lines. Only user input stays flush.
- **One uniform vocabulary.** Running / `✓` / `✗` / `·`, the same for every
  kind, extending the four states the tool card already used.

## Key design decisions

1. **A wrapper row owns the indent and the mark.** Every block renders inside a
   `.stream-row` ([ConsolePane.vue:180-191](website/src/components/ConsolePane.vue#L180-L191)),
   which carries the indent as its left padding and holds the block's single
   mark absolutely positioned in it.

   The alternative — each block rendering its own outdented mark — is not
   actually implementable. `.tool-card` carries `overflow: hidden`
   ([styles.css:733](website/src/styles.css#L733)), and that clip is
   load-bearing: the card is a `border-radius: 6px` box whose header paints a
   full-width `:hover` background, which the clip rounds off. An outdented mark
   (`left: -22px`) inside the card is clipped, and dropping the clip squares off
   the hover fill. Per-component would also mean five copies of the status, or
   threading a `status` prop through `MessageBubble` (breaking its
   `{role, text}` contract) and `PlanCard`.

   The row is a **block box, never a flex row**: a card's `flex: none` would,
   in a row-flex container, pin its *width* to max-content and let long tool
   signatures overflow the pane. Carrying `flex: none` on the row instead
   ([styles.css:553-561](website/src/styles.css#L553-L561)) hands the cards'
   own min-size guard to the single element that is now the flex item.

2. **One status vocabulary, computed centrally.** New module
   [blockStatus.ts](website/src/blockStatus.ts) owns the transition logic;
   new component [StatusMark.vue](website/src/components/StatusMark.vue) owns
   the glyphs and the spinner. `ToolCard` keeps its computed, now delegating —
   so its error border and its trailing word stay exactly as they were.

   It lives at `src/` root, not in `types.ts` (whose header commits it to
   mirroring the Python serializers; it is a type-only contract) and not in
   `utils.ts` (token and path helpers for the panes). Precedent:
   `reasoning.ts`, `choiceNav.ts`, `gitStatus.ts`.

   A status is read from the block alone, **never from console-level `busy`**:
   one that consulted `busy` would make the same block mean different things in
   two tabs, and every kind already carries its own lifecycle flag — assistant
   blocks `closed`, thinking and tool blocks `open`, plans a `decision` plus
   their step rows.

3. **The tool card's trailing word stays.** Only the *mark* moved. "done" /
   "failed" / "cancelled" say what a glyph cannot — `✗` cannot distinguish a
   failed call from one the turn abandoned.

4. **The thought's `💭` and the card's inline mark are gone.** The gutter
   carries one mark per block now; a second reads as a second state. The labels
   ("Thinking…", "Thought for 3 seconds") carry the identity instead.

5. **The answer block is closed when the stream is interrupted.** §Phase 2.

## Shared vocabulary

| State | Glyph | Colour | Source of truth in the block |
| --- | --- | --- | --- |
| `running` | spinner | `--accent` | `assistant.closed === false`, `thinking.open`, `tool.open`, or a plan that is undecided / mid-step |
| `ok` | `✓` | `--success` | `closed`, `!open` with a successful result, all steps `completed`, a notice (its command completed) |
| `error` | `✗` | `--error` | a tool result with `success: false`, an error line, a plan step `failed` |
| `cancelled` | `·` | `--text-subtle` | a tool closed with `result: null`, a fold line, a rejected plan |

Two mappings are judgment calls, both recorded: **a settled thought is `ok`**
even when the turn was cancelled — the fold line below it carries the cancel,
and the thought itself finished saying what it had to say; and **a notice is
`ok`** — the command completed, and the notice is its own success report.

`.step-status.status-failed` ([styles.css:948](website/src/styles.css#L948))
has never had a producer: `PlanStepStatus` is `pending | in_progress |
completed` ([plan.py:35-37](toddler/tools/plan.py#L35-L37)). The `error` branch
in `blockStatus` is kept anyway, so a dead step can never leave a spinner
turning if the status is ever added server-side.

## Phase 1 — Extract the status ✅

`blockStatus.ts`, `StatusMark.vue`, and `ToolCard` delegating. No visual change.

## Phase 2 — Close the answer block when the stream is interrupted ✅

`content_delta` merges only into the *last* block
([useConsole.ts:208-213](website/src/composables/useConsole.ts#L208-L213)), but
`closeAssistant` ran only at turn end — so a text → tool → text turn left **two**
assistant blocks open, one of them permanently stale and unable to grow.
Nothing rendered that flag, so it went unnoticed until the gutter began to.

The fix closes the answer block at every event that interrupts the stream, the
same rule the thought blocks already followed: `tool_call_start`,
`plan_proposed` (which holds while the user decides), and `recoverable_error`.
Merging is unaffected — the event's own block is last once it lands, so later
text opens a fresh block either way. Only the stale flag changes.

*Rejected:* a UI-only rule ("an open assistant block renders running only while
it is the last block"). It avoids the reducer, but `closed` would stop meaning
what it says and the position knowledge would live in the view.

## Phase 3 — The rows, the mark, the CSS ✅

`ConsolePane` wraps each block in `.stream-row` and renders one `StatusMark`.
The gutter is one custom property, `--stream-gutter: 22px`, set on
`.console-pane` so the row's padding and the mark's box can never drift apart.

The row is marked with `data-kind`, not a class of the same name: `.thinking`
is already a top-level rule ([styles.css:813](website/src/styles.css#L813)),
and the `Block` union's names should not own the class namespace. Precedent:
`:data-expanded` on `.tool-card`, `data-file` from the markdown renderer.

The mark's box is exactly one line of the global 13px/1.55 type and is centred,
so per-kind `top` values are just each block's own top inset copied from its
rule — a card's border plus its padding, a stream line's own padding. Assistant
content and the thought toggle start their first line flush with the block top
and take the default. Child combinators keep the offsets from reaching a nested
block.

## Phase 4 — Harness coverage ✅

`thoughtAboveBubble()` compared `.console-pane`'s pane-child *indexes*, so
wrapping every block in a row broke it; it now compares document order, which
is what the assertion actually meant. New phase 6 asserts the contract —
indent, one outdented mark per output block, none on user input, uniform
vocabulary with nothing left turning, the gate holding exactly one live mark
with its answer settled, and `·` / `✗` for the two unhappy endings. Edges are
compared, never a hard-coded width.

`mock-server.mjs` gained its first `fail:` fixture — the only producer of `✗`,
since the seed only ever succeeds and the gate path is always approved.

**Coverage gap:** the mock has no plan fixtures, so the plan row's mapping
(`running` → `ok` / `cancelled`) is unverified by the harness. The plan card's
lifecycle is still exercised through the real server path.

## Out of scope

- Gating the plan mark on console `busy`. The card's own note already reads
  "running…", so the mark matches it; making both honest needs a `busy` prop
  through `App` → `ConsolePane` and a server-side terminal signal.
- A hover tooltip or text on the mark.
- A distinct settled glyph per kind for content blocks.
- Plan fixtures in the mock.

## Risks

- The wrapper's `flex: none` is load-bearing — removing it re-shrinks rows
  exactly as the card comment warns.
- A block whose first child is a fenced `pre` puts the mark ~8px above the
  code's first line (`:first-child { margin-top: 0 }` zeroes the margin, but the
  `pre` keeps its 8px padding); a first-child `h1` is ~1.5px off. Accepted —
  fixing it needs a per-row measurement observer.
- The `.thinking-quote`'s 2px left rule now sits 8px right of the mark: two left
  rails close together. If it crowds, widen the gutter rather than moving the
  quote.
- `.stream-line.error` shows a red border, a red tint, **and** a red `✗` — three
  signals for one state, kept deliberately; its inline `⛔` is likewise
  redundant with the mark and is the obvious thing to drop if it reads as noise.
- A `✓` on every settled block makes a long transcript a column of green.
  Tunable to `--text-subtle` for `ok` without touching the vocabulary.
- The plan mark is unverified by the harness (see the coverage gap above).

## Verification

```
cd website && npm run build && cd ..        # vue-tsc is the type contract
node website/scripts/mock-server.mjs                     # :8100
cd website && npx vite --config mock-vite.config.ts      # :5199
node website/scripts/ui-test.mjs                         # exit 1 on failure
```

The mock is **stateful** — its `transcript` accumulates live turns and cancel
markers, and the context pill's percentage persists — so a second run against
the same process fails phase 3's fold count and phase 4's pill. Restart the
mock between runs.

By eye in both selectable themes (tokyo-night, github-light), checking
`--success` / `--error` legibility on `--editor-surface`: a `think:` turn
(spinner → `✓`, and never two blocks spinning at once), a `gate:` turn (only the
tool's mark live), a `fail:` turn (`✗`), a cancel (`·`), a reload (everything
replays settled), and reduced motion on. The Python web suites
(`tests/test_web_events.py`, `test_web_ws.py`, `test_web_api.py`) are the
"frames untouched" net — this change never touches the wire.
