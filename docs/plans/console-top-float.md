# The console's floating input box

## Motivation

The console is a long linear transcript, and it rests pinned to its bottom. Once
the reader scrolls up into history, the prompt that produced the output they are
reading is off-screen: nothing on screen says which turn they are inside, and
the further they scroll the less they can tell. Every assistant block reads the
same — only the user's own boxes delimit turns, and those are exactly what the
viewport has scrolled past.

The change pins a copy of the input box the fold has reached to the top edge of
the console, the way the tool gate and the input bar already float over its
bottom edge, and hands it over to the next one as that arrives. Its content
follows the fold — which is what makes it worth having in both of the cases that
matter: reading back through history, and watching a reply long enough to have
pushed its own prompt off the top.

Decided with the user, 2026-09-16, in three passes the same day:

- **One line, ellipsized** — the transcript's own collapsed box, reused as-is.
  *Revised:* that box is one line because `white-space: nowrap` collapses the
  reader's newlines into spaces, a run-on sentence they never wrote — and the
  transcript can undo it with `more ▾` while the float could not.
- **Three lines, then a fade.** The echo keeps its breaks, stands three of them
  up, and the third dissolves into the box's own fill — a reading of "there is
  more" rather than a box that happens to end there.
- **A chip that opens the rest.** *Revised:* a fade that cannot be answered is
  a taunt — the reader could see there was more and had no way to reach it. The
  float carries the transcript's own `more ▾` / `less ▴`, which is the one
  control in it; everything else still belongs to the scroll.
- **The transcript's own box reads the same way.** *Revised:* the echo and the
  row it copies are one box, so they are one reading. Standing the transcript's
  copy down to a single ellipsized line while the echo showed three was two
  answers to the same message, and the one-line reading was the answer that
  flattened the reader's newlines in the first place.
- **No grace band, and no threshold.** *Revised twice:* the echo appears as soon
  as the fold reaches a box's **top** — not once the box has cleared it — and it
  stands until the next input reaches its own bottom edge. Every "three lines" in
  the first cut was an approximation of a box's height, and the bottom-edge test
  was an approximation of the box being gone: the echo can cover the box instead
  of waiting for it to leave, because it is the same box.
- **Shown at the live tail too.** *Revised:* the first cut hid the float while
  the console was pinned to the bottom, on the reading that it means "you are
  reading history". That is backwards for the case the float is most useful in: a
  long answer takes the prompt that asked for it off the top, and the answer
  coming in cannot say which turn it belongs to. The tail is where that prompt
  needs echoing, not where it should be withheld.

Scope: `website/` only. The wire is untouched — no protocol, no server change —
so the Python web suites stay the "frames untouched" net.

## The rule

Everything is measured in viewport space against the pane's own top edge.
`.console-pane` has no border, so its border-box top *is* the top of the visible
output, and the pane's own top padding never enters the arithmetic.

Let `rows` be the pane's `user` rows, `fold` the pane's top edge, and `box` the
echo's own rect. The float shows when both hold:

| # | Term | Why |
| --- | --- | --- |
| 1 | Some `rows[i]` has `top <= fold` | There is a box the fold has reached; that box is what the output on screen answers |
| 2 | No `rows[i+1]`, or `rows[i+1].top > box.bottom` | The echo stands for exactly as long as it has room: the next input ends it by reaching its bottom edge |

**Term 1 reads the box's *top* edge, and that is the handover.** The first cut
read its bottom — the echo appeared only once a box was entirely gone, so a
reader scrolling through one watched most of it leave the screen with nothing
floating. Now the echo takes the box over the moment it starts to leave, and
nothing has to be hidden for that to read: the echo *is* that box — same text,
same width, same three-line clamp, same padding — so it covers the part still
showing, and the two are never read as two. The handover is one line moving
under another, not a box appearing above one.

That cover is the property term 2 does not share, and the one thing to keep an
eye on: it holds while the echo is inset to the pane's edge (`top: 0`) and both
render the same clamp. Raise the echo's inset and a sliver of the box underneath
shows above it; that is a look, not a break, but it is the difference between a
handover and a double image.

**There is no threshold.** The first cut had a three-line grace band on either
side — a box had to be three lines clear of the fold before it was echoed, and
the next had to stay three lines away. Both are gone, decided with the user on
2026-09-16: what ends the echo is its own footprint rather than a number of
lines, and what starts it is the fold reaching the box rather than the box
clearing it.

Term 1 has no special case for a box straddling the fold: it is the same
comparison. Term 2 is the spec's "the top of the visual console output is not an
input box" — the echo stands in for a box in the course of leaving, so a box
arriving under it ends it. **The two are mutually independent**: term 1 asks
which box to name, term 2 asks whether there is room for the name, and the second
cannot be answered until the first has rendered — which is why `updateFloat` is
async (below).

Note what is *not* in either term: where the scroller sits. The rule reads the
fold and the rows around it, so it answers the same way whether the reader put
the viewport there or a long answer grew under it — which is the whole reason the
float is worth having while a reply streams. One consequence reads as an
omission: for the first screenful of a reply the prompt being answered is still
in sight and the box above the fold is the *previous* turn, so the echo names
that one until the current prompt crosses the fold. Decided with the user: the
geometric reading, no special case for "an answer is in flight".

The echoed text is the `text` of the `kind: 'user'` block at that index: row `i`
is the *i*-th user block, the one-row-per-block order the template renders
([ConsolePane.vue:102](website/src/components/ConsolePane.vue#L102)). Reading it
off the DOM instead is not an option — `.message-content`'s `textContent` would
drag in the `more ▾` toggle's label.

## Key design decisions

1. **The float is `.console-wrap`'s positioned last child, and `ConsolePane` is
   multi-root.** The float cannot live inside `.console-pane`, which would scroll
   it away with the content it echoes, and it must land below `ConsoleHeader` —
   which rules out `.pane-console`, the dock's offset parent, without a
   hand-maintained copy of the header's height (`flex: none`, a 34px row, and an
   editable title).

   `.console-wrap` ([App.vue:540](website/src/App.vue#L540)) is already exactly
   that region, so the component renders the scroller and the float as two roots
   inside it. Nothing moves: no new DOM level, no `App.vue` diff, and the wrap
   gains only `position: relative`
   ([styles.css:507-514](website/src/styles.css#L507-L514)). "A sibling of the
   scroller, below the header" becomes structural rather than a comment — the
   same property `ConsoleDock` gets by being placed inside `.pane-console`
   ([ConsoleDock.vue:39-45](website/src/components/ConsoleDock.vue#L39-L45)).

   The cost is that a multi-root SFC cannot inherit attrs. `App.vue` passes props
   and handlers only, so nothing is dropped today, and Vue warns rather than
   failing silently if that ever changes — which is why no
   `inheritAttrs: false` is added to suppress it.

   *Rejected:* a wrapper div inside `ConsolePane`. It would have to restate
   `.console-wrap`'s own contract (`flex: 1`, column, `min-height: 0`) to become
   the flex item, a second place the scroll container's height chain can be
   broken. *Rejected:* a `ConsoleTopFloat.vue` placed by `App.vue`, the dock's
   shape — every input to the rule is scroller-local, so it would buy a prop, an
   emit and a `ref` in `App` that only forwards.

2. **The refresh hangs off scroll and resize, never off `blocks`.** `props.blocks`
   is a fresh clone on *every* frame (`reduce()` → `structuredClone`,
   [useConsole.ts:42-46](website/src/composables/useConsole.ts#L42-L46)), so a
   blocks-keyed refresh would measure per streamed frame — and the console reads
   `scrollHeight` on that path only while pinning. `updateFloat()` is called from
   `onScroll`, `onMounted`, the blocks watch and the `ResizeObserver`.

   The blocks watch only *invalidates* the row list
   ([ConsolePane.vue:151](website/src/components/ConsolePane.vue#L151)): content
   streaming in lands below the fold and cannot move the rows the rule reads, and
   the pin that follows a send fires the scroll event that re-reads them. The
   `ResizeObserver` calls it before its height guard, because a taller `--bar-h`
   moves the pane's content box with no scroll event behind it.

   The cost is a measurement per scroll tick — and while an answer streams at the
   bottom, the pins do fire a scroll event per frame, so that is a measurement
   per frame. It is the price of a rule that reads the fold rather than the
   scroller's pinned state, and it is small: a viewport read, one query over the
   pane's own children, and a rect for each user row in the walk below. It is
   also the same order as what the pinned path already does — `scrollToBottom`
   reads `scrollHeight`, a forced layout, on every one of those frames.

   The row *elements* are cached and every rect is read live, so only the list is
   cached and no row that grows in place can go stale. The boundary index carries
   over between ticks as a hint — the rows above the fold are a prefix of an
   ordered, disjoint list, so the answer is unique and the walk converges from
   either side: a continuously moving fold costs a rect or two, and a programmatic
   jump costs one pass, once.

   The float is written through `if (text !== floatText.value)` — a scroll tick
   that changes nothing re-renders nothing.

   **The decision is two-staged, and so `updateFloat` is `async`.** Term 2 needs
   the echo's own height, and on the tick the echo changes there is no box for it
   yet: the text is a ref and the patch lands on the next microtask. So the
   function sets the text, `await nextTick()`s only when it changed, and measures
   after. In the steady state — the common one, where the echo follows the fold
   without changing — there is no await and the decision is synchronous.

   Which is also why the box stays in the document whenever there is anything
   above the fold to name, even while it is down: `[data-hidden]` takes it out of
   paint, out of the tab order and out of the accessibility tree, but leaves it
   laid out, because a box that is not rendered has no height to hand the pane.

   **No oscillation is possible by construction:** the float is out of flow and
   the pane reserves no room for it, so its presence can never feed back into the
   geometry it measures. That is what lets the rule be stateless — no hysteresis,
   no deadband. Term 2 does not break that: it reads the echo's height, which is
   the box's content, not its visibility.

3. **The box's reading lives in `MessageBubble`, and the float is a wrapper
   around it.** The echo and the transcript's row are the *same box* — accent
   border, `--editor-bg` fill, 8/10px padding, 6px radius, `pre-wrap` — so they
   are one component, and the clamp, the fade and the chip are one implementation
   ([MessageBubble.vue:19-46](website/src/components/MessageBubble.vue#L19-L46)).
   `ConsoleTopFloat.vue` is then only what makes it float: a positioned div that
   hands the box two props and an emit, and the `aria-hidden` that keeps the
   repeat out of the accessibility tree.

   **Whether a box is open belongs to the block, not to either reading of it.**
   The row and the echo are one message, so `expanded` is a `Set` of block ids in
   `ConsolePane` ([ConsolePane.vue:73-77](website/src/components/ConsolePane.vue#L73-L77)),
   passed down as a prop, with the chip's click emitted back up. The first cut
   kept it inside `MessageBubble`, which meant the two instances drifted: the
   reader opened a message in the transcript, scrolled on, and the echo that took
   it over came up closed. Decided with the user on 2026-09-16. A block's id is
   per push, so a replay (`hello`, a compaction) drops the state with the
   elements — an opened box does not survive a reload, which is the same deal the
   transcript had before.

   The id travels with the *index* the echo resolves, not with the text it
   renders: two inputs can read the same, and an echo that compared texts would
   carry the first one's state onto the second. That was the shape of the bug
   the user hit — open one of two identical messages, scroll to the other, and
   the echo came up open over a box that had never been touched. `updateFloat`
   therefore assigns the id on every pass, and only the *text* is written on a
   change.

   The chip is *not* hidden with it: it is the one thing in the echo that is not
   a repeat, and a focusable button under `aria-hidden` is a trap. It is also why
   the chip is a child of the box rather than a sibling pinned to the wrapper —
   in the transcript it is the box's own trailing control, and here the box is
   the same box.

   **`clamped` is measured, never inferred from the text.** The old transcript
   rule was content-based — "has a newline or is over 240 chars gets a `more ▾`"
   — because content is immutable once pushed and one *line* is what it stood a
   message down to. Three lines makes that rule wrong in both directions: a short
   two-line message has nothing cut off, and a long unbroken one can overflow
   without a newline in sight, which would leave a fade with no chip. So a
   `ResizeObserver` on the box measures `scrollHeight - clientHeight`, and both
   the fade (`data-clamped`) and the chip (`expanded || clamped`) turn on the
   answer. It also re-answers when a pane resize reflows the text, which a
   content rule could never do.

   The cut is `max-height` in `lh` units, not `-webkit-line-clamp`
   ([styles.css:657-700](website/src/styles.css#L657-L700)): the clamp family
   cannot be told to drop its own ellipsis, and an ellipsis over a fade is two
   answers to one question. `lh` keeps the cut on a line boundary whatever the
   type scale is, and the stylesheet already reaches for it in the input bar's
   14-line cap ([styles.css:1327-1332](website/src/styles.css#L1327-L1332)).

   Two smaller things the chip and the fade settle between them.  The chip is
   **sized as a target**, not as a run of text — a 22px box on 12px type, and the
   corner the box reserves for it grows with it
   ([styles.css:693-716](website/src/styles.css#L693-L716)).  And the fade is a
   **`::before`, not a `::after`**: the chip is positioned in that same corner,
   and among positioned siblings with no `z-index` anywhere, tree order *is*
   paint order — a pseudo-element is generated either as the first child or as
   the last, and the last would paint the gradient over the very control it
   exists to send the reader to.

4. **Absolute, `pointer-events: none` — bar two exceptions.** The float is inset
   to the pane's content edges — `left`/`right` matching the transcript's rows so
   the echo spans exactly the box it copies, and `top` its own free value (0 as
   it stands: flush with the pane's edge, where the header's border above it is
   separation enough) — with the lift on the bubble rather than on the wrapper,
   so the shadow hugs the box's own radius instead of the pane's full width
   ([styles.css:1245-1276](website/src/styles.css#L1245-L1276)). The inset is a
   look, not the rule: term 2 measures the box whatever it is inset by. No
   `z-index`, for the dock's reason: `.console-wrap` is not a stacking context
   and the float is its last positioned child, so it paints over the rows.

   `pointer-events: none` is load-bearing rather than decoration: the float's
   ancestors do not scroll, so a wheel over it would scroll nothing and the
   console would feel frozen under the cursor. The chip takes the pointer back,
   because it is a control; and so does an *opened* box, because past `55vh` it
   holds a scroll of its own and that is the one thing under the cursor the wheel
   should move. Everything else stays transparent to it — including the box's
   background, so a click on the echo lands on the transcript beneath, exactly as
   it would with no float at all.

   The float's opened-box rule keys on `:not(.collapsed)`
   ([styles.css:1287-1292](website/src/styles.css#L1287-L1292)) for the same
   reason the fade keys on `data-clamped`: the box already publishes both states
   on itself, so no third attribute has to travel from the component to the
   stylesheet to say what the stylesheet can see.

5. **No reserved space.** The pane gets no top padding for the float. The dock
   reserves its height because it is persistent, interactive, and output
   accumulates under it; the float is transient, non-interactive, and only ever
   shows for a box already at least `T` out of sight. Reserving would change the
   layout at the moment of appearance — appear → rows shift down → the row that
   was three lines above the fold is now one line above it → hide → rows shift
   back — which is the oscillation decision 2's no-feedback property exists to
   avoid. It would also move the geometry phase 6 asserts on. The cost is the
   occlusion below the fold while shown — up to ~93px, the three clamped lines
   plus the inset and the bubble's own chrome.

6. **No transition.** Dropping an animated opacity on the float would run on the
   same frames as the scroll that crossed the threshold, so it reads as lag while
   scrubbing and as flicker at the boundary. The clamp's fade is a static
   gradient, not an animation, which is why it can be there at all. Nothing in
   the console animates except the spinner, the one rule under
   `prefers-reduced-motion` ([styles.css:999](website/src/styles.css#L999)).

## Phases

**Phase 1 — the reading ✅** The clamp, the measured `clamped`, the fade and the
chip, in `MessageBubble` — where both readings of the box get them.

**Phase 2 — the rule ✅** `ConsolePane` multi-root, the row/text caches and the
hint, `floatEcho`/`updateFloat` and their four call sites.

**Phase 3 — the float ✅** `ConsoleTopFloat`: the positioning and the two
pointer exceptions ([styles.css:1245-1300](website/src/styles.css#L1245-L1300)).

**Phase 4 — harness coverage ✅** Phase 10.

**Phase 5 — the record ✅** This file.

## Harness coverage

Phase 10 builds its own fixture rather than seeding one: phase 7's `/clear`
empties the mock's transcript store, so a seeded user message could never reach
it, and the float's DOM is identical for replayed and live rows. It sends two
prompts it asserts most of its geometry on — the second four short lines, short
enough that joined by spaces they would fit on one line, which is what makes "the
echo kept its breaks" an assertion rather than a coincidence, and one past the
three-line cut, which is what gives it a fade and a chip to assert on — plus a
third turn whose answer the folds above need as room.

That third turn is the one fixture the change owes the mock
([mock-server.mjs:249-255](website/scripts/mock-server.mjs#L249-L255)): it is a
`long:` turn, streaming 60 lines rather than a plain turn's 26, and the prefix
exists for one assertion. A plain answer leaves its prompt *just* above the fold
at the tail — 57px in this pane, inside `T` — so the tail can only be read with
an answer that runs past a screenful, which is exactly the state the change is
about.

The phase works in *content* coordinates throughout (`rect.bottom - paneTop +
scrollTop`): the pane's `scrollTop` shares that origin, so a row sits exactly
`scrollTop - bottom` px above the fold and a target fold is a plain `scrollTop`
assignment. The read-back is compared in every geometric assertion, so a pane
that ran out of scroll cannot pass silently.

| Phase | Assertion |
| --- | --- |
| P10a | Three lines past A: the float exists and its text is A's, word for word |
| P10b | A is one line tall in a one-line box: no clamp, no fade, no chip, spanning exactly the row it copies and inside the pane, `pointer-events: none`, and the point at its own centre resolves outside it |
| P10c | Past B the echo follows — the update, not just the presence |
| P10d | B's four short lines are kept as breaks (`pre-wrap`, a line's worth past the cut), stand three lines tall, carry the `linear-gradient` fade, and offer a `more ▾` where its transcript row has one |
| P10e | The chip opens the whole input (four lines tall, no fade, `less ▴`) and closes it back to three |
| P10f | 20px past A's bottom: the echo is up — nothing has to clear the fold for it to appear |
| P10g | The boundary is the echo's own bottom edge: B's top four pixels below it and the echo stands, four pixels above it and the echo is gone. The edge is measured off the box that is up, so no line count and no inset is written into the test |
| P10h | Eight pixels into A's own box: the echo is up and covering it — the row's bottom ends no lower than the echo's, so the handover reads as one line, not two |
| P10i | `scrollTop = 0`: no float |
| P10j | Pinned to the live tail, the far end of C's `long:` answer: the echo names C — the turn being answered, with its prompt a long way off the top |
| P10k | The transcript's own rows read exactly as the echo does: A one line with nothing cut and no chip, B three lines, faded, chipped |
| P10l | A message opened in the transcript comes up already open in the echo that takes it over — the row's chip clicked, then B scrolled past the fold |
| P10m | With two boxes carrying the *same* text and only one of them open, each echo reads its own box — the case that would expose an echo keyed on what it renders rather than on which block it names |

`elementFromPoint` is the harness's reading of the pointer contract, the one
thing `getComputedStyle` cannot answer: the point at the box's centre must
resolve *outside* the float, and the point at the chip's centre *inside* it.

No existing phase reads the float's DOM, and the only pointer-opaque parts of it
are the chip and an opened box, so it cannot intercept anything else — including
`Send` and the ask rows.

## Out of scope

- Click-to-jump back to the source message, and any other hover affordance or
  dismiss control — the chip is the float's one control.
- Echoes for non-user blocks — folded cancel and compact markers stay fold lines.
- Hysteresis at the boundary (unnecessary — see decision 2).
- A `--console-inset` property for the three places 14px is now written.
- A `data-id` on `.stream-row`; the row/block index lockstep is documented in
  `refreshFloatRows` instead.
- Scrolling the opened box to its top on open; it opens where it stands.

## Risks

- **The handover assumes the echo covers the box it names.** It does, because
  the two are the same text at the same width under the same clamp and the echo
  is inset to the pane's edge — but a wider inset, or a future rule that rendered
  the echo differently from the row, would leave a sliver of the real box above
  the echo and read as two.  P10h is the assertion that notices: it measures the
  row's bottom against the echo's and fails when the box is not covered.
  An *opened* box is the one case it does not reach: past `55vh` the echo caps and
  scrolls while the row keeps growing, so the longest messages show their tail
  below the echo rather than under it.
- **The float names the previous turn for the first screenful of a reply**
  (decided with the user). It is not wrong so much as early: the reader can see
  the prompt being answered, so the echo is redundant for a few seconds and then
  becomes the right one. Suppressing it there would mean keying the rule on
  whether an answer is in flight, which is the scroller state this rule exists
  not to read.
- **The refresh triggers are an argument, not a mechanism.** They hold because
  nothing today changes rows *above* the fold without also moving the scroller.
  A future feature that grows a block in place above the fold would need its own
  refresh point; the symptom would be a float naming the wrong prompt, not a
  crash.
- **Multi-root is the first in this repo.** Attrs no longer fall through, and the
  component has no single root element for a future `ref` to grab. Vue warns if
  it ever becomes a problem.
- **Term 2 makes the float quiet in dense transcripts**, where a box is rarely
  more than an echo's height from the fold. Deliberate: in that transcript a box
  is always in sight, which is the thing the float exists to supply — and with
  the threshold gone, "in sight" now means exactly that rather than "within three
  lines".
- **The float occludes up to ~80px below the fold** while shown — three clamped
  lines and the bubble's chrome. Term 2 is exactly the trade the dock makes at
  the other edge: the echo covers output, never a user box, and what it covers is
  content that has already scrolled past being readable at the top.
- **The transcript is taller than it was.** Every user message that used to be
  one ellipsized line now stands up to three, so a long conversation scrolls
  further. That is the reading the user asked for, and it is the reason the
  transcript's copy of the box exists at all — but it is a change to *every*
  transcript, not just to long ones.
- **A chip inside a `55vh` opened float scrolls with its content** — the chip is
  the box's own trailing control, so an input long enough to overflow the opened
  box takes its `less ▴` to the bottom of that scroll. Reaching it means
  scrolling the box, not the transcript. Pinning it would need the chip to be a
  sibling of the box again, which is the duplication this reading exists to
  avoid.
- **The cut is a `max-height`, so a box shorter than three lines is not
  "clamped" at all** — which is the point, but it also means the fade and the
  chip are driven by a measurement, and a measurement taken a frame late shows
  three full lines for that frame before the fade appears. It is the same
  one-frame window the dock's `--bar-h` handshake already lives with, and it is
  why the fade is a gradient rather than a transition: a frame of un-faded text
  is invisible, a frame mid-fade is not.
- **An opened box takes the pointer**, so wheeling over it scrolls the box, not
  the console. Correct while it is open — that scroll is why the cap exists —
  but it does mean a reader who opened a long input cannot scroll the transcript
  under it without moving the cursor off first.
- **Two inputs can read the same**, which is the one thing an echo cannot tell
  apart from the outside: its text, and its rendered state, are identical either
  way. Everything that decides *which* box is echoed has to key on the block —
  `floatIds` alongside `floatTexts`, and the id assigned on every pass rather
  than only when the text changes. P10m is the assertion; it was written after
  the bug, and it fails if the id ever follows the text again.
- **`$el` is not the float's box.** The template's leading comment makes
  `ConsoleTopFloat` a fragment root, and a fragment's `$el` is its *first node* —
  in a dev build, that comment. The first cut read the box through `$el` and
  threw `box.getBoundingClientRect is not a function` on every scroll, in dev
  only. The box is exposed by name instead
  ([ConsoleTopFloat.vue:21-28](website/src/components/ConsoleTopFloat.vue#L21-L28)),
  which is what `defineExpose` is for; anyone adding a second root there should
  leave it alone.
- **The decision is two-staged and async**, so the echo's visibility lands a
  microtask after the scroll tick that changed its text — one frame where the new
  echo is in the DOM at the previous echo's height. It cannot be avoided while
  the rule reads a height, and it cannot oscillate: the height is the box's
  content, not its visibility.
- **`getComputedStyle(...).lineHeight` could report `normal`** on some engine; the
  fallback derives the line from the font size. Read once and cached — a theme
  switch swaps colours, never the type scale.

## Verification

```
cd website && npm run build && cd ..        # vue-tsc is the type contract
node website/scripts/mock-server.mjs                     # :8100
cd website && npx vite --config mock-vite.config.ts      # :5199
node website/scripts/ui-test.mjs                         # exit 1 on failure
```

The mock is **stateful** — its transcript accumulates live turns and `/clear`
empties it — so a second run against the same process dies at phase 1's
wait-for-scroll. Restart the mock between runs. (On a machine where Vite binds
IPv6 only and the bundled Playwright browser is absent, the harness needs
`BASE_URL=http://localhost:5199/ PW_CHANNEL=chrome`.)

By eye in both selectable themes (tokyo-night, github-light) on
`--editor-surface`: scroll up through a long answer and watch the echo take a
prompt over the moment the fold reaches its top edge — the box below should slide
under the echo with nothing left showing above it — then change as the next
prompt arrives; take the scroll back down slowly and watch the incoming box meet
the echo's bottom edge and end it. That handover is the whole rule now, so it is
the thing to look at, from both sides; pop a tool gate while scrolled up and
confirm no float over the dock; drag the divider to reflow the pane; and check a
conversation with no user rows at all.

Then the streaming case, which is what the float is for as much as history is:
send a prompt and watch the echo name the *previous* turn while the new one is
still on screen, then take the current one when the answer has run a screenful
past it — and, with the mock's `long:` prefix, sit at the tail of a long reply
and confirm the prompt that asked for it is the one being echoed. That last one
is the case a plain turn is too short to show.

Then the box itself, **in both places at once** — the echo at the fold and the
row it copied a screen below should read identically: a one-line input with no
fade and no chip; a five-line one standing three lines up with the third
dissolving and a `more ▾` in the corner; the chip opening it to the whole input
and closing it again; a paste long enough to pass `55vh`, which in the float
should cap and scroll inside the box rather than run past the pane, and in the
transcript should simply make its row taller; and the wheel over the closed echo
still scrolling the console underneath it. The Python web suites
(`tests/test_web_events.py`,
`test_web_ws.py`, `test_web_api.py`) are the "frames untouched" net — this change
never touches the wire.
