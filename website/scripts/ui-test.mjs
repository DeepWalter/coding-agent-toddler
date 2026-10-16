// Headless repro against the mock server:
//  phase 1 — console scrolled to the middle → a tool gate pops → the
//            console must pin to the bottom (the ask wins over atBottom)
//  phase 1.5 — the gate's rows rove with ↑/↓ (wrapping both ways), and
//            Enter resolves the gate keyboard-only
//  phase 2 — after the gate resolves, console scrolled to the middle again
//            → sending a plain message must pin to the bottom too
//  phase 3 — console chrome: no "turn finished" line after a plain turn,
//            a cancelled turn folds (and returns to idle), and the fold
//            replays after reload
//  phase 4 — context pill: disabled hover text explains headroom below 50%
//            usage; past 50% the pill turns into a live /compact button
//            (two-line hover copy) whose click lands the server's hello
//            replay + notice
//  phase 5 — thought blocks: the seeded reasoning replays collapsed and
//            expands to its quoted body — prose verbatim, its fenced and
//            inline code lifted out and highlighted; a live `think:` turn
//            adds a second block above its bubble, counting tokens while it
//            streams and reading "Thought for …" once agent_finished lands
//            — and a reload replays the same block, body byte-identical
//            but with the live-only readings gone
//  phase 7 — console header: the conversation's title sits on the same row
//            as the explorer's, and follows the live conversation through
//            /clear (fresh, untitled) and the auto-title its first turn gets
//  phase 8 — renaming it in place: hovering lights the title block and its
//            pen, the click opens a selected box sized to the text on the
//            same row, Enter commits (and keeps the transcript), Escape and
//            an empty commit both revert, and the new title survives a reload
//  phase 9 — reloaded file tabs: the tab list a reload restores closes
//            cleanly — the tab to the right takes over when the active one
//            goes, and closing the last one empties the pane (header, doc
//            and the stored active tab all go with it)
//  phase 10 — the console's top float: the last input box the fold has
//            reached is echoed over the pane's top edge and follows the
//            scroll, from the moment the box's *top* crosses the fold until
//            the next input reaches the echo's own bottom edge, and stands
//            down at the top of the transcript where there is nothing above
//            to echo.  It holds at the live tail too, which is where a long
//            stream needs it.  An echo taller than three lines is cut there
//            and faded, with a chip that opens the rest of the input and
//            closes it again
//  phase 11 — the model + effort pill: it reads "spec tier", opens onto the
//            three slots (the row the conversation was picked by is the one
//            selected, even when another row names the same model) and the
//            effort row, switches the model on a row click, cycles on the
//            effort label, takes a drag on the rail that tracks live and
//            reaches the wire exactly once, shows the tier it commits before
//            the server echoes it back, obeys the busy gate, follows
//            another tab's switch into an open menu, replays the pill and
//            the picked row from the server after a reload, stops claiming a
//            conversation whose slot was retargeted, and excludes the mode
//            popup from the keyboard
//  phase 13 — the search card: grep and glob read as a tool card too — a
//            title over the pattern the call was given and an OUT row under
//            it, with the same three-line clip and the same click-through to
//            the editor, nothing to copy, and a row that fits staying plain
//            text
//  phase 14 — the git card: a git call reads as its result alone — the title
//            and one OUT row, no row for what it was passed — openable in
//            the editor under the card's own name
//  phase 15 — the file card: a read, an edit and a write read as their titles
//            alone, naming the file rather than describing the call, and that
//            name is a control exactly where there is a file behind it — a
//            write that failed and a read of a path with nothing there both
//            read as plain text, hovering the path instead.  Which is also
//            why a write over a file that is already there opens it while its
//            gate still holds the call.  A file outside the served root opens
//            too — under its absolute path, read-only, wearing the external
//            marker — and its card says all of that before the gate on the
//            call has been answered: the start frame resolves the file, not
//            the result.  The titles name the basename and the hover the
//            whole path the server resolved, and both survive a reload
//  phase 16 — the plan: a proposed plan reads as one title-only line and
//            opens its display markdown as a rendered, read-only document
//            in the editor by itself (one tab per plan, never persisted;
//            the title click lands on that same tab), while each
//            plan_update call prints the whole step list as a todo block
//            and the document stays frozen
//  phase 17 — the gate's card: a title every call is asked about, the call's
//            own content where it has any — a shell command, whole — and the
//            model's line about the call under it.  The content is boxed and
//            capped at six lines, drawing a one-line box for a one-line
//            command and scrolling a script, with the dock reporting each
//            height it grew to.  The card also remembers which row it would
//            answer with: clicking elsewhere (the theme toggle) leaves that
//            mark where it was and gives ↑/↓ to what was clicked, and a
//            click on the card takes them back — with the dock's edge
//            saying which of the two owns them
// Prints PASS/FAIL plus diagnostics; exits 1 on any failure.
import { chromium } from 'playwright'
import WebSocket from 'ws'

const BASE = process.env.BASE_URL ?? 'http://127.0.0.1:5199/'
const WS_BASE = `${BASE.replace(/^http/, 'ws')}ws`

// PW_CHANNEL (e.g. chrome) lets the harness drive a system browser where
// the bundled Playwright build for this OS is unavailable.
const browser = await chromium.launch(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {})
const page = await browser.newPage({ viewport: { width: 1280, height: 700 } })
page.on('pageerror', (e) => console.log('[pageerror]', e.message))

await page.goto(BASE)

// Wait for the seeded console content to be tall enough to scroll.
await page.waitForFunction(() => {
  const el = document.querySelector('.console-pane')
  return el && el.scrollHeight - el.clientHeight > 800
}, { timeout: 15000 })
await page.waitForTimeout(400)

const nearBottom = () =>
  page.evaluate(() => {
    const el = document.querySelector('.console-pane')
    return el.scrollTop + el.clientHeight >= el.scrollHeight - 5
  })
const waitPinned = () =>
  page.waitForFunction(() => {
    const el = document.querySelector('.console-pane')
    return el && el.scrollTop + el.clientHeight >= el.scrollHeight - 5
  }, { timeout: 8000 })
// A second mock connection scripts session state into the page: the mock
// broadcasts session_info (and acks the control socket), so the harness can
// step context_usage_pct across the pill's clickability gate.
function sendControl(cmd, payload = {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(WS_BASE)
    const timer = setTimeout(() => {
      ws.close()
      reject(new Error(`no ack for ${cmd} within 8s`))
    }, 8000)
    ws.on('error', (e) => {
      clearTimeout(timer)
      reject(e)
    })
    ws.on('message', (raw) => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ack' && frame.cmd === cmd) {
        clearTimeout(timer)
        ws.close()
        // The whole ack, so a caller can read what the backdoor returned
        // (get_mutations) — callers that only await the round trip ignore it.
        resolve(frame)
      }
    })
    ws.on('open', () => ws.send(JSON.stringify({ cmd, ...payload })))
  })
}

const scrollMid = async (label) => {
  const at = await page.evaluate(() => {
    const el = document.querySelector('.console-pane')
    el.scrollTop = Math.round((el.scrollHeight - el.clientHeight) / 2)
    return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
  })
  await page.waitForTimeout(300)
  console.log(`${label} scrolled to middle:`, JSON.stringify(at))
  return at
}

let fails = 0

// --- Phase 1: tool gate pops while scrolled to the middle → pinned ---
const m1 = await scrollMid('phase1')
await page.fill('textarea.input-bar-textarea', 'gate: list files')
await page.click('button:has-text("Send")')
await page.waitForSelector('.pause-prompt', { timeout: 15000 })
await waitPinned()
const ok1 = await nearBottom()
console.log(`phase1 gate -> pinned to bottom ${ok1 ? 'PASS' : 'FAIL'}`)
if (!ok1) fails++

// --- Phase 1.5: gate rows rove with ↑/↓ (wrap both ways) ---
// Focus lands on Approve when the card mounts; every move is a real
// keydown on the focused row, and Enter resolves the gate keyboard-only.
const rowText = () => page.evaluate(() => document.activeElement?.textContent?.trim() ?? '')
const step = async (key, want) => {
  await page.keyboard.press(key)
  const got = await rowText()
  const ok = got === want
  console.log(`phase1.5 ${key} on gate -> ${JSON.stringify(got)} ${ok ? 'PASS' : `FAIL (want ${JSON.stringify(want)})`}`)
  if (!ok) fails++
}
{
  const got = await rowText()
  const ok = got === 'Approve'
  console.log(`phase1.5 gate focus -> ${JSON.stringify(got)} ${ok ? 'PASS' : 'FAIL (want "Approve")'}`)
  if (!ok) fails++
  await step('ArrowDown', 'Deny')
  await step('ArrowDown', 'Approve') // wraps at the end
  await step('ArrowUp', 'Deny')
  await step('ArrowUp', 'Approve') // wraps at the start
  await page.keyboard.press('Enter')
}

// --- Phase 2: resolve the gate, then a plain send pins too ---
await page.waitForSelector('.pause-prompt', { state: 'detached', timeout: 8000 })
await page.waitForSelector('button:has-text("Send")', { timeout: 8000 })
await page.waitForTimeout(300)

const m2 = await scrollMid('phase2')
const stuck = await nearBottom()
if (stuck) {
  console.log('phase2 FAIL: console would not stay scrolled away from the bottom after the gate resolved')
  fails++
} else {
  await page.fill('textarea.input-bar-textarea', 'plain message, no gate')
  await page.click('button:has-text("Send")')
  await waitPinned()
  await page.waitForTimeout(500) // stream finishes; must not yank back up
  const ok2 = await nearBottom()
  const tail = await page.evaluate(() => {
    const el = document.querySelector('.console-pane')
    return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
  })
  console.log(
    `phase2 send (mid=${m2.scrollTop}) -> pinned to ${tail.scrollTop}/${tail.scrollHeight} ${ok2 ? 'PASS' : 'FAIL'}`,
  )
  if (!ok2) fails++
}

// --- Phase 3: console chrome — no finished line, fold line on cancel ---
const paneText = () =>
  page.evaluate(() => document.querySelector('.console-pane')?.innerText ?? '')
const FOLD = '~~~The previous turn was cancelled by the user~~~'

// P3a: a completed turn leaves no "turn finished" line — that notice was
// live-only, so reloaded transcripts never had it (parity drove its removal).
{
  const text = await paneText()
  const ok = !text.includes('turn finished')
  console.log(`phase3 no "turn finished" after plain turn ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P3b: cancelling a gated turn (the ask's ✕ — the input bar is swapped for
// the ask while paused) shows the fold line and returns to idle.
await page.fill('textarea.input-bar-textarea', 'gate: list files')
await page.click('button:has-text("Send")')
await page.waitForSelector('.pause-prompt', { timeout: 15000 })
await page.click('.ask-close')
await page.waitForFunction(
  (fold) => document.querySelector('.console-pane')?.innerText.includes(fold),
  FOLD,
  { timeout: 8000 },
)
{
  // The raw marker text must not surface — it renders as the fold, not as a
  // boxed user message the human never typed.
  const text = await paneText()
  const ok = !text.includes('[The previous turn was cancelled by the user.]')
  console.log(`phase3 cancel fold hides raw marker ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
  await page.waitForSelector('.pause-prompt', { state: 'detached', timeout: 8000 })
  await page.fill('textarea.input-bar-textarea', 'x')
  const sendable = await page.locator('button:has-text("Send")').isEnabled()
  console.log(`phase3 cancel returns to idle ${sendable ? 'PASS' : 'FAIL'}`)
  if (!sendable) fails++
}

// P3c: reload — the marker replays from the mock's store as one fold line,
// still not a user box.
await page.reload()
await page.waitForFunction(
  (fold) => document.querySelector('.console-pane')?.innerText.includes(fold),
  FOLD,
  { timeout: 15000 },
)
{
  const info = await page.evaluate(() => {
    const folds = [...document.querySelectorAll('.stream-line.fold')]
    return {
      count: folds.length,
      boxed: folds.some((el) => !!el.closest('.message')),
    }
  })
  const ok = info.count === 1 && !info.boxed
  console.log(`phase3 reload replays one unboxed fold ${ok ? 'PASS' : `FAIL (${JSON.stringify(info)})`}`)
  if (!ok) fails++
}

// --- Phase 4: context pill — hover copy + clickable /compact ---
const pill = page.locator('.input-bar-context')
const pillWrap = page.locator('.input-bar-context-wrap')
const tooltip = page.locator('.context-tooltip')

// P4a: low usage (12%) → pill disabled, but hover still explains the
// headroom — a native title on a disabled button would not show in
// Chromium; the custom tooltip must.
{
  await pill.waitFor({ timeout: 15000 })
  const disabled = await pill.isDisabled()
  // textContent (not innerText): the ratio number sits absolutely centered
  // over its ring gauge, and Chromium's innerText inserts a line break
  // around out-of-flow text.  textContent holds the label's raw form —
  // the template keeps it on one physical line — so normalize and compare.
  const label = (await pill.textContent()).replace(/\s+/g, ' ').trim()
  const ok = disabled && label === 'context 12%'
  console.log(`phase4 low usage pill disabled (${JSON.stringify(label)}) ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
  await pillWrap.hover()
  await tooltip.waitFor({ state: 'visible', timeout: 5000 })
  const tip = (await tooltip.innerText()).trim()
  const ok2 = tip === '68% of context remaining\nuntil auto-compact'
  console.log(`phase4 low usage hover copy ${JSON.stringify(tip)} ${ok2 ? 'PASS' : 'FAIL'}`)
  if (!ok2) fails++
  await page.mouse.move(0, 0)
}

// P4b: usage past half the window → the pill turns into a live button and
// the hover copy gains the second-line click hint.
{
  await sendControl('set_context', { pct: 62 })
  await page.waitForFunction(() => {
    const el = document.querySelector('.input-bar-context')
    return el && !el.disabled && el.textContent?.includes('context 62%')
  }, { timeout: 8000 })
  const enabled = await pill.isEnabled()
  console.log(`phase4 past-half pill clickable ${enabled ? 'PASS' : 'FAIL'}`)
  if (!enabled) fails++
  await pillWrap.hover()
  await tooltip.waitFor({ state: 'visible', timeout: 5000 })
  const lines = (await tooltip.innerText())
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
  const ok =
    lines.length === 3 &&
    lines[0] === '18% of context remaining' &&
    lines[1] === 'until auto-compact' &&
    lines[2] === 'Click to compact now'
  console.log(`phase4 past-half hover copy ${JSON.stringify(lines)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
  const sizes = await page.evaluate(() => {
    const hint = document.querySelector('.context-tip-hint')
    const copy = document.querySelector('.context-tip-copy')
    if (!hint || !copy) return null
    return {
      hint: parseFloat(getComputedStyle(hint).fontSize),
      copy: parseFloat(getComputedStyle(copy).fontSize),
    }
  })
  const okSize = sizes !== null && sizes.hint < sizes.copy
  console.log(`phase4 hint smaller than headroom ${JSON.stringify(sizes)} ${okSize ? 'PASS' : 'FAIL'}`)
  if (!okSize) fails++
  await page.mouse.move(0, 0)
}

// P4c: clicking the pill sends /compact → the mock answers with the real
// server's slash contract: a hello replay at the new usage + a notice.
// The pill lands back below the gate and its copy drops the click hint.
{
  await pill.click()
  await page.waitForFunction(() => {
    const el = document.querySelector('.input-bar-context')
    const pane = document.querySelector('.console-pane')
    return (
      el?.textContent?.includes('context 34%') &&
      pane?.innerText.includes('Compacted context: 14 → 2 messages (62% → 34% of context window).')
    )
  }, { timeout: 8000 })
  const state = await page.evaluate(() => ({
    label: document.querySelector('.input-bar-context')?.textContent?.trim(),
    disabled: document.querySelector('.input-bar-context')?.disabled,
  }))
  const ok = state.label === 'context 34%' && state.disabled === true
  console.log(`phase4 compact click → ${JSON.stringify(state)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
  await pillWrap.hover()
  await tooltip.waitFor({ state: 'visible', timeout: 5000 })
  const tip = (await tooltip.innerText()).trim()
  const ok2 = tip === '46% of context remaining\nuntil auto-compact'
  console.log(`phase4 post-compact hover copy ${JSON.stringify(tip)} ${ok2 ? 'PASS' : 'FAIL'}`)
  if (!ok2) fails++
}

// P4d: the click must not pin the tooltip.  A compact that lands above the
// 50% gate leaves the pill enabled and focused — its mouse-click focus used
// to keep :focus-within true, so the tooltip stayed on the page after the
// pointer left.  Mouse focus must not qualify; only hover does.
{
  await sendControl('set_context', { pct: 62, compact_to: 55 })
  await page.waitForFunction(() => {
    const el = document.querySelector('.input-bar-context')
    return el && !el.disabled && el.textContent?.includes('context 62%')
  }, { timeout: 8000 })
  await pillWrap.hover()
  await tooltip.waitFor({ state: 'visible', timeout: 5000 })
  await pill.click()
  await page.waitForFunction(() => {
    const el = document.querySelector('.input-bar-context')
    return el && !el.disabled && el.textContent?.includes('context 55%')
  }, { timeout: 8000 })
  await page.mouse.move(0, 0)
  let stuck = true
  try {
    await tooltip.waitFor({ state: 'hidden', timeout: 1500 })
    stuck = false
  } catch {
    // still visible — timeout is the failure signal
  }
  console.log(`phase4 click-then-leave hides tooltip ${stuck ? 'FAIL (tooltip still visible)' : 'PASS'}`)
  if (stuck) fails++
}

// --- Phase 5: thought blocks — collapsed, verbatim, replayable ---
// The mock seeds one reasoning-bearing transcript entry (SEED_REASONING in
// mock-server.mjs) and streams a second on a `think:` turn.
const SEED_REASONING =
  'seed thought: check `scrollable` before answering\n' +
  '```python\n' +
  'def fits(pane):\n' +
  '    return pane.scrollHeight > pane.clientHeight\n' +
  '```\n' +
  'second line — prose stays verbatim, fences do not'

// What the block renders that seed as: the prose lines survive character
// for character, the code is lifted into its own element, and the fence
// lines and backticks are consumed rather than shown.
const SEED_PROSE = [
  'seed thought: check scrollable before answering',
  'second line — prose stays verbatim, fences do not',
]
const SEED_CODE = 'def fits(pane):\n    return pane.scrollHeight > pane.clientHeight'
const SEED_CHIP = 'scrollable'

// Label shapes — the counts are the point, so these match a pattern rather
// than pinning the mock's fragment count and stream duration (which lands in
// the sub-second branch on a fast run, "1 second" on a slow one).
const THINKING_LIVE = /^Thinking… · \d+(\.\d+[KM])? tokens?$/
const THOUGHT_DONE =
  /^Thought for (\d+ seconds?|\d+ minutes?( \d+ seconds?)?|less than a second)$/

// Document positions of the newest thought block and newest assistant
// bubble — the block must sit above the bubble its answer lands in.
// Compared in document order, not by pane-child index: every block now
// renders inside a .stream-row (the status gutter), so the pane's children
// are rows, not blocks.
const thoughtAboveBubble = () =>
  page.evaluate(() => {
    const thoughts = document.querySelectorAll('.console-pane .thinking')
    const bubbles = document.querySelectorAll('.console-pane .message.assistant')
    const thought = thoughts[thoughts.length - 1]
    const bubble = bubbles[bubbles.length - 1]
    const ok =
      !!thought &&
      !!bubble &&
      (thought.compareDocumentPosition(bubble) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
    return { thoughts: thoughts.length, bubbles: bubbles.length, ok }
  })

// P5a: the replayed block starts collapsed — "Thought" + ▸, no quote, and no
// count: a block that never streamed in this page has no reading to show.
{
  const block = page.locator('.thinking').first()
  await block.waitFor({ timeout: 15000 })
  const info = {
    label: (await block.locator('.thinking-label').textContent()).trim(),
    chevron: (await block.locator('.thinking-chevron').textContent()).trim(),
    quotes: await block.locator('.thinking-quote').count(),
    details: await block.locator('.thinking-detail').count(),
  }
  const ok =
    info.label === 'Thought' && info.chevron === '▸' && info.quotes === 0 && info.details === 0
  console.log(`phase5 seeded block collapsed ${JSON.stringify(info)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P5b: click expands to the reasoning inside a quoted block (a rule down its
// left edge, not the old bordered card): the prose verbatim, the code it
// carries lifted out and highlighted.  A second click collapses it.
{
  const block = page.locator('.thinking').first()
  await block.locator('.thinking-toggle').click()
  const revealed = await block.locator('.thinking-quote').evaluate((el) => ({
    text: el.textContent ?? '',
    bar: getComputedStyle(el).borderLeftWidth,
    code: el.querySelector('code.thinking-code')?.textContent ?? null,
    chip: el.querySelector('code.thinking-inline')?.textContent ?? null,
    tokens: el.querySelectorAll('code.thinking-code span').length,
  }))
  const ok =
    revealed.bar === '2px' &&
    SEED_PROSE.every((line) => revealed.text.includes(line)) &&
    // The syntax the block renders away must not leak into the text.
    !revealed.text.includes('`') &&
    revealed.code === SEED_CODE &&
    revealed.chip === SEED_CHIP &&
    // Coloured, not just boxed: the grammar produced token spans.
    revealed.tokens > 0
  console.log(
    `phase5 expand shows prose verbatim and code lifted out ${ok ? 'PASS' : `FAIL (${JSON.stringify(revealed)})`}`,
  )
  if (!ok) fails++
  await block.locator('.thinking-toggle').click()
  const collapsed = (await block.locator('.thinking-quote').count()) === 0
  console.log(`phase5 second click collapses ${collapsed ? 'PASS' : 'FAIL'}`)
  if (!collapsed) fails++
}

// P5c: a live think turn adds one more block, counting tokens while the
// reasoning streams (the mock holds before the answer for this window).  It
// stays collapsed — revealing reasoning is always the user's click, live or
// replayed alike.
// Counts are relative: the mock's turnLog grows across runs, so asserting
// a literal block count would only hold for a freshly started mock.
const thoughtCount = () => page.locator('.thinking').count()
const thoughtsBefore = await thoughtCount()
{
  await page.fill('textarea.input-bar-textarea', 'think: about it')
  await page.click('button:has-text("Send")')
  let sawOpen = true
  try {
    await page.waitForFunction(
      // The phrase and the count are separate spans (the count rides at a
      // lighter weight), so the label a user reads is both of them.
      ([want, pattern]) => {
        const blocks = [...document.querySelectorAll('.thinking')]
        const last = blocks[blocks.length - 1]
        const label = `${last?.querySelector('.thinking-label')?.textContent ?? ''} ${
          last?.querySelector('.thinking-detail')?.textContent ?? ''
        }`.trim()
        return (
          blocks.length === want &&
          new RegExp(pattern).test(label) &&
          last?.querySelector('.thinking-quote') === null
        )
      },
      [thoughtsBefore + 1, THINKING_LIVE.source],
      { timeout: 15000 },
    )
  } catch {
    sawOpen = false
  }
  console.log(`phase5 live block counts tokens while streaming ${sawOpen ? 'PASS' : 'FAIL'}`)
  if (!sawOpen) fails++
}

// P5d: after agent_finished it reports its duration and sits above its bubble.
{
  let closed = true
  try {
    await page.waitForFunction(
      ([want, pattern]) => {
        const blocks = [...document.querySelectorAll('.thinking')]
        const last = blocks[blocks.length - 1]
        const label = `${last?.querySelector('.thinking-label')?.textContent ?? ''} ${
          last?.querySelector('.thinking-detail')?.textContent ?? ''
        }`.trim()
        return (
          blocks.length === want &&
          new RegExp(pattern).test(label) &&
          last?.querySelector('.thinking-detail') === null
        )
      },
      [thoughtsBefore + 1, THOUGHT_DONE.source],
      { timeout: 15000 },
    )
  } catch {
    closed = false
  }
  console.log(`phase5 live block reports its duration ${closed ? 'PASS' : 'FAIL'}`)
  if (!closed) fails++

  const order = await thoughtAboveBubble()
  console.log(`phase5 live block above its bubble ${JSON.stringify(order)} ${order.ok ? 'PASS' : 'FAIL'}`)
  if (!order.ok) fails++
}

// P5e: reload replays the same blocks; the live block's body is byte-identical
// to the replayed one, still above its bubble — but the duration, being a
// reading of the live stream, is not replayed with it.
{
  const live = page.locator('.thinking').last()
  await live.locator('.thinking-toggle').click()
  const liveBody = await live.locator('.thinking-quote').textContent()
  const okLive = liveBody.startsWith('live thought:')
  console.log(`phase5 live block body is the streamed reasoning ${okLive ? 'PASS' : `FAIL (${JSON.stringify(liveBody)})`}`)
  if (!okLive) fails++

  await page.reload()
  let replayed = null
  let replayedLabel = null
  let count = -1
  try {
    await page.waitForFunction(
      (want) => document.querySelectorAll('.thinking').length === want,
      thoughtsBefore + 1,
      { timeout: 15000 },
    )
    const block = page.locator('.thinking').last()
    replayedLabel = (await block.locator('.thinking-label').textContent()).trim()
    await block.locator('.thinking-toggle').click()
    replayed = await block.locator('.thinking-quote').textContent()
  } catch {
    // replayed stays null — the mismatch below reports it
  }
  count = await thoughtCount()
  const ok = count === thoughtsBefore + 1 && replayed === liveBody
  console.log(
    `phase5 reload replays the block byte-identically (count=${count}) ${ok ? 'PASS' : `FAIL (${JSON.stringify(replayed)})`}`,
  )
  if (!ok) fails++

  const bare = replayedLabel === 'Thought'
  console.log(
    `phase5 reload drops the live-only duration ${bare ? 'PASS' : `FAIL (${JSON.stringify(replayedLabel)})`}`,
  )
  if (!bare) fails++

  const order = await thoughtAboveBubble()
  console.log(`phase5 replayed block above its bubble ${JSON.stringify(order)} ${order.ok ? 'PASS' : 'FAIL'}`)
  if (!order.ok) fails++
}

// --- Phase 6: the status gutter — every output block indents behind one
// status mark; user input is the one row that stays flush left, unmarked ---
const GLYPH = { running: '', ok: '✓', error: '✗', cancelled: '·' }

// P6a: a plain turn settles the whole transcript, so the gutter contract —
// indent, one outdented mark per output block, none on user input — can be
// read in one pass.  Widths are measured, never hard-coded: the pane's own
// 14px inset and the gutter are compared as edges, so retuning either cannot
// silently fail this.
await page.fill('textarea.input-bar-textarea', 'plain message, no gate')
await page.click('button:has-text("Send")')
await waitPinned()
await page.waitForTimeout(700)
{
  const info = await page.evaluate(() => {
    const pane = document.querySelector('.console-pane')
    const rows = [...pane.querySelectorAll(':scope > .stream-row')]
    const mark = (r) => r.querySelector(':scope > .status-mark')
    const block = (r) => r.querySelector(':scope > :not(.status-mark)')
    const box = (el) => el.getBoundingClientRect()
    const users = rows.filter((r) => r.dataset.kind === 'user')
    // The mark-less kinds stay out: user input, and a todo list — its own
    // boxes carry the statuses (phase 16 asserts that).
    const output = rows.filter(
      (r) => r.dataset.kind !== 'user' && r.dataset.kind !== 'todo',
    )
    return {
      rows: rows.length,
      output: output.length,
      // the doc's contract: exactly one mark per marked output row, none on
      // a user row, and every mark left of the block it belongs to
      stray: output.filter((r) => r.querySelectorAll(':scope > .status-mark').length !== 1).length,
      userMarked: users.filter((r) => mark(r)).length,
      outdented: output.filter((r) => box(mark(r)).right <= box(block(r)).left + 1).length,
      // the indent: the user's text sits at the pane's content edge, the
      // output blocks one gutter to its right
      contentLeft: Math.round(box(pane).left) + 14,
      userTextLeft: Math.round(box(block(users[0])).left),
      outputTextLeft: Math.round(box(block(output[0])).left),
    }
  })
  const ok =
    info.output > 0 &&
    info.stray === 0 &&
    info.userMarked === 0 &&
    info.outdented === info.output &&
    info.userTextLeft === info.contentLeft &&
    info.outputTextLeft > info.userTextLeft
  console.log(
    `phase6 output blocks indented behind one outdented mark, user flush left ${JSON.stringify(info)} ${ok ? 'PASS' : 'FAIL'}`,
  )
  if (!ok) fails++
}

// P6a2: the settled transcript reads as one vocabulary — spinner iff
// running, the right glyph otherwise, and nothing still turning.
{
  const marks = await page.evaluate(() =>
    [...document.querySelectorAll('.console-pane .status-mark')].map((el) => ({
      state: ['running', 'ok', 'error', 'cancelled'].find((s) => el.classList.contains(s)) ?? '(none)',
      kind: el.closest('.stream-row')?.dataset.kind ?? '?',
      text: el.textContent.trim(),
      spinner: el.querySelector('.spinner') !== null,
    })),
  )
  const uniform = marks.every(
    (m) => m.spinner === (m.state === 'running') && m.text === (GLYPH[m.state] ?? null),
  )
  const live = marks.filter((m) => m.state === 'running').length
  const ok = uniform && live === 0
  console.log(
    `phase6 settled transcript carries one vocabulary (${marks.length} marks, ${live} live) ${ok ? 'PASS' : `FAIL (${JSON.stringify(marks.slice(0, 4))})`}`,
  )
  if (!ok) fails++
}

// P6b: the gate is a wait state — the tool's mark is the only live one, and
// the answer that asked for it has already settled.  Before the reducer
// closed the answer block on tool_call_start, this row span forever.  The
// card is a wait state too: it holds what the call was told to do, and no
// result row until there is a result to read.
await page.fill('textarea.input-bar-textarea', 'gate: list files')
await page.click('button:has-text("Send")')
await page.waitForSelector('.pause-prompt', { timeout: 15000 })
{
  const live = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('.console-pane > .stream-row')]
    const has = (r, sel) => r?.querySelector(`:scope > ${sel}`) !== null
    const last = rows[rows.length - 1]
    return {
      live: rows.filter((r) => has(r, '.status-mark.running')).length,
      lastKind: last?.dataset.kind ?? '?',
      toolLive: has(last, '.status-mark.running'),
      answerSettled: has(rows[rows.length - 2], '.status-mark.ok'),
      // the mark lives in the gutter: nothing renders one inside a card
      inline: document.querySelectorAll('.tool-card .status-mark').length,
      // the command is on screen, "waiting for result…" is not: no result
      // row until one lands
      inRows: last?.querySelectorAll('.tool-card-row[data-row="in"]').length ?? -1,
      outRows: last?.querySelectorAll('.tool-card-row[data-row="out"]').length ?? -1,
    }
  })
  const ok =
    live.live === 1 &&
    live.lastKind === 'tool' &&
    live.toolLive &&
    live.answerSettled &&
    live.inline === 0 &&
    live.inRows === 1 &&
    live.outRows === 0
  console.log(`phase6 gate holds one live mark, its answer settled ${JSON.stringify(live)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}
await page.click('.ask-close')
await page.waitForSelector('.pause-prompt', { state: 'detached', timeout: 8000 })

// P6c: a call the cancelled turn abandoned settles as ✗ — not a spinner
// that never stops, and not the neutral · the turn-cancel fold line wears:
// nothing came back, so the call failed with the turn.
{
  const mark = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('.stream-row[data-kind="tool"]')]
    const row = rows[rows.length - 1]
    const el = row?.querySelector(':scope > .status-mark')
    return { state: el?.className ?? null, glyph: el?.textContent.trim() ?? null }
  })
  const ok = !!mark.state?.includes('error') && mark.glyph === '✗'
  console.log(`phase6 cancelled call reads ✗ ${JSON.stringify(mark)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P6d: a failed call also reads ✗ — the two unhappy endings share the
// glyph, and the fold line above says which one it was.
await page.fill('textarea.input-bar-textarea', 'fail: run it')
await page.click('button:has-text("Send")')
{
  let seen = null
  try {
    await page.waitForFunction(
      () => {
        const rows = [...document.querySelectorAll('.stream-row[data-kind="tool"]')]
        const row = rows[rows.length - 1]
        return row?.querySelector(':scope > .status-mark.error')?.textContent.trim() === '✗'
      },
      { timeout: 15000 },
    )
    seen = '✗'
  } catch {
    // Left null — the mismatch below reports it.
  }
  console.log(`phase6 failed call reads ✗ ${JSON.stringify(seen)} ${seen === '✗' ? 'PASS' : 'FAIL'}`)
  if (seen !== '✗') fails++
}

// --- Phase 7: the console header — the pane's title line, on the same row
// as the explorer's, following the live conversation ---

// Longer than the server's 80-char title limit, so the auto-title's
// truncation is exercised rather than assumed.
const FIRST_TURN_MESSAGE =
  'auto-title this conversation from its first user input, which the server truncates at eighty chars'
const AUTO_TITLE = FIRST_TURN_MESSAGE.slice(0, 80)
const RENAMED_TITLE = 'renamed by the test'

// Reads the two header rows and the console's title in one pass.  The
// boxes are compared as measured geometry, so retuning the padding cannot
// silently drift the rows apart.
const headerInfo = () =>
  page.evaluate(() => {
    const rect = (sel) => {
      const el = document.querySelector(sel)
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { top: Math.round(r.top), height: Math.round(r.height) }
    }
    const title = document.querySelector('.console-title')
    const explorerTitle = document.querySelector('.explorer-title')
    return {
      console: rect('.console-header'),
      explorer: rect('.explorer-header'),
      text: title?.textContent.trim() ?? null,
      weight: title ? getComputedStyle(title).fontWeight : null,
      explorerWeight: explorerTitle ? getComputedStyle(explorerTitle).fontWeight : null,
    }
  })

// P7a: one row across the split, naming the conversation the hello payload
// carried — and weighting the title like the explorer's next to it.
{
  const info = await headerInfo()
  const ok =
    info.console !== null &&
    info.explorer !== null &&
    info.console.top === info.explorer.top &&
    info.console.height === info.explorer.height &&
    info.text === 'seed conversation' &&
    info.weight === info.explorerWeight
  console.log(`phase7 console header names the conversation on the explorer's row ${JSON.stringify(info)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P7b: /clear starts a fresh conversation — the header drops to its empty
// state live, off the hello replay the server broadcasts, no reload.
await page.fill('textarea.input-bar-textarea', '/clear')
await page.click('button:has-text("Send")')
{
  let text = null
  try {
    await page.waitForFunction(
      () => document.querySelector('.console-title')?.textContent.trim() === 'New conversation',
      { timeout: 8000 },
    )
    text = 'New conversation'
  } catch {
    // Left null — the mismatch below reports it.
  }
  console.log(`phase7 /clear leaves the header on a fresh conversation ${JSON.stringify(text)} ${text === 'New conversation' ? 'PASS' : 'FAIL'}`)
  if (text !== 'New conversation') fails++
}

// P7c: the first turn titles the conversation (server-side, truncated at
// 80 chars) and the header follows — still on the one row the explorer's
// header holds, however long the title runs.
{
  await page.fill('textarea.input-bar-textarea', FIRST_TURN_MESSAGE)
  await page.click('button:has-text("Send")')
  let info = null
  try {
    await page.waitForFunction(
      (want) => document.querySelector('.console-title')?.textContent.trim() === want,
      AUTO_TITLE,
      { timeout: 8000 },
    )
    info = await headerInfo()
  } catch {
    info = await headerInfo()
  }
  const ok =
    info.text === AUTO_TITLE &&
    info.console.height === info.explorer.height &&
    info.console.top === info.explorer.top
  console.log(`phase7 first turn titles the header (${info.text?.length} chars) on the same row ${JSON.stringify(info.console)}/${JSON.stringify(info.explorer)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// --- Phase 8: renaming the title in place ---
const titleText = () =>
  page.evaluate(() => document.querySelector('.console-title')?.textContent.trim() ?? null)
// The title box, or null when the header is showing the title itself.
// `sizer` is the invisible span the box takes its width from, so
// input === sizer is what "the box fits the title" means.
const boxInfo = () =>
  page.evaluate(() => {
    const input = document.querySelector('.console-title-input')
    if (!input) return null
    const header = document.querySelector('.console-header')
    const explorer = document.querySelector('.explorer-header')
    const sizer = document.querySelector('.console-title-sizer')
    const hcs = getComputedStyle(header)
    return {
      value: input.value,
      focused: document.activeElement === input,
      selected: input.selectionStart === 0 && input.selectionEnd === input.value.length,
      width: Math.round(input.getBoundingClientRect().width),
      sizer: Math.round(sizer.getBoundingClientRect().width),
      // the room the box is clamped to: the header's content box, not its
      // border box — the 10px side paddings are not available to it
      avail: Math.round(
        header.clientWidth - parseFloat(hcs.paddingLeft) - parseFloat(hcs.paddingRight),
      ),
      height: Math.round(header.getBoundingClientRect().height),
      explorerHeight: Math.round(explorer.getBoundingClientRect().height),
      top: Math.round(header.getBoundingClientRect().top),
      explorerTop: Math.round(explorer.getBoundingClientRect().top),
    }
  })

// What the title offers before it is clicked: the hover block and the pen.
// `block` is the button's own width — it hugs the title, it is not the row.
const titleAffordance = () =>
  page.evaluate(() => {
    const btn = document.querySelector('.console-title')
    const pen = document.querySelector('.console-title-pen')
    return {
      pen: Number(getComputedStyle(pen).opacity),
      background: getComputedStyle(btn).backgroundColor,
      block: Math.round(btn.getBoundingClientRect().width),
      row: Math.round(document.querySelector('.console-header').getBoundingClientRect().width),
    }
  })

// Let the auto-titling turn settle first: the row count below is the
// baseline for "renaming did not replay the transcript", and a turn still
// streaming would add to it on its own.
await page.waitForSelector('button:has-text("Send")', { timeout: 15000 })

// P8a0: hovering the title lights the block and fades the pen in — at
// rest neither shows, so the affordance is what says "editable".
await page.mouse.move(10, 400) // park the pointer clear of the header
{
  const resting = await titleAffordance()
  await page.hover('.console-title')
  const hovered = await titleAffordance()
  const ok =
    resting.pen === 0 &&
    resting.background === 'rgba(0, 0, 0, 0)' &&
    hovered.pen === 1 &&
    hovered.background !== resting.background
  console.log(`phase8 hover lights the title block and its pen ${JSON.stringify({ resting, hovered })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P8a: clicking the title opens a box holding it, focused and selected, on
// the same row — and no wider than the text in it, which is the whole point
// of replacing the full-width box.
{
  const rows = await page.evaluate(
    () => document.querySelectorAll('.console-pane .stream-row').length,
  )
  await page.click('.console-title')
  const info = await boxInfo()
  const ok =
    info !== null &&
    info.value === AUTO_TITLE &&
    info.focused &&
    info.selected &&
    info.height === info.explorerHeight &&
    info.top === info.explorerTop &&
    // The box is the text's width, capped by the row: this 80-char title is
    // wider than the pane, so here it clamps — the short-title rename below
    // is where hugging is read on its own.
    Math.abs(info.width - Math.min(info.sizer, info.avail)) <= 1
  console.log(`phase8 click opens a focused, selected title box on the same row, sized to the text ${JSON.stringify(info)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++

  // P8b: Enter commits — the header takes the new title, and the transcript
  // it labeled is still there (a rename is metadata, not a replay).
  await page.fill('.console-title-input', RENAMED_TITLE)
  await page.keyboard.press('Enter')
  let text = null
  try {
    await page.waitForFunction(
      (want) => document.querySelector('.console-title')?.textContent.trim() === want,
      RENAMED_TITLE,
      { timeout: 8000 },
    )
    text = RENAMED_TITLE
  } catch {
    text = await titleText()
  }
  const after = await page.evaluate(
    () => document.querySelectorAll('.console-pane .stream-row').length,
  )
  // With a short title the resting block is unmistakably not the row —
  // which is what the hover highlight draws around.
  const block = await titleAffordance()
  const ok2 =
    text === RENAMED_TITLE &&
    after === rows &&
    block.block < block.row / 2
  console.log(`phase8 Enter commits the title and keeps the transcript (${rows} rows) ${JSON.stringify(text)} ${ok2 ? 'PASS' : 'FAIL'}`)
  if (!ok2) fails++
}

// P8c: Escape discards the edit — the stored title comes back.
await page.click('.console-title')
await page.fill('.console-title-input', 'discard me')
await page.keyboard.press('Escape')
await page.waitForTimeout(200)
{
  const text = await titleText()
  const box = await boxInfo()
  const ok = text === RENAMED_TITLE && box === null
  console.log(`phase8 Escape discards the edit ${JSON.stringify(text)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P8d: an empty box is not a title — it reverts instead of clearing, since
// a cleared title would be re-derived from the next turn's first words.
await page.click('.console-title')
await page.fill('.console-title-input', '   ')
await page.keyboard.press('Enter')
await page.waitForTimeout(200)
{
  const text = await titleText()
  const ok = text === RENAMED_TITLE
  console.log(`phase8 an empty commit reverts ${JSON.stringify(text)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P8e: the rename reached the server, not just the header — a reload
// replays the stored title through hello, and the discarded edits are gone.
await page.reload()
await page.waitForSelector('.console-pane')
{
  let text = null
  try {
    await page.waitForFunction(
      (want) => document.querySelector('.console-title')?.textContent.trim() === want,
      RENAMED_TITLE,
      { timeout: 8000 },
    )
    text = RENAMED_TITLE
  } catch {
    text = await titleText()
  }
  console.log(`phase8 the rename survives a reload ${JSON.stringify(text)} ${text === RENAMED_TITLE ? 'PASS' : 'FAIL'}`)
  if (text !== RENAMED_TITLE) fails++
}

// --- Phase 9: the tab list a reload restores closes cleanly ---

// The regression: open a file, reload, close its tab — the strip emptied
// while the pane kept the file, header and all.  The restored active tab was
// a second literal for the path rather than the open list's own entry, so
// closeTab's match missed it and activeTab was never cleared.  The restore
// reads localStorage (root must match the session cwd, /tmp), which is
// exactly the state a reload rebuilds from.
const editorState = () =>
  page.evaluate(() => {
    const active = document.querySelector('.editor-tab.active .editor-tab-name')
    const stored = JSON.parse(localStorage.getItem('tod.tabs') ?? 'null')
    return {
      tabs: [...document.querySelectorAll('.editor-tab-name')].map((el) => el.textContent.trim()),
      active: active?.textContent.trim() ?? null,
      header: document.querySelector('.editor-path')?.textContent.trim() ?? null,
      doc: document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
      stored,
    }
  })

await page.evaluate(() => {
  localStorage.setItem('tod.tabs', JSON.stringify({
    open: ['mock-a.md', 'mock-b.md'],
    active: 'mock-b.md',
    root: '/tmp',
  }))
})
await page.reload()
await page.waitForSelector('.editor-tab')
{
  // Both tabs come back with their own content — the restore is what the
  // close below is matched against, so assert it rather than assume it.
  let st = { tabs: [], active: null, doc: '', header: null, stored: null }
  try {
    await page.waitForFunction(
      () => document.querySelector('.editor-cm .cm-content')?.textContent.includes('mock B'),
      { timeout: 8000 },
    )
    st = await editorState()
  } catch {
    st = await editorState()
  }
  const ok = st.tabs.join(',') === 'mock-a.md,mock-b.md' &&
    st.active === 'mock-b.md' && st.doc.includes('mock B')
  console.log(`phase9 a reload restores the tabs and the active file's content ${JSON.stringify(st.tabs)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P9b: closing the active tab hands the pane to its neighbour — with the
// neighbour's doc, not the closed file's.
await page.click('.editor-tab.active .editor-tab-close')
await page.waitForTimeout(400)
{
  const st = await editorState()
  const ok = st.tabs.join(',') === 'mock-a.md' &&
    st.active === 'mock-a.md' && st.header === 'mock-a.md' &&
    st.doc.includes('mock A') && !st.doc.includes('mock B')
  console.log(`phase9 closing the active tab shows its neighbour ${JSON.stringify(st.doc.slice(0, 20))} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P9c: the last tab leaves nothing behind — the pane returns to its empty
// state and the stored active tab is cleared, so the next reload does not
// resurrect a tab that is no longer open.
await page.click('.editor-tab .editor-tab-close')
await page.waitForTimeout(400)
{
  const st = await editorState()
  const ok = st.tabs.length === 0 && st.active === null &&
    st.header === 'no file selected' && st.doc.trim() === '' &&
    st.stored?.open.length === 0 && st.stored?.active === null
  console.log(`phase9 closing the last tab empties the pane ${JSON.stringify(st)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// --- Phase 10: the console's top float — the last input box the fold has
// scrolled past, echoed over the pane's top edge.  A plain turn streams 26
// lines, which is the room the fold needs to sit between two boxes. ---
const FLOAT_A = 'float A — the input box the fold scrolls past first'
// Four short lines.  Short is the point: joined by spaces the text would fit
// on one line, so the echo showing more than one can only be the breaks being
// kept — and four of them is one past the three-line clamp.
const FLOAT_B = ['fix login bug', 'on expiry', 'not on retry', 'in prod'].join('\n')
// A third turn: room below B for the folds above, and — being a `long:` turn,
// so its answer runs well past a screenful — the state the float exists for
// while a reply streams, with its own prompt a long way off the top.
const FLOAT_C = 'long: float C — the turn whose answer runs past a screenful'

// The transcript is rebuilt by this phase rather than seeded: phase 7's
// /clear emptied the mock's store, and the float's DOM is the same for
// replayed and live rows.
for (const text of [FLOAT_A, FLOAT_B, FLOAT_C]) {
  await page.waitForSelector('button:has-text("Send")', { timeout: 8000 })
  await page.fill('textarea.input-bar-textarea', text)
  await page.click('button:has-text("Send")')
  await waitPinned()
  await page.waitForTimeout(400)
}

// One transcript line (13px/1.55), and the phase's threshold: the same three
// lines ConsolePane's FLOAT_LINES counts.
const LINE = await page.evaluate(() => {
  const el = document.querySelector('.console-pane .message-content')
  return Number.parseFloat(getComputedStyle(el).lineHeight)
})
const T = 3 * LINE

// The last `count` user rows in *content* coordinates: the pane's scrollTop
// shares that origin, so a row sits `scrollTop - bottom` px above the fold
// and a target fold is a plain scrollTop.  Positions in content space do not
// move when the pane scrolls, so these stay good for the whole phase.
const lastInputs = (count) =>
  page.evaluate((n) => {
    const pane = document.querySelector('.console-pane')
    const paneTop = pane.getBoundingClientRect().top
    const scrollTop = pane.scrollTop
    return [...document.querySelectorAll('.console-pane > .stream-row[data-kind="user"]')]
      .slice(-n)
      .map((el) => {
        const box = el.getBoundingClientRect()
        const top = box.top - paneTop + scrollTop
        return { top, height: box.height, bottom: box.bottom - paneTop + scrollTop }
      })
  }, count)

// Put the fold `gap` px above a row's bottom edge — a gap smaller than the
// row's height lands inside it, and a negative one leaves it hanging into
// view.  The read-back is what the assertions compare, so a pane that ran out
// of scroll cannot pass silently.
const foldAt = async (row, gap) => {
  const at = await page.evaluate(
    ({ bottom, by }) => {
      const pane = document.querySelector('.console-pane')
      pane.scrollTop = bottom + by
      return {
        fold: Math.round(pane.scrollTop),
        want: Math.round(bottom + by),
        max: Math.round(pane.scrollHeight - pane.clientHeight),
      }
    },
    { bottom: row.bottom, by: gap },
  )
  await page.waitForTimeout(150)
  return at
}

// Everything the float's contract is read from.  `hit` is what the point at
// the box's own centre resolves to and `chipHit` the point at the chip's:
// null when nothing is there, false when something underneath is, true when
// the float itself is.
const floatState = () =>
  page.evaluate(() => {
    const el = document.querySelector('.console-top-float')
    // The box is in the document whenever there is anything above the fold to
    // echo, shown or not — the pane measures it either way.  Not shown is what
    // visibility says.
    if (!el || getComputedStyle(el).visibility === 'hidden') return null
    const box = el.getBoundingClientRect()
    const paneEl = document.querySelector('.console-pane')
    const pane = paneEl.getBoundingClientRect()
    // The row the echo copies, for the alignment the box owes it — the float's
    // own inset is a look, the row's edges are what it lines up with.
    const row = paneEl.querySelector(':scope > .stream-row[data-kind="user"]')?.getBoundingClientRect()
    const content = el.querySelector('.message-content')
    const chip = el.querySelector('.message-toggle')
    const chipBox = chip?.getBoundingClientRect()
    const text = [...(content?.childNodes ?? [])]
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent)
      .join('')
    const style = getComputedStyle(content)
    const at = (x, y) => {
      const found = document.elementFromPoint(x, y)
      return found ? el.contains(found) : null
    }
    return {
      text,
      top: Math.round(box.top),
      bottom: Math.round(box.bottom),
      left: Math.round(box.left),
      right: Math.round(box.right),
      paneTop: Math.round(pane.top),
      paneLeft: Math.round(pane.left),
      paneRight: Math.round(pane.right),
      paneBottom: Math.round(pane.bottom),
      rowLeft: row ? Math.round(row.left) : null,
      rowRight: row ? Math.round(row.right) : null,
      height: Math.round(box.height),
      line: Number.parseFloat(style.lineHeight),
      pointerEvents: getComputedStyle(el).pointerEvents,
      buttons: el.querySelectorAll('button').length,
      whiteSpace: style.whiteSpace,
      // The box is boxes not lines: scrollHeight carries the whole text, so
      // content taller than the box is what the clamp cut away.
      overflowY: content ? content.scrollHeight - content.clientHeight : 0,
      // The box's own three readings, plus the fade it paints with — the
      // gradient is the whole visual difference between a cut box and one
      // that ends where its text ends.
      clamped: content ? content.hasAttribute('data-clamped') : false,
      expanded: content ? !content.classList.contains('collapsed') : false,
      chip: chip !== null,
      fade: content ? getComputedStyle(content, '::before').backgroundImage : '',
      chipText: chip?.textContent?.trim() ?? null,
      hit: at(box.left + box.width / 2, box.top + box.height / 2),
      chipHit: chipBox ? at(chipBox.left + chipBox.width / 2, chipBox.top + chipBox.height / 2) : null,
    }
  })

const [A, B] = await lastInputs(3)

// P10a: three lines past A, inside B's answer — the float echoes A, word for
// word (the copy is the string, not a truncation of it).
const foldA = await foldAt(A, T + 12)
const shownA = await floatState()
{
  const ok = shownA !== null && shownA.text === FLOAT_A && foldA.fold === foldA.want
  console.log(
    `phase10 a box past the fold is echoed with its own text ${JSON.stringify({ fold: foldA, text: shownA?.text })} ${ok ? 'PASS' : 'FAIL'}`,
  )
  if (!ok) fails++
}

// P10b: A is one line, so the box is one line — no clamp, no fade, no chip —
// spanning exactly the row it copies and sitting inside the pane, out of the
// pointer's way (so the wheel still reaches the scroller and a click lands on
// what is underneath).  The 18 is the bubble's own 8px padding and 1px border.
{
  const f = shownA
  const ok =
    f !== null &&
    Math.abs(f.left - f.rowLeft) <= 1 &&
    Math.abs(f.right - f.rowRight) <= 1 &&
    f.top >= f.paneTop &&
    f.top + f.height <= f.paneBottom &&
    Math.abs(f.height - (f.line + 18)) <= 1 &&
    f.overflowY === 0 &&
    !f.clamped &&
    !f.chip &&
    f.buttons === 0 &&
    f.pointerEvents === 'none' &&
    // Never the float itself — the point under it belongs to the transcript.
    f.hit !== true
  console.log(`phase10 a one-line input is echoed whole, with nothing to open ${JSON.stringify(f)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P10c: past B, the echo follows.
await foldAt(B, T + 12)
const shownB = await floatState()
{
  const ok = shownB !== null && shownB.text === FLOAT_B
  console.log(`phase10 the echo follows the last box above the fold ${JSON.stringify(shownB?.text?.slice(0, 24))} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}
// P10d: the echo keeps the four lines B was typed as and stands three of them
// up, the fourth going under a fade — with a chip, because there is more of B
// than the box shows.  Its transcript row is the same text with the same
// `more ▾`, so the chip is the transcript's control, read at the fold.
{
  const f = shownB
  const rowToggles = await page.evaluate(
    () => document.querySelectorAll('.console-pane .stream-row[data-kind="user"] .message-toggle').length,
  )
  const ok =
    f !== null &&
    f.whiteSpace === 'pre-wrap' &&
    Math.abs(f.height - (3 * f.line + 18)) <= 1 &&
    // A whole line's worth of text is past the cut — which only the breaks
    // being kept can produce, since B on one line would not fill three.
    f.overflowY >= f.line &&
    f.clamped &&
    !f.expanded &&
    f.chip &&
    f.fade.includes('linear-gradient') &&
    f.chipText === 'more ▾' &&
    // The chip is the one thing here that takes the pointer.
    f.chipHit === true &&
    rowToggles > 0
  console.log(`phase10 a multi-line input keeps its breaks, fades at three lines, and offers the rest ${JSON.stringify({ whiteSpace: f?.whiteSpace, height: f?.height, line: f?.line, past: f?.overflowY, clamped: f?.clamped, fade: f?.fade?.slice(0, 21), chip: f?.chipText, chipHit: f?.chipHit, transcriptToggles: rowToggles })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P10e: the chip opens the whole input, and closes it again — and the fade
// goes with the clamp, since a box showing everything has nothing to point at.
await page.click('.console-top-float .message-toggle')
await page.waitForTimeout(200)
{
  const open = await floatState()
  const okOpen =
    open !== null &&
    open.expanded &&
    !open.clamped &&
    open.chipText === 'less ▴' &&
    open.height > 3 * open.line + 18 &&
    open.fade === 'none'
  console.log(`phase10 the chip opens the whole input ${JSON.stringify({ expanded: open?.expanded, height: open?.height, chip: open?.chipText, fade: open?.fade })} ${okOpen ? 'PASS' : 'FAIL'}`)
  if (!okOpen) fails++

  await page.click('.console-top-float .message-toggle')
  await page.waitForTimeout(200)
  const shut = await floatState()
  const okShut =
    shut !== null &&
    !shut.expanded &&
    shut.clamped &&
    Math.abs(shut.height - (3 * shut.line + 18)) <= 1 &&
    shut.chipText === 'more ▾'
  console.log(`phase10 the chip closes it back to three lines ${JSON.stringify({ expanded: shut?.expanded, height: shut?.height, chip: shut?.chipText })} ${okShut ? 'PASS' : 'FAIL'}`)
  if (!okShut) fails++
}

// P10f: a box the fold has just cleared is echoed at once.  There is no grace
// band — 20px of it is enough, where the three-line threshold this replaced
// would have withheld the echo for another 40.
const foldJust = await foldAt(A, 20)
{
  const f = await floatState()
  const ok =
    f !== null && f.text === FLOAT_A && foldJust.fold === foldJust.want
  console.log(`phase10 a box 20px past the fold is echoed at once ${JSON.stringify({ fold: foldJust, text: f?.text?.slice(0, 16) })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P10g: and what ends it is the echo's own room, not a number of lines.  The
// boundary is its bottom edge, measured off the box that is up: B's top four
// pixels below that edge and the echo stands, four pixels above it and the
// echo is gone.
const edge = (shown) => (shown ? shown.bottom - shown.paneTop : 0)
{
  const up = await floatState()
  const px = edge(up)
  await foldAt(B, -(B.height + px + 4))
  const under = await floatState()
  await foldAt(B, -(B.height + px - 4))
  const over = await floatState()
  const ok =
    up !== null && px > 0 &&
    under !== null && under.text === FLOAT_A &&
    over === null
  console.log(`phase10 the echo ends where the next input reaches its bottom edge ${JSON.stringify({ edge: px, below: under?.text?.slice(0, 16), above: over })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P10h: eight pixels into A's own box — the fold has only just reached its
// top, and the echo is already up naming it.  That is the handover: the echo is
// the same text at the same width under the same clamp, so it covers what is
// still showing rather than sitting beside it.  The row's bottom ends up no
// lower than the echo's, which is what "covers" means here.
await foldAt(A, 8 - A.height)
{
  const f = await floatState()
  const uncovered = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('.console-pane > .stream-row[data-kind="user"]')]
    const box = document.querySelector('.console-top-float')?.getBoundingClientRect()
    if (!box) return null
    return Math.round(rows[rows.length - 3].getBoundingClientRect().bottom - box.bottom)
  })
  const ok = f !== null && f.text === FLOAT_A && uncovered !== null && uncovered <= 1
  console.log(`phase10 the echo takes over the moment the box's top reaches the fold ${JSON.stringify({ text: f?.text?.slice(0, 16), uncovered })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P10i: at the very top there is nothing above the fold to echo.
await page.evaluate(() => {
  document.querySelector('.console-pane').scrollTop = 0
})
await page.waitForTimeout(150)
{
  const f = await floatState()
  const ok = f === null
  console.log(`phase10 nothing above the fold at the top of the transcript ${JSON.stringify(f)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P10j: pinned to the live tail — the far end of C's answer — the echo names
// C, the turn being answered.  This is the reading the float exists for while
// a reply streams: the prompt that asked for it is a screen or more off the
// top, and the answer on screen cannot say which turn it belongs to.
await page.evaluate(() => {
  const el = document.querySelector('.console-pane')
  el.scrollTop = el.scrollHeight
})
await page.waitForTimeout(150)
{
  const f = await floatState()
  const ok = f !== null && f.text === FLOAT_C
  console.log(`phase10 at the live tail the echo names the turn being answered ${JSON.stringify(f?.text?.slice(0, 24))} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P10k: the transcript's own box — the one the float is a copy of — is read
// the same way, which is the point of the echo: one box, one reading, whether
// it sits in a row or over the top edge.  A (one line) is cut nowhere and
// offers nothing; B (four short lines) is the same three lines and a chip the
// float showed, in the row this time.
const rowState = (fromEnd) =>
  page.evaluate((n) => {
    const rows = [...document.querySelectorAll('.console-pane > .stream-row[data-kind="user"]')]
    const content = rows[rows.length - n]?.querySelector('.message-content')
    if (!content) return null
    return {
      height: Math.round(content.getBoundingClientRect().height),
      line: Number.parseFloat(getComputedStyle(content).lineHeight),
      whiteSpace: getComputedStyle(content).whiteSpace,
      past: content.scrollHeight - content.clientHeight,
      collapsed: content.classList.contains('collapsed'),
      clamped: content.hasAttribute('data-clamped'),
      chip: content.querySelector('.message-toggle')?.textContent?.trim() ?? null,
      fade: getComputedStyle(content, '::before').backgroundImage,
    }
  }, fromEnd)

{
  const short = await rowState(3)
  const long = await rowState(2)
  const ok =
    short !== null &&
    short.whiteSpace === 'pre-wrap' &&
    short.collapsed &&
    !short.clamped &&
    short.chip === null &&
    long !== null &&
    long.whiteSpace === 'pre-wrap' &&
    long.collapsed &&
    long.clamped &&
    Math.abs(long.height - (3 * long.line + 18)) <= 1 &&
    long.past >= long.line &&
    long.chip === 'more ▾' &&
    long.fade.includes('linear-gradient')
  console.log(`phase10 the transcript's own boxes read exactly as the echo does ${JSON.stringify({ short: { height: short?.height, clamped: short?.clamped, chip: short?.chip }, long: { height: long?.height, line: long?.line, past: long?.past, clamped: long?.clamped, chip: long?.chip } })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P10l: a box is read twice — its row in the transcript, and the echo over the
// fold — and it is one message, so opening it in either place opens it in both.
// This is the direction the reader meets first: they open a message in the
// transcript, scroll on, and the echo that takes it over comes up already open.
{
  // B's row in view with the echo still naming A above it, so the chip about to
  // be clicked is the transcript's own and nothing is covering it.
  const [, parked] = await lastInputs(3)
  await foldAt(parked, -(parked.height + 60))
  await page
    .locator('.console-pane .stream-row[data-kind="user"]', { hasText: 'not on retry' })
    .locator('.message-toggle')
    .click()
  const opened = await rowState(2)
  // Then B's top crosses the fold: the echo takes it over, and it is open.
  const [, moved] = await lastInputs(3)
  await foldAt(moved, 20 - moved.height)
  const f = await floatState()
  const ok =
    opened !== null && opened.collapsed === false &&
    f !== null && f.expanded && f.chipText === 'less ▴' &&
    f.height > 3 * f.line + 18
  console.log(`phase10 opening a message in the transcript opens the echo that takes it over ${JSON.stringify({ rowCollapsed: opened?.collapsed, floatExpanded: f?.expanded, height: f?.height, line: f?.line })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P10m: two inputs can read the same, and the echo's state follows the box it
// *names*, not the one it used to.  The twin is the same text as B and has
// never been touched, so the two differ in exactly one way — which block is
// behind them — and nothing in the echo's content can tell them apart.  What
// each echo should read is taken from its own row, so the assertion holds
// whichever way the two stand.
await page.waitForSelector('button:has-text("Send")', { timeout: 8000 })
await page.fill('textarea.input-bar-textarea', FLOAT_B)
await page.click('button:has-text("Send")')
await waitPinned()
await page.waitForTimeout(400)
{
  const rows4 = await lastInputs(4) // A, B, C, and B again
  const openAt = (n) =>
    page.evaluate(
      (i) =>
        ![...document.querySelectorAll('.console-pane > .stream-row[data-kind="user"]')][i]
          .querySelector('.message-content')
          .classList.contains('collapsed'),
      n,
    )
  const open = [await openAt(1), await openAt(3)]
  await foldAt(rows4[1], 6 - rows4[1].height)
  const overB = await floatState()
  await foldAt(rows4[3], 6 - rows4[3].height)
  const overTwin = await floatState()
  const ok =
    open[0] !== open[1] &&
    overB !== null && overB.expanded === open[0] &&
    overTwin !== null && overTwin.expanded === open[1]
  console.log(`phase10 an echo over a second box with the same text reads that box's state ${JSON.stringify({ open, overB: overB && { expanded: overB.expanded, h: overB.height }, overTwin: overTwin && { expanded: overTwin.expanded, h: overTwin.height } })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// --- Phase 11: the model + effort pill ---
//
// The pill shows the live spec and tier together ("mock-model high"), and
// its menu is four rows: the three slots, then the effort control.  A slot
// click switches the model; the effort row cycles on its label and takes a
// drag on its rail.  The mock ships two slots on the live spec and one on a
// `[1m]` spec, so the several-rows-selected case and the window column are
// both live.

// P11a: the closed pill reads "spec tier" as one line, and it is the tier
// the server holds (the mock's session starts at "high").
const pillText = async () =>
  (await page.locator('.model-toggle').textContent())?.replace(/\s+/g, ' ').trim()
const effortRow = async () => ({
  value: (await page.locator('.effort-value').textContent())?.trim(),
  now: await page.locator('.effort-rail').getAttribute('aria-valuenow'),
  text: await page.locator('.effort-rail').getAttribute('aria-valuetext'),
  checked: await page.locator('.model-menu-item[aria-checked="true"]').count(),
  open: await page.locator('.model-menu').count(),
})
const waitPill = (want) =>
  page.waitForFunction(
    (w) =>
      document.querySelector('.model-toggle')?.textContent?.replace(/\s+/g, ' ').trim() === w,
    want,
    { timeout: 8000 },
  )
{
  await waitPill('mock-model high')
  const ok = (await pillText()) === 'mock-model high'
  console.log(`phase11 the pill reads "spec tier" (${JSON.stringify(await pillText())}) ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11b: four rows.  The three slots carry the spec they name and that
// spec's window; the fourth is the effort control, parked at the stop the
// stored tier collapses onto.  Exactly ONE slot row reads selected — the
// one the conversation was picked by — even though `flash` names the same
// model as `default`: the highlight follows the slot, not the spec.
// The checked rows' slot names.  Read the name element, not the row's text:
// the row is a flex line, so its textContent runs the three parts together.
const selectedRow = async () =>
  (await page.locator('.model-menu-item[aria-checked="true"] .model-menu-name').allTextContents())
    .map((t) => t.trim())
await page.locator('.model-toggle').click()
await page.waitForSelector('.model-menu')
{
  const rows = await page.locator('.model-menu-item').allTextContents()
  const state = await effortRow()
  const picked = await selectedRow()
  const ok =
    rows.length === 3 &&
    rows[0].includes('default') && rows[0].includes('mock-model') && rows[0].includes('200.0K') &&
    rows[1].includes('pro') && rows[1].includes('mock-model[1m]') && rows[1].includes('1.0M') &&
    rows[2].includes('flash') &&
    picked.join() === 'default' &&
    state.value === 'high' && state.now === '4'
  console.log(`phase11 menu shows three slots and the effort row, one of them selected ${JSON.stringify({ rows, picked, ...state })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11c: the row the conversation holds is highlighted even when another row
// names the same model — and picking that other row moves the highlight
// while changing nothing else about the model.  This is the case the spec
// alone cannot express, and the reason the slot is stored at all.
{
  const before = await selectedRow()
  await page.locator('.model-menu-item', { hasText: 'flash' }).click()
  await page.waitForFunction(
    () =>
      document.querySelector('.model-menu-item[aria-checked="true"]')
        ?.textContent?.trim().startsWith('flash') === true,
    null,
    { timeout: 8000 },
  )
  const picked = await selectedRow()
  const text = await pillText()
  const state = await effortRow()
  const ok =
    before.join() === 'default' && picked.join() === 'flash' &&
    text === 'mock-model high' && state.open === 1
  console.log(`phase11 a pick between two slots naming the SAME model moves the highlight only ${JSON.stringify({ before, picked, text, open: state.open })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11d: a pick that DOES change the model — pro names a `[1m]` spec — moves
// both the spec in the pill and the highlighted row.  The menu stays open
// (a picker you compare within).
{
  await page.locator('.model-menu-item', { hasText: 'pro' }).click()
  await waitPill('mock-model[1m] high')
  const state = await effortRow()
  const picked = await selectedRow()
  const ok = picked.join() === 'pro' && state.open === 1
  console.log(`phase11 a pick that changes the spec moves the pill and the highlight ${JSON.stringify({ picked, open: state.open })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11e: the label click cycles one tier at a time — high → xhigh — and the
// menu stays open, so a trip across the scale is one open, not one per step.
{
  await page.locator('.effort-label').click()
  await page.waitForFunction(
    () => document.querySelector('.effort-value')?.textContent?.trim() === 'xhigh',
    null,
    { timeout: 8000 },
  )
  const state = await effortRow()
  const ok = state.value === 'xhigh' && state.now === '5' && state.text === 'xhigh' && state.open === 1
  console.log(`phase11 the effort label cycles one tier to xhigh ${JSON.stringify(state)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11f: the drag.  The knob follows the pointer, the row names the stop it
// would commit — and the wire is untouched until release, however many
// stops the pointer crosses.  Then exactly one set_effort lands, on the
// stop nearest where the pointer stopped.
{
  const rail = await page.locator('.effort-rail').boundingBox()
  const cy = rail.y + rail.height / 2
  const before = (await sendControl('get_mutations')).mutations.length
  // Inset by a couple of px at both ends: the end stops sit ON the rail's
  // edges, and a press exactly on the boundary lands outside the box (no
  // capture, no drag).  The inset is far inside the ~1/7-width rounding
  // tolerance, so each point still resolves to the stop it names.
  const LAST = 7 // the eighth stop; EFFORT_STOPS has no client-side export here
  const atStop = (i) => rail.x + 2 + ((rail.width - 4) * i) / LAST

  await page.mouse.move(atStop(LAST), cy)
  await page.mouse.down()
  await page.mouse.move(atStop(2), cy, { steps: 3 })
  const mid = await effortRow()
  const midMutations = (await sendControl('get_mutations')).mutations.length
  await page.mouse.move(atStop(0), cy, { steps: 5 })
  await page.mouse.up()
  await page.waitForFunction(
    () => document.querySelector('.effort-value')?.textContent?.trim() === 'none',
    null,
    { timeout: 8000 },
  )
  const after = (await sendControl('get_mutations')).mutations
  const landed = after[after.length - 1]
  const ok =
    mid.value === 'low' && mid.now === '2' &&
    midMutations === before &&
    after.length === before + 1 &&
    landed.cmd === 'set_effort' && landed.tier === 'none'
  console.log(`phase11 a drag tracks live and commits once ${JSON.stringify({ mid, midMutations, before, after: after.length, landed })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11g: the arrows clamp at the ends — a slider is a scale, and stepping
// off the top onto `none` would turn thinking off behind the user's back.
{
  await page.locator('.effort-rail').press('ArrowRight') // none → minimal
  await page.waitForFunction(
    () => document.querySelector('.effort-value')?.textContent?.trim() === 'minimal',
    null,
    { timeout: 8000 },
  )
  await page.locator('.effort-rail').press('ArrowLeft') // back to none
  await page.waitForFunction(
    () => document.querySelector('.effort-value')?.textContent?.trim() === 'none',
    null,
    { timeout: 8000 },
  )
  await page.locator('.effort-rail').press('ArrowLeft') // still none — clamped
  await page.waitForTimeout(200)
  const state = await effortRow()
  const ok = state.value === 'none' && state.now === '0'
  console.log(`phase11 the rail's arrows step and clamp ${JSON.stringify(state)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11h: the tier the pointer picks is on screen the moment it is released,
// not one round trip later.  The rail renders its in-flight stop only while
// the pointer is down, so a readout that waits for the server's echo falls
// back to the tier the server is still holding on release — the old one —
// and blinks the picked one back when the echo finally lands.  The mock
// parks that echo, so while it is held the tier on screen can only be the
// client's own; releasing it then confirms rather than moves.
{
  await sendControl('hold_effort_echo', { hold: true })
  const rail = await page.locator('.effort-rail').boundingBox()
  const cy = rail.y + rail.height / 2
  const before = (await sendControl('get_mutations')).mutations.length
  const LAST = 7 // the eighth stop; EFFORT_STOPS has no client-side export here
  const atStop = (i) => rail.x + 2 + ((rail.width - 4) * i) / LAST

  // A click, not a drag: down and up land on the same stop, so the commit
  // is the whole gesture — the case where the flash is the only feedback.
  await page.mouse.move(atStop(6), cy)
  await page.mouse.down()
  await page.mouse.up()
  const state = await effortRow()
  const pill = await pillText()
  const after = (await sendControl('get_mutations')).mutations
  const landed = after[after.length - 1]
  const ok =
    state.value === 'max' && state.now === '6' && state.text === 'max' &&
    pill === 'mock-model[1m] max' &&
    after.length === before + 1 &&
    landed.cmd === 'set_effort' && landed.tier === 'max'
  console.log(`phase11 a click shows the picked tier before the server's echo ${JSON.stringify({ state, pill, landed })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++

  // And the echo agrees with it — the optimistic value is not a second,
  // different answer that the server has to walk back.
  await sendControl('hold_effort_echo', { hold: false })
  await page.waitForTimeout(250)
  const settled = await effortRow()
  const ok2 = settled.value === 'max' && settled.now === '6' && settled.text === 'max'
  console.log(`phase11 the parked echo confirms the optimistic tier ${JSON.stringify(settled)} ${ok2 ? 'PASS' : 'FAIL'}`)
  if (!ok2) fails++
}

// P11i: the busy gate.  A turn starting anywhere takes the picker's gate
// away, so the open menu closes rather than offering switches the server
// would reject — and it comes back when the turn ends.
{
  await sendControl('set_busy', { busy: true })
  await page.waitForFunction(
    () => document.querySelector('.model-toggle')?.disabled === true,
    null,
    { timeout: 8000 },
  )
  const closed = (await page.locator('.model-menu').count()) === 0
  await page.locator('.model-toggle').click({ force: true })
  await page.waitForTimeout(150)
  const stillClosed = (await page.locator('.model-menu').count()) === 0
  await sendControl('set_busy', { busy: false })
  await page.waitForFunction(
    () => document.querySelector('.model-toggle')?.disabled === false,
    null,
    { timeout: 8000 },
  )
  const ok = closed && stillClosed
  console.log(`phase11 a turn closes the menu and disables the pill ${JSON.stringify({ closed, stillClosed })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11j: another tab's switch.  The broadcast updates the pill AND the menu
// it has open — a session_info is not a reason to close a popup.
{
  await page.locator('.model-toggle').click()
  await page.waitForSelector('.model-menu')
  await sendControl('set_effort', { tier: 'xhigh' })
  await page.waitForFunction(
    () => document.querySelector('.effort-value')?.textContent?.trim() === 'xhigh',
    null,
    { timeout: 8000 },
  )
  const state = await effortRow()
  const ok = state.open === 1 && state.now === '5' && state.text === 'xhigh'
  console.log(`phase11 another tab's tier moves the open menu ${JSON.stringify(state)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11k: this tab's own two switches ride the same session_info, so the pill
// follows the server rather than a local guess — including across a reload,
// which replays them from the mock's session.
{
  await sendControl('set_model', { slot: 'flash' })
  await waitPill('mock-model xhigh')
  await page.keyboard.press('Escape')
  await page.waitForFunction(
    () => document.querySelector('.model-menu') === null,
    null,
    { timeout: 8000 },
  )
  await page.reload()
  await page.waitForSelector('.model-toggle', { timeout: 15000 })
  await page.locator('.model-toggle').click()
  await page.waitForSelector('.model-menu')
  const picked = await selectedRow()
  const text = await pillText()
  const ok = text === 'mock-model xhigh' && picked.join() === 'flash'
  console.log(`phase11 the pill and the picked row replay from the server after a reload ${JSON.stringify({ text, picked })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P11l: a slot retargeted underneath a live conversation.  The conversation
// still runs the model it was created with — the spec is what runs — so the
// stored slot no longer names it, and highlighting that row would claim a
// model this conversation has never asked for.  The highlight falls back to
// whatever row DOES name the running model.
{
  await sendControl('retarget_slot', { slot: 'flash', spec: 'other-model' })
  await page.waitForFunction(
    () =>
      document.querySelector('.model-menu-item[aria-checked="true"]')
        ?.textContent?.trim().startsWith('default') === true,
    null,
    { timeout: 8000 },
  )
  const picked = await selectedRow()
  const text = await pillText()
  const ok = picked.join() === 'default' && text === 'mock-model xhigh'
  console.log(`phase11 a retargeted slot stops claiming the conversation ${JSON.stringify({ picked, text })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
  await page.keyboard.press('Escape')
  await page.waitForFunction(
    () => document.querySelector('.model-menu') === null,
    null,
    { timeout: 8000 },
  )
}

// P11m: a tier the scale does not hold.  `TODDLER_EFFORT_LEVEL` is never
// validated, so an unrecognized one reaches a conversation row and the
// picker has to show it: the knob parks at `max` — where the endpoint's own
// coercion sends the request — while the row still names the tier the
// server holds.
{
  await page.locator('.model-toggle').click()
  await page.waitForSelector('.model-menu')
  await sendControl('force_effort', { tier: 'ludicrous' })
  await page.waitForFunction(
    () => document.querySelector('.effort-value')?.textContent?.trim() === 'ludicrous',
    null,
    { timeout: 8000 },
  )
  const state = await effortRow()
  const ok = state.now === '6' && state.text === 'ludicrous' && state.open === 1
  console.log(`phase11 an unknown tier parks at max and keeps its name ${JSON.stringify(state)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
  await page.keyboard.press('Escape')
  await page.waitForFunction(
    () => document.querySelector('.model-menu') === null,
    null,
    { timeout: 8000 },
  )
}

// P11n: the two popups exclude each other on the keyboard path as well.
// Tabbing out of the open mode menu reaches the model pill; Enter there
// must leave exactly one menu on screen.  Two independent `open` refs would
// leave both up — a few dozen pixels apart and overlapping — because the
// pointer path (one trigger is always outside the other) has no keyboard
// equivalent.
{
  await page.locator('.mode-toggle').focus()
  await page.keyboard.press('Enter')
  const opened = await page.locator('.mode-menu').count()
  for (let i = 0; i < 4; i++) await page.keyboard.press('Tab')
  const landed = await page.evaluate(() => document.activeElement?.className)
  await page.keyboard.press('Enter')
  await page.waitForTimeout(200)
  const mode = await page.locator('.mode-menu').count()
  const model = await page.locator('.model-menu').count()
  const ok = opened === 1 && landed === 'model-toggle' && mode === 0 && model === 1
  console.log(`phase11 the two popups exclude each other from the keyboard too ${JSON.stringify({ opened, landed, mode, model })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
  await page.keyboard.press('Escape')
}

// --- Phase 12: a shell call reads as a Bash card ---

// The card shows the command and its output outright: a title carrying the
// call's own description, then IN and OUT rows clipped to three lines.  A
// clipped row is the control — it copies (IN) and opens its whole content in
// the editor pane, which is the first thing the editor shows that no path
// backs onto.
await page.fill('textarea.input-bar-textarea', 'shell: count the python files')
await page.click('button:has-text("Send")')
await page.waitForSelector('.tool-card', { timeout: 8000 })
let shellCard = null
{
  await page.waitForTimeout(300)
  shellCard = await page.locator('.tool-call').last().evaluate((el) => {
    const card = el.querySelector('.tool-card')
    const row = (side) => {
      const box = card.querySelector(`.tool-card-row[data-row="${side}"]`)
      const label = box.querySelector('.tool-card-section-title')
      const text = box.querySelector('.tool-card-text')
      const line = parseFloat(getComputedStyle(text).lineHeight)
      return {
        label: label.textContent.trim(),
        // The label column is sized to its own label, so a label has to sit
        // wholly left of the text it labels rather than run into it.
        labelFits: label.getBoundingClientRect().right <= text.getBoundingClientRect().left,
        clamped: box.hasAttribute('data-clamped'),
        role: box.getAttribute('role'),
        lines: Math.round(text.clientHeight / line),
        text: text.textContent,
        faded: getComputedStyle(box, '::before').backgroundImage.includes('linear-gradient'),
      }
    }
    return {
      name: el.querySelector('.tool-call-name')?.textContent.trim() ?? null,
      desc: el.querySelector('.tool-call-desc')?.textContent.trim() ?? null,
      nameSize: parseFloat(getComputedStyle(el.querySelector('.tool-call-name')).fontSize),
      bodySize: parseFloat(getComputedStyle(document.body).fontSize),
      // The title is read as ordinary output — the box holds only the rows,
      // and the heading is above it.
      titleInCard: card.querySelectorAll('.tool-call-heading').length,
      copy: card.querySelectorAll('.tool-card-copy').length,
      in: row('in'),
      out: row('out'),
    }
  })
  const ok =
    shellCard.name === 'Bash' &&
    shellCard.desc === 'count the Python files per directory' &&
    // The heading is a step above the body type — the shared rule's whole
    // point, and the reason it lives in shared/ for the next tool to use.
    shellCard.nameSize > shellCard.bodySize &&
    shellCard.titleInCard === 0 &&
    shellCard.copy === 1 &&
    // The pair a command reads as, and both labels clear of their text.
    shellCard.in.label === 'IN' &&
    shellCard.out.label === 'OUT' &&
    shellCard.in.labelFits &&
    shellCard.out.labelFits &&
    // Three lines each, and both cut — the fade and the control follow the
    // clip rather than the text.
    shellCard.in.clamped &&
    shellCard.in.lines === 3 &&
    shellCard.in.faded &&
    shellCard.in.role === 'button' &&
    shellCard.out.clamped &&
    shellCard.out.lines === 3
  console.log(`phase12 a shell call reads as a Bash card ${JSON.stringify({ name: shellCard.name, desc: shellCard.desc, nameSize: shellCard.nameSize, bodySize: shellCard.bodySize, titleInCard: shellCard.titleInCard, legacyHeader: shellCard.legacyHeader, copy: shellCard.copy, inLabel: shellCard.in.label, outLabel: shellCard.out.label, inLines: shellCard.in.lines, outLines: shellCard.out.lines })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}
// P12b: the gutter mark lines up with the title, not with the box — the
// shell row's first line is the title, so it takes the default inset where
// every other tool's mark sits inside its card's header.
{
  const marks = await page.locator('.stream-row[data-kind="tool"][data-tool="shell"]').last().evaluate((row) => {
    const mark = row.querySelector('.status-mark')
    const title = row.querySelector('.tool-call-heading')
    const box = row.querySelector('.tool-card')
    const mid = (el) => el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2
    return {
      markMid: mark ? mid(mark) : null,
      titleMid: title ? mid(title) : null,
      boxTop: box ? box.getBoundingClientRect().top : null,
      titleTop: title ? title.getBoundingClientRect().top : null,
      tool: row.getAttribute('data-tool'),
    }
  })
  const ok =
    marks.tool === 'shell' &&
    marks.markMid !== null &&
    // Centred on the title's line, which itself starts at the row's top —
    // and so, unlike the in-card case, above the box.
    Math.abs(marks.markMid - marks.titleMid) <= 3 &&
    marks.titleTop < marks.boxTop
  console.log(`phase12 the gutter mark aligns with the title, above the box ${JSON.stringify({ markMid: marks.markMid, titleMid: marks.titleMid, titleTop: marks.titleTop, boxTop: marks.boxTop })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P12c: the copy button writes the command, and copying is not opening —
// the click must not reach the row's own handler.
{
  await page.evaluate(() => {
    window.__copied = null
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: (text) => {
          window.__copied = text
          return Promise.resolve()
        },
      },
    })
  })
  await page.locator('.tool-card').last().locator('.tool-card-copy').click()
  await page.waitForTimeout(150)
  const state = await page.evaluate(() => ({
    copied: window.__copied,
    tabs: document.querySelectorAll('.editor-tab').length,
    icon: document.querySelector('.tool-card-copy')?.classList.contains('copied') ?? false,
  }))
  const ok =
    state.copied === shellCard.in.text && state.tabs === 0 && state.icon === true
  console.log(`phase12 the IN row copies the command without opening it ${JSON.stringify({ chars: state.copied?.length, tabs: state.tabs, icon: state.icon })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}
// P12d: a clipped row opens its whole content in the editor, read-only — and
// clicking it again focuses that tab rather than opening a second one.
{
  const title = 'Bash — count the Python files per directory'
  const outTitle = 'Bash output — count the Python files per directory'
  await page.locator('.tool-card').last().locator('.tool-card-row[data-row="in"]').click()
  await page.waitForTimeout(300)
  const opened = await page.evaluate(() => ({
    tabs: [...document.querySelectorAll('.editor-tab-name')].map((el) => el.textContent.trim()),
    active: document.querySelector('.editor-tab.active .editor-tab-name')?.textContent.trim() ?? null,
    header: document.querySelector('.editor-path')?.textContent.trim() ?? null,
    doc: document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
    status: document.querySelector('.editor-status')?.textContent.trim() ?? null,
  }))
  // The doc is the whole command, not the three lines the card showed.
  const ok =
    opened.tabs.length === 1 &&
    opened.active === title &&
    opened.header === title &&
    opened.doc.includes('done') &&
    opened.status?.startsWith('read-only') === true
  console.log(`phase12 the IN row opens the whole command in the editor ${JSON.stringify({ tabs: opened.tabs, active: opened.active, status: opened.status, docLines: opened.doc.split('\n').length })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++

  // Read-only: typing into the buffer must not change it.
  const before = opened.doc
  await page.locator('.editor-cm .cm-content').click()
  await page.keyboard.type('zzz')
  await page.waitForTimeout(150)
  const after = await page.evaluate(() => document.querySelector('.editor-cm .cm-content')?.textContent ?? '')
  const okRo = after === before
  console.log(`phase12 the text tab refuses edits ${JSON.stringify({ changed: after !== before })} ${okRo ? 'PASS' : 'FAIL'}`)
  if (!okRo) fails++

  // The OUT row is its own tab; the command's tab stays one tab.
  await page.locator('.tool-card').last().locator('.tool-card-row[data-row="out"]').click()
  await page.waitForTimeout(300)
  const out = await page.evaluate(() => ({
    tabs: [...document.querySelectorAll('.editor-tab-name')].map((el) => el.textContent.trim()),
    active: document.querySelector('.editor-tab.active .editor-tab-name')?.textContent.trim() ?? null,
    doc: document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
  }))
  const okOut =
    out.tabs.length === 2 &&
    // The output tab names its side — two tabs called the same thing would
    // be indistinguishable in the strip.
    out.tabs.includes(outTitle) &&
    out.active === outTitle &&
    out.doc.includes('line 10 of the listing')
  console.log(`phase12 the OUT row opens the output as its own tab ${JSON.stringify({ tabs: out.tabs, docLines: out.doc.split('\n').length })} ${okOut ? 'PASS' : 'FAIL'}`)
  if (!okOut) fails++

  await page.locator('.tool-card').last().locator('.tool-card-row[data-row="in"]').click()
  await page.waitForTimeout(250)
  const again = await page.evaluate(() => ({
    tabs: document.querySelectorAll('.editor-tab').length,
    active: document.querySelector('.editor-tab.active .editor-tab-name')?.textContent.trim() ?? null,
  }))
  const okAgain = again.tabs === 2 && again.active === title
  console.log(`phase12 a second click focuses the tab it already opened ${JSON.stringify(again)} ${okAgain ? 'PASS' : 'FAIL'}`)
  if (!okAgain) fails++
}
// P12e: a call that fits its three lines is plain text — no fade, no cursor,
// no tab.  The failure fixture is also the card's error reading.
{
  await page.fill('textarea.input-bar-textarea', 'fail: run it')
  await page.click('button:has-text("Send")')
  await page.waitForTimeout(400)
  const short = await page.locator('.tool-card').last().evaluate((el) => {
    const box = el.querySelector('.tool-card-row[data-row="in"]')
    return {
      clamped: box.hasAttribute('data-clamped'),
      role: box.getAttribute('role'),
      error: el.classList.contains('error'),
      outError: el.querySelector('.tool-card-row[data-row="out"] .tool-card-text')?.classList.contains('error') ?? false,
      out: el.querySelector('.tool-card-row[data-row="out"] .tool-card-text')?.textContent ?? '',
    }
  })
  const tabsBefore = await page.locator('.editor-tab').count()
  await page.locator('.tool-card').last().locator('.tool-card-row[data-row="in"]').click()
  await page.waitForTimeout(250)
  const tabsAfter = await page.locator('.editor-tab').count()
  const ok =
    short.clamped === false &&
    short.role === null &&
    short.error &&
    short.outError &&
    short.out === 'exit status 1' &&
    tabsAfter === tabsBefore
  console.log(`phase12 a row that fits stays plain text, and a failed call reads as one ${JSON.stringify({ clamped: short.clamped, role: short.role, error: short.error, out: short.out, tabsAfter })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// --- Phase 13: a search call reads as a Grep card ---

// A search is the second reader of the tool card: the same title and the
// same clipped rows.  What it was told to do is a pattern rather than a
// command, so its IN row carries that and its OUT row the matches — and with
// two rows, each tab names its side, exactly as the Bash card's do.
const GREP_DESC = 'find every place the search root is resolved'
await page.fill('textarea.input-bar-textarea', 'grep: find the search root')
await page.click('button:has-text("Send")')
// Waiting on `.tool-card` would pass at once — the Bash card's box is still
// in the DOM — so wait on the new card's own title instead.
await page.waitForFunction(
  () => [...document.querySelectorAll('.tool-call-name')].some((el) => el.textContent.trim() === 'Grep'),
  { timeout: 8000 },
)
let grepCard = null
{
  await page.waitForTimeout(300)
  grepCard = await page.locator('.tool-call').last().evaluate((el) => {
    const card = el.querySelector('.tool-card')
    const row = (side) => {
      const box = card.querySelector(`.tool-card-row[data-row="${side}"]`)
      const label = box.querySelector('.tool-card-section-title')
      const text = box.querySelector('.tool-card-text')
      const line = parseFloat(getComputedStyle(text).lineHeight)
      return {
        label: label.textContent.trim(),
        // The label column sizes to its own label, and the label must not
        // run into the text it labels.
        labelFits: label.getBoundingClientRect().right <= text.getBoundingClientRect().left,
        text: text.textContent,
        clamped: box.hasAttribute('data-clamped'),
        role: box.getAttribute('role'),
        lines: Math.round(text.clientHeight / line),
        faded: getComputedStyle(box, '::before').backgroundImage.includes('linear-gradient'),
      }
    }
    return {
      name: el.querySelector('.tool-call-name')?.textContent.trim() ?? null,
      desc: el.querySelector('.tool-call-desc')?.textContent.trim() ?? null,
      nameSize: parseFloat(getComputedStyle(el.querySelector('.tool-call-name')).fontSize),
      bodySize: parseFloat(getComputedStyle(document.body).fontSize),
      // The heading lives above the box rather than inside it.
      rows: card.querySelectorAll('.tool-card-row').length,
      // Output is not a thing to paste anywhere: the copy button is the
      // command's alone, and a search has no command.
      copy: card.querySelectorAll('.tool-card-copy').length,
      in: row('in'),
      out: row('out'),
    }
  })
  const ok =
    grepCard.name === 'Grep' &&
    grepCard.desc === GREP_DESC &&
    grepCard.nameSize > grepCard.bodySize &&
    // Two rows: the pattern the call was given, and what it found.
    grepCard.rows === 2 &&
    grepCard.copy === 0 &&
    // A search's first row is named for what it holds rather than for its
    // place in the pair, and the label column has to make room for the word.
    grepCard.in.label === 'PAT' &&
    grepCard.in.labelFits &&
    grepCard.out.label === 'OUT' &&
    // The pattern fits its three lines, so its row is not a control — the
    // clip is the affordance, and nothing here is past it.
    grepCard.in.text === 'search_path' &&
    grepCard.in.clamped === false &&
    grepCard.in.role === null &&
    grepCard.out.clamped &&
    grepCard.out.role === 'button' &&
    grepCard.out.lines === 3 &&
    grepCard.out.faded
  console.log(`phase13 a search call reads as a Grep card ${JSON.stringify({ name: grepCard.name, desc: grepCard.desc, rows: grepCard.rows, copy: grepCard.copy, inLabel: grepCard.in.label, inFits: grepCard.in.labelFits, inText: grepCard.in.text, inClamped: grepCard.in.clamped, outLabel: grepCard.out.label, outLines: grepCard.out.lines, outRole: grepCard.out.role, faded: grepCard.out.faded })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}
// P13b: a search's first line is its title too, so its mark takes the same
// inset the Bash card's does — the alignment follows the card, not the tool.
{
  const marks = await page.locator('.stream-row[data-kind="tool"][data-tool="grep"]').last().evaluate((row) => {
    const mark = row.querySelector('.status-mark')
    const title = row.querySelector('.tool-call-heading')
    const box = row.querySelector('.tool-card')
    const mid = (el) => el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2
    return {
      markMid: mark ? mid(mark) : null,
      titleMid: title ? mid(title) : null,
      titleTop: title ? title.getBoundingClientRect().top : null,
      boxTop: box ? box.getBoundingClientRect().top : null,
    }
  })
  const ok =
    marks.markMid !== null &&
    Math.abs(marks.markMid - marks.titleMid) <= 3 &&
    marks.titleTop < marks.boxTop
  console.log(`phase13 the gutter mark aligns with the title, above the box ${JSON.stringify(marks)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}
// P13c: the result row opens the whole match list, read-only, while the
// pattern row — which fits — stays plain text and opens nothing.  The tab
// counts are relative: earlier phases leave tabs of their own open.
{
  const title = `Grep output — ${GREP_DESC}`
  const tabsBefore = await page.locator('.editor-tab').count()
  await page.locator('.tool-card').last().locator('.tool-card-row[data-row="in"]').click()
  await page.waitForTimeout(250)
  const tabsAfterPattern = await page.locator('.editor-tab').count()
  const okPattern = tabsAfterPattern === tabsBefore
  console.log(`phase13 the pattern row is plain text, with nothing to open ${JSON.stringify({ tabsBefore, tabsAfterPattern })} ${okPattern ? 'PASS' : 'FAIL'}`)
  if (!okPattern) fails++

  await page.locator('.tool-card').last().locator('.tool-card-row[data-row="out"]').click()
  await page.waitForTimeout(300)
  const opened = await page.evaluate(() => ({
    tabs: [...document.querySelectorAll('.editor-tab-name')].map((el) => el.textContent.trim()),
    active: document.querySelector('.editor-tab.active .editor-tab-name')?.textContent.trim() ?? null,
    header: document.querySelector('.editor-path')?.textContent.trim() ?? null,
    doc: document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
    status: document.querySelector('.editor-status')?.textContent.trim() ?? null,
  }))
  // The twelfth match is one the three-line clip never showed, and the tab
  // names its side — a two-row search card has two tabs to tell apart.
  const ok =
    opened.tabs.length === tabsBefore + 1 &&
    opened.active === title &&
    opened.header === title &&
    opened.doc.includes('search.py:98:') &&
    opened.status?.startsWith('read-only') === true
  console.log(`phase13 the OUT row opens the whole result in the editor ${JSON.stringify({ tabs: opened.tabs.length, active: opened.active, status: opened.status, docLines: opened.doc.split('\n').length })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++

  await page.locator('.tool-card').last().locator('.tool-card-row[data-row="out"]').click()
  await page.waitForTimeout(250)
  const again = await page.evaluate(() => ({
    tabs: document.querySelectorAll('.editor-tab').length,
    active: document.querySelector('.editor-tab.active .editor-tab-name')?.textContent.trim() ?? null,
  }))
  const okAgain = again.tabs === tabsBefore + 1 && again.active === title
  console.log(`phase13 a second click focuses the tab it already opened ${JSON.stringify(again)} ${okAgain ? 'PASS' : 'FAIL'}`)
  if (!okAgain) fails++
}
// P13d: the other search tool reads under its own name, and a result that
// fits its three lines stays plain text — no control, and nothing to open.
{
  await page.fill('textarea.input-bar-textarea', 'glob: list the search tools')
  await page.click('button:has-text("Send")')
  await page.waitForFunction(
    () => [...document.querySelectorAll('.tool-call-name')].some((el) => el.textContent.trim() === 'Glob'),
    { timeout: 8000 },
  )
  await page.waitForTimeout(300)
  const tabsBefore = await page.locator('.editor-tab').count()
  const globCard = await page.locator('.tool-call').last().evaluate((el) => {
    const card = el.querySelector('.tool-card')
    const box = card.querySelector('.tool-card-row[data-row="out"]')
    return {
      name: el.querySelector('.tool-call-name')?.textContent.trim() ?? null,
      desc: el.querySelector('.tool-call-desc')?.textContent.trim() ?? null,
      rows: card.querySelectorAll('.tool-card-row').length,
      pattern: card.querySelector('.tool-card-row[data-row="in"] .tool-card-text')?.textContent ?? '',
      patternLabel: card.querySelector('.tool-card-row[data-row="in"] .tool-card-section-title')?.textContent.trim() ?? '',
      clamped: box.hasAttribute('data-clamped'),
      role: box.getAttribute('role'),
      text: box.querySelector('.tool-card-text')?.textContent ?? '',
    }
  })
  await page.locator('.tool-card').last().locator('.tool-card-row[data-row="out"]').click()
  await page.waitForTimeout(250)
  const tabsAfter = await page.locator('.editor-tab').count()
  const ok =
    globCard.name === 'Glob' &&
    globCard.desc === 'list the search tools' &&
    globCard.rows === 2 &&
    globCard.pattern === 'tools/*.py' &&
    globCard.patternLabel === 'PAT' &&
    globCard.clamped === false &&
    globCard.role === null &&
    globCard.text.includes('tools/executor.py') &&
    tabsAfter === tabsBefore
  console.log(`phase13 the second search tool reads as Glob, and a row that fits stays plain text ${JSON.stringify({ name: globCard.name, rows: globCard.rows, pattern: globCard.pattern, patternLabel: globCard.patternLabel, clamped: globCard.clamped, role: globCard.role, tabsAfter })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// --- Phase 14: a git call reads as a Git status card ---

// A git call is the plainest reader of the tool card: the title, and one
// OUT row holding what came back — nothing the call was passed gets a row
// of its own.  The turn runs two of them, so the name table is read twice and
// both readings of a single row, the clipped one and the one that fits, are
// covered.
const GIT_STATUS_DESC = 'check the working tree before committing'
await page.fill('textarea.input-bar-textarea', 'git: check the working tree')
await page.click('button:has-text("Send")')
await page.waitForFunction(
  () => [...document.querySelectorAll('.tool-call-name')].some((el) => el.textContent.trim() === 'Git log'),
  { timeout: 8000 },
)
await page.waitForTimeout(300)
{
  const read = (tool) =>
    page.locator(`.stream-row[data-tool="${tool}"]`).last().evaluate((row) => {
      const card = row.querySelector('.tool-card')
      const box = card.querySelector('.tool-card-row[data-row="out"]')
      const text = box.querySelector('.tool-card-text')
      const line = parseFloat(getComputedStyle(text).lineHeight)
      return {
        name: row.querySelector('.tool-call-name')?.textContent.trim() ?? null,
        desc: row.querySelector('.tool-call-desc')?.textContent.trim() ?? null,
        rows: card.querySelectorAll('.tool-card-row').length,
        label: box.querySelector('.tool-card-section-title').textContent.trim(),
        // Nothing the call was passed gets a row: the box is its result.
        inputRows: card.querySelectorAll('.tool-card-row[data-row="in"]').length,
        copy: card.querySelectorAll('.tool-card-copy').length,
        clamped: box.hasAttribute('data-clamped'),
        role: box.getAttribute('role'),
        lines: Math.round(text.clientHeight / line),
        faded: getComputedStyle(box, '::before').backgroundImage.includes('linear-gradient'),
        text: text.textContent,
      }
    })
  const status = await read('git_status')
  const log = await read('git_log')
  const ok =
    status.name === 'Git status' &&
    status.desc === GIT_STATUS_DESC &&
    status.rows === 1 &&
    status.label === 'OUT' &&
    status.inputRows === 0 &&
    status.copy === 0 &&
    status.clamped &&
    status.role === 'button' &&
    status.lines === 3 &&
    status.faded &&
    // The other name off the table, and a result that fits its three lines:
    // plain text, with nothing to open.
    log.name === 'Git log' &&
    log.rows === 1 &&
    log.label === 'OUT' &&
    log.clamped === false &&
    log.role === null &&
    log.text.includes('3aa5c7c')
  console.log(`phase14 a git call reads as its result alone ${JSON.stringify({ status: { name: status.name, rows: status.rows, label: status.label, inputRows: status.inputRows, copy: status.copy, lines: status.lines, role: status.role }, log: { name: log.name, rows: log.rows, label: log.label, clamped: log.clamped, role: log.role } })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}
// P14b: the single row opens the whole result in a tab named for the card
// alone — a card with one row has no side to name.
{
  const title = `Git status — ${GIT_STATUS_DESC}`
  const tabsBefore = await page.locator('.editor-tab').count()
  await page.locator('.stream-row[data-tool="git_status"] .tool-card-row[data-row="out"]').last().click()
  await page.waitForTimeout(300)
  const opened = await page.evaluate(() => ({
    tabs: [...document.querySelectorAll('.editor-tab-name')].map((el) => el.textContent.trim()),
    active: document.querySelector('.editor-tab.active .editor-tab-name')?.textContent.trim() ?? null,
    doc: document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
    status: document.querySelector('.editor-status')?.textContent.trim() ?? null,
  }))
  // The untracked entry is one the three-line clip never showed.
  const ok =
    opened.tabs.length === tabsBefore + 1 &&
    opened.active === title &&
    opened.doc.includes('console-pane-tool-card.css') &&
    opened.status?.startsWith('read-only') === true
  console.log(`phase14 the OUT row opens the whole result in the editor ${JSON.stringify({ tabs: opened.tabs.length, active: opened.active, status: opened.status })} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++

  await page.locator('.stream-row[data-tool="git_status"] .tool-card-row[data-row="out"]').last().click()
  await page.waitForTimeout(250)
  const again = await page.evaluate(() => ({
    tabs: document.querySelectorAll('.editor-tab').length,
    active: document.querySelector('.editor-tab.active .editor-tab-name')?.textContent.trim() ?? null,
  }))
  const okAgain = again.tabs === tabsBefore + 1 && again.active === title
  console.log(`phase14 a second click focuses the tab it already opened ${JSON.stringify(again)} ${okAgain ? 'PASS' : 'FAIL'}`)
  if (!okAgain) fails++
}

// --- Phase 15: a file call reads as its title alone ---

// A file call draws no box: the path in the title is the whole of it, and a
// click opens that file in the editor — which is where a file is read, rather
// than in a row of copied content.  The title prints the basename of the file
// the *server* resolved for the call, the hover names where it actually is,
// and the click hands the editor the root-relative path a tab holds — or, for
// a call whose file lies outside the root, the absolute path, opened
// read-only and marked external.  That resolution arrives with the call's
// start frame, so a card names its file — and its marker — while the call is
// still waiting at a gate.  Whether that name is a control is the frame's
// answer too, and it is about the file rather than the tool: a path with
// nothing behind it reads as plain text — a write that failed, a read of a
// file that is not there — still saying on hover where the file would be.
const READ_PATH = 'mock-a.md'
const READ_PATH_ABS = '/tmp/mock-a.md'
const WRITE_PATH = 'mock-b.md'
const WRITE_PATH_ABS = '/tmp/mock-b.md'
// A path with no file behind it at all, for the call the mock answers with a
// failure: its title is text, whatever tool named it.
const GONE_PATH_ABS = '/tmp/mock-gone.md'
// The mock's home sits behind a link, the way macOS has /home behind one:
// what the call names and where the file actually is are two strings for one
// file.  The card shows the name the call used, the editor opens the file it
// resolved to.
const OUTSIDE_PATH = '/tmp/mock-home/.toddler/notes.md'
const OUTSIDE_PATH_ABS = '/home/mock/.toddler/notes.md'
await page.fill('textarea.input-bar-textarea', 'files: read and write')
await page.click('button:has-text("Send")')
await page.waitForFunction(
  () => [...document.querySelectorAll('.tool-call-name')].some((el) => el.textContent.trim() === 'Write'),
  { timeout: 8000 },
)
await page.waitForTimeout(300)

// The file turn's last call — the outside read — holds at a gate: the card
// must be complete (basename, external marker, a real click) before the
// call is allowed to run.  Everything up to the approval below judges a
// call that has not executed: the start frame resolves the file, not the
// result.
await page.waitForSelector('.pause-prompt', { timeout: 8000 })
{
  const gated = await page.evaluate(() => {
    const row = [...document.querySelectorAll('.stream-row[data-tool="read_file"]')].at(-1)
    return {
      paused: !!document.querySelector('.pause-prompt'),
      // The card asks about the file the tool would read, not about the
      // call in general: each tool names its own question.
      title: document.querySelector('.pause-prompt-title')?.textContent.trim() ?? null,
      running: row?.querySelector('.status-mark')?.className.includes('running') ?? false,
      label: row?.querySelector('.tool-call-path')?.textContent.trim() ?? null,
      external: row?.querySelector('.external-tag')?.textContent.trim() ?? null,
      symlink: row?.querySelector('.symlink-tag')?.textContent.trim() ?? null,
      tag: row?.querySelector('.tool-call-path')?.tagName ?? null,
    }
  })
  const ok =
    gated.paused && gated.running &&
    gated.title === 'Allow read this file?' &&
    gated.label === 'notes.md' && gated.external === 'external' &&
    gated.symlink === 'symlink' &&
    gated.tag === 'BUTTON'
  console.log(`phase15 the outside card is complete before the gate resolves ${JSON.stringify(gated)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

/** The last five file cards, read the way the phase judges them. */
const readFileCards = () =>
  page.locator('.tool-call').evaluateAll((els) =>
    els.slice(-5).map((el) => {
      const link = el.querySelector('.tool-call-path')
      return {
        name: el.querySelector('.tool-call-name')?.textContent.trim() ?? null,
        // The description gives way to the path in these titles: the path is
        // the half a reader can act on.
        desc: el.querySelector('.tool-call-desc')?.textContent.trim() ?? null,
        // The heading prints the basename; the whole path it stands for is
        // the hover.
        label: link?.textContent.trim() ?? null,
        title: link?.getAttribute('title') ?? null,
        // A path is a control only where there is a file to open: a button
        // where there is one, plain text where there is not.
        tag: link?.tagName ?? null,
        static: link?.classList.contains('static') ?? null,
        // The markers ride beside the button, not inside it, and in
        // document order: the symlink one explains the path, the external
        // one says how the file behind it is reached.
        external: el.querySelector('.external-tag')?.textContent.trim() ?? null,
        symlink: el.querySelector('.symlink-tag')?.textContent.trim() ?? null,
        markers: [...el.querySelectorAll('.symlink-tag, .external-tag')]
          .map((tag) => tag.className),
        symlinkHover: el.querySelector('.symlink-tag')?.getAttribute('title') ?? null,
        // One path slot per title: the button/static pair is a chain, and a
        // marker wedged into it once made every card name its file twice.
        paths: el.querySelectorAll('.tool-call-path').length,
        boxes: el.querySelectorAll('.tool-card').length,
        rows: el.querySelectorAll('.tool-card-row').length,
      }
    }),
  )

/** Phase 15's five cards: [read, edit, write, failed write, outside read]. */
function checkFileCards(cards) {
  const [read, edit, write, failed, outside] = cards
  return (
    read.name === 'Read' &&
    read.label === 'mock-a.md' &&
    read.title === `Open ${READ_PATH_ABS}` &&
    read.tag === 'BUTTON' &&
    edit.name === 'Edit' &&
    edit.label === 'mock-a.md' &&
    edit.title === `Open ${READ_PATH_ABS}` &&
    edit.tag === 'BUTTON' &&
    write.name === 'Write' &&
    write.label === 'mock-b.md' &&
    write.title === 'Open /tmp/mock-b.md' &&
    write.tag === 'BUTTON' &&
    // The write that failed names a path all the same — it just has nothing
    // behind it, so it reads as text.
    failed.name === 'Write' &&
    failed.label === 'mock-c.md' &&
    failed.title === '/tmp/read-only/mock-c.md' &&
    failed.tag === 'SPAN' &&
    failed.static === true &&
    // The read that reached outside the root ran and returned content, and
    // the absolute path it resolved to is what a tab opens it by — a real
    // click, wearing the external marker.
    outside.name === 'Read' &&
    outside.label === 'notes.md' &&
    outside.title === `Open ${OUTSIDE_PATH}` &&
    outside.tag === 'BUTTON' &&
    outside.static === false &&
    outside.external === 'external' &&
    // Its file sits behind a link, so the name the card shows and the path
    // a click opens are two spellings of one file — said out loud, or read
    // as a mistake.  The marker comes first and its hover names the file,
    // which is the one thing left to say once the title names the other
    // spelling.
    outside.symlink === 'symlink' &&
    outside.markers.join() === 'symlink-tag,external-tag' &&
    outside.symlinkHover === `named through a symbolic link — the file is at ${OUTSIDE_PATH_ABS}` &&
    // The markers ride only on the outside file, which is both outside the
    // root and named through a link; the in-root four carry neither.
    read.external === null && read.symlink === null && read.markers.length === 0 &&
    edit.external === null && edit.symlink === null && edit.markers.length === 0 &&
    write.external === null && write.symlink === null && write.markers.length === 0 &&
    failed.external === null && failed.symlink === null && failed.markers.length === 0 &&
    // No box and no rows on any of them, no description either, and exactly
    // one path slot per title.
    cards.every((c) => c.boxes === 0 && c.rows === 0 && c.desc === null && c.paths === 1)
  )
}

{
  // Still gated — the read has not run, and all five cards already read as
  // they will after it does.
  const cards = await readFileCards()
  const ok = checkFileCards(cards)
  console.log(`phase15 a file call is its title alone ${JSON.stringify(cards)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// Approve the outside read so it may run — the tab opened below needs its
// content, and the turn has to finish before the phase's reload.
{
  await page.click('.pause-prompt button:has-text("Approve")')
  await page.waitForSelector('.pause-prompt', { state: 'detached', timeout: 8000 })
  await page.waitForTimeout(250)
}
// P15b: the path opens the file in the editor — the in-root read is the
// first read_file path — and a write that failed opens nothing.  (Two
// read_file paths are buttons now, hence the .first()/.last() pins.)
{
  await page.locator('.stream-row[data-tool="read_file"] .tool-call-path:not(.static)').first().click()
  await page.waitForTimeout(400)
  const opened = await page.evaluate(() => ({
    tabs: [...document.querySelectorAll('.editor-tab-name')].map((el) => el.textContent.trim()),
    header: document.querySelector('.editor-path')?.textContent.trim() ?? null,
    doc: document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
  }))
  const ok =
    opened.tabs.includes(READ_PATH) &&
    opened.header === READ_PATH &&
    opened.doc.includes('first seeded file')
  console.log(`phase15 the path opens that file in the editor ${JSON.stringify(opened)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++

  const tabsBefore = await page.locator('.editor-tab').count()
  await page.locator('.stream-row[data-tool="write_file"] .tool-call-path.static').last().click()
  await page.waitForTimeout(250)
  const tabsAfter = await page.locator('.editor-tab').count()
  const okStatic = tabsAfter === tabsBefore
  console.log(`phase15 a path with no file behind it opens nothing ${JSON.stringify({ tabsBefore, tabsAfter })} ${okStatic ? 'PASS' : 'FAIL'}`)
  if (!okStatic) fails++
}

// P15c: the file outside the root opens too — under its absolute path, in a
// tab that says "external" and refuses every way of writing back.
{
  await page.locator('.stream-row[data-tool="read_file"] .tool-call-path:not(.static)').last().click()
  await page.waitForTimeout(400)
  const opened = await page.evaluate(() => {
    const tabs = [...document.querySelectorAll('.editor-tab')].map((el) => ({
      name: el.querySelector('.editor-tab-name')?.textContent.trim() ?? null,
      title: el.getAttribute('title'),
      external: el.querySelector('.external-tag')?.textContent.trim() ?? null,
      dirty: !!el.querySelector('.editor-tab-dirty'),
    }))
    return {
      tabs,
      header: document.querySelector('.editor-path')?.textContent.trim() ?? null,
      headerExternal: !!document.querySelector('.editor-header .external-tag'),
      doc: document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
      status: document.querySelector('.editor-status')?.textContent.trim() ?? null,
    }
  })
  const external = opened.tabs.find((t) => t.external === 'external')
  const ok =
    external?.name === 'notes.md' &&
    external.title === OUTSIDE_PATH_ABS &&
    external.dirty === false &&
    opened.tabs.filter((t) => t.external !== null).length === 1 &&
    opened.header === OUTSIDE_PATH_ABS &&
    opened.headerExternal &&
    opened.doc.includes('outside the repo') &&
    opened.status === 'read-only · 2 lines'
  console.log(`phase15 the outside file opens read-only under its absolute path ${JSON.stringify(opened)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++

  // Read-only for real: typing must not move the doc, and Ctrl+S must never
  // reach the write endpoint — the mock records every PUT on the mutations
  // backdoor, so an attempt could not hide.
  const before = opened.doc
  await page.locator('.editor-cm .cm-content').click()
  await page.keyboard.type('zzz')
  await page.keyboard.press('Control+s')
  await page.waitForTimeout(150)
  const after = await page.evaluate(() =>
    document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
  )
  const writes = (await sendControl('get_mutations')).mutations
    .filter((m) => m.cmd === 'file_write').length
  const okRo = after === before && writes === 0
  console.log(`phase15 the external tab refuses edits and saves ${JSON.stringify({ changed: after !== before, writes })} ${okRo ? 'PASS' : 'FAIL'}`)
  if (!okRo) fails++
}
// P15d: a reload rebuilds the same cards from the transcript, which stores no
// resolved path — the server recomputes one, so a replayed card opens what a
// live one did — and the external tab comes back from localStorage, still
// read-only and still marked.
{
  await page.reload()
  await page.waitForFunction(
    () => [...document.querySelectorAll('.tool-call-name')].some((el) => el.textContent.trim() === 'Write'),
    { timeout: 8000 },
  )
  await page.waitForTimeout(400)
  const cards = await readFileCards()
  const ok = checkFileCards(cards)
  console.log(`phase15 a reload replays the same cards ${JSON.stringify(cards)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++

  const restored = await page.evaluate(() => {
    const tab = [...document.querySelectorAll('.editor-tab')]
      .find((el) => el.querySelector('.external-tag'))
    return {
      name: tab?.querySelector('.editor-tab-name')?.textContent.trim() ?? null,
      title: tab?.getAttribute('title') ?? null,
      active: tab?.classList.contains('active') ?? false,
      header: document.querySelector('.editor-path')?.textContent.trim() ?? null,
      doc: document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
      status: document.querySelector('.editor-status')?.textContent.trim() ?? null,
    }
  })
  const okRestored =
    restored.name === 'notes.md' &&
    restored.title === OUTSIDE_PATH_ABS &&
    restored.active &&
    restored.header === OUTSIDE_PATH_ABS &&
    restored.doc.includes('outside the repo') &&
    restored.status === 'read-only · 2 lines'
  console.log(`phase15 the external tab survives a reload ${JSON.stringify(restored)} ${okRestored ? 'PASS' : 'FAIL'}`)
  if (!okRestored) fails++
}

// P15e: the rule is about the file behind the name, not about the tool.  A
// read of a path with nothing there reads as plain text — the same reading a
// write that failed gets — and a write over a file that IS there turns its
// basename into a control from the moment the call is named, before the gate
// has let it run: the file it is about to replace is exactly what a reader
// wants in front of them while deciding whether to let it.
{
  await page.fill('textarea.input-bar-textarea', 'overwrite it')
  await page.click('button:has-text("Send")')
  await page.waitForSelector('.pause-prompt', { timeout: 8000 })
  await page.waitForTimeout(300)
  const titles = await page.evaluate(() => {
    const last = (tool) =>
      [...document.querySelectorAll(`.stream-row[data-tool="${tool}"]`)].at(-1)
    const gone = last('read_file')?.querySelector('.tool-call-path')
    const write = last('write_file')?.querySelector('.tool-call-path')
    return {
      paused: !!document.querySelector('.pause-prompt'),
      gone: {
        label: gone?.textContent.trim() ?? null,
        tag: gone?.tagName ?? null,
        static: gone?.classList.contains('static') ?? null,
        // A path with nothing to open still says where the file would be.
        hover: gone?.getAttribute('title') ?? null,
      },
      write: {
        label: write?.textContent.trim() ?? null,
        tag: write?.tagName ?? null,
        hover: write?.getAttribute('title') ?? null,
      },
    }
  })
  const okTitles =
    titles.paused &&
    titles.gone.label === 'mock-gone.md' &&
    titles.gone.tag === 'SPAN' &&
    titles.gone.static === true &&
    titles.gone.hover === GONE_PATH_ABS &&
    titles.write.label === 'mock-b.md' &&
    titles.write.tag === 'BUTTON' &&
    titles.write.hover === `Open ${WRITE_PATH_ABS}`
  console.log(`phase15 a title is a control where a file is, and plain text where none is ${JSON.stringify(titles)} ${okTitles ? 'PASS' : 'FAIL'}`)
  if (!okTitles) fails++

  // The click is a real one, and it lands while the gate is still up — on
  // the file the write has not replaced yet, which is the point of offering
  // it this early.
  await page.locator('.stream-row[data-tool="write_file"] .tool-call-path').last().click()
  await page.waitForTimeout(400)
  const opened = await page.evaluate(() => ({
    tabs: [...document.querySelectorAll('.editor-tab-name')].map((el) => el.textContent.trim()),
    header: document.querySelector('.editor-path')?.textContent.trim() ?? null,
    doc: document.querySelector('.editor-cm .cm-content')?.textContent ?? '',
  }))
  const okOpened =
    opened.tabs.includes(WRITE_PATH) &&
    opened.header === WRITE_PATH &&
    opened.doc.includes('second seeded file')
  console.log(`phase15 the title of a pending write opens the file it would replace ${JSON.stringify(opened)} ${okOpened ? 'PASS' : 'FAIL'}`)
  if (!okOpened) fails++

  // Approving settles the call, and the title stays a control: what the end
  // frame reports is the disk after the write, not a card that went back to
  // guessing.
  await page.click('.pause-prompt button:has-text("Approve")')
  await page.waitForSelector('.pause-prompt', { state: 'detached', timeout: 8000 })
  await page.waitForTimeout(300)
  const landed = await page.evaluate(() => {
    const write = [...document.querySelectorAll('.stream-row[data-tool="write_file"]')].at(-1)
    const link = write?.querySelector('.tool-call-path')
    return {
      tag: link?.tagName ?? null,
      running: write?.querySelector('.status-mark')?.className.includes('running') ?? false,
    }
  })
  const okLanded = landed.tag === 'BUTTON' && !landed.running
  console.log(`phase15 the title opens the file it wrote, once it has written it ${JSON.stringify(landed)} ${okLanded ? 'PASS' : 'FAIL'}`)
  if (!okLanded) fails++
}

// --- Phase 16: the plan line, its document and the todo trail ---

// A proposed plan reads as the title-only line a file call does — "Plan"
// beside the plan's title — and opens its display markdown as a rendered,
// read-only document in the editor on arrival; the title stays the way back
// to that tab.  The document is frozen: each plan_update call prints the
// whole step list as a todo block beside it, so the console keeps the trail
// while the plan it describes never changes.
const PLAN_TITLE = 'Split the search root out of the tool'
const PLAN_STEP_DESCRIPTIONS = [
  'Read search.py and find the resolver',
  'Move the resolver into its own module',
]
/** One printed checklist, flattened for comparison: "mark|description" per
 *  item, one per line, in plan order. */
const planTodo = (marks) =>
  marks.map((mark, i) => `${mark}|${PLAN_STEP_DESCRIPTIONS[i]}`).join('\n')
{
  const tabsBeforePlan = await page.locator('.editor-tab').count()
  await page.fill('textarea.input-bar-textarea', 'plan the refactor')
  await page.click('button:has-text("Send")')
  await page.waitForSelector('.stream-row[data-kind="plan"]', { timeout: 8000 })
  await page.waitForTimeout(250)

  // P16a: the line is the whole console reading — name, clickable title, no
  // box, no steps, no inline decision buttons.
  const line = await page.$eval('.stream-row[data-kind="plan"]', (el) => {
    const link = el.querySelector('.tool-call-path')
    return {
      name: el.querySelector('.tool-call-name')?.textContent.trim() ?? null,
      link: link?.textContent.trim() ?? null,
      tag: link?.tagName ?? null,
      boxes: el.querySelectorAll('.tool-card').length,
      // The row is one heading line and nothing else — name + title, no
      // step list, no box, no inline decision buttons.
      headings: el.querySelectorAll('.tool-call-heading').length,
      parts: el.querySelector('.tool-call-heading')?.children.length ?? 0,
      buttons: el.querySelectorAll('.btn').length,
    }
  })
  const okLine =
    line.name === 'Plan' &&
    line.link === PLAN_TITLE &&
    line.tag === 'BUTTON' &&
    line.boxes === 0 &&
    line.headings === 1 &&
    line.parts === 2 &&
    line.buttons === 0
  console.log(`phase16 a plan proposal is one line ${JSON.stringify(line)} ${okLine ? 'PASS' : 'FAIL'}`)
  if (!okLine) fails++

  // The gutter mark centres on the title line, the way a tool call's does —
  // the plan row has no card box under it, so the mark takes the heading's
  // own inset rather than the old card's border + padding.
  const mark = await page.$eval('.stream-row[data-kind="plan"]', (row) => {
    const rect = (el) => el.getBoundingClientRect()
    const m = rect(row.querySelector('.status-mark'))
    const t = rect(row.querySelector('.tool-call-heading'))
    return { markMid: m.top + m.height / 2, titleMid: t.top + t.height / 2 }
  })
  const okMark = Math.abs(mark.markMid - mark.titleMid) <= 3
  console.log(`phase16 the gutter mark centres on the plan title ${JSON.stringify(mark)} ${okMark ? 'PASS' : 'FAIL'}`)
  if (!okMark) fails++

  // Awaiting the decision, the proposal's own mark is the live one: a
  // spinner, not a reading of the steps (nothing has run yet).
  const pending = await page.$eval('.stream-row[data-kind="plan"] .status-mark', (el) => ({
    cls: el.className,
    spinner: !!el.querySelector('.spinner'),
    glyph: el.textContent.trim(),
  }))
  const okPending = pending.cls.includes('running') && pending.spinner && !pending.glyph
  console.log(`phase16 an unanswered plan spins ${JSON.stringify(pending)} ${okPending ? 'PASS' : 'FAIL'}`)
  if (!okPending) fails++

  const ask = await page.$eval('.plan-ask', (el) => el.textContent ?? '').catch(() => '')
  const okAsk = ask.includes(PLAN_TITLE) && ask.includes('Approve')
  console.log(`phase16 the dock still asks for the decision ${JSON.stringify(ask.slice(0, 60))} ${okAsk ? 'PASS' : 'FAIL'}`)
  if (!okAsk) fails++

  // P16b: the proposal opens the plan itself — a rendered document, one
  // tab, active, without waiting for a click: the decision bar is up, and
  // the plan is the thing to read while it waits.
  await page.waitForSelector('.editor-doc', { timeout: 8000 })
  const doc = await page.evaluate(() => {
    const el = document.querySelector('.editor-doc')
    return {
      h2: el?.querySelector('h2')?.textContent.trim() ?? null,
      steps: [...(el?.querySelectorAll('ol > li') ?? [])].map((li) => li.textContent.trim()),
      rationale: el?.textContent.includes('Rationale') ?? false,
      risks: el?.textContent.includes('Risks') ?? false,
      header: document.querySelector('.doc-view .editor-path')?.textContent.trim() ?? null,
      status: document.querySelector('.doc-view .editor-status')?.textContent.trim() ?? null,
      bodyHidden: document.querySelector('.editor-body')?.classList.contains('hidden') ?? false,
      tabs: document.querySelectorAll('.editor-tab').length,
      active: document.querySelector('.editor-tab.active .editor-tab-name')?.textContent.trim() ?? null,
      raw: el?.textContent.includes('## Plan:') ?? false,
    }
  })
  const okDoc =
    doc.h2 === `Plan: ${PLAN_TITLE}` &&
    // startsWith, not equality: format_for_display appends each step's
    // files_affected in brackets, which the renderer keeps.
    doc.steps[0].startsWith(PLAN_STEP_DESCRIPTIONS[0]) &&
    doc.steps[1].startsWith(PLAN_STEP_DESCRIPTIONS[1]) &&
    doc.rationale &&
    doc.risks &&
    doc.header === PLAN_TITLE &&
    doc.status === 'read-only · rendered markdown' &&
    doc.bodyHidden &&
    doc.tabs === tabsBeforePlan + 1 &&
    doc.active === PLAN_TITLE &&
    !doc.raw // rendered, not the source
  console.log(`phase16 the proposal opens the plan as a rendered document ${JSON.stringify(doc)} ${okDoc ? 'PASS' : 'FAIL'}`)
  if (!okDoc) fails++

  // P16c: the tab is one per plan — the title click lands on the tab the
  // proposal already opened, never a copy.
  await page.click('.stream-row[data-kind="plan"] .tool-call-path')
  await page.waitForTimeout(250)
  const tabsAgain = await page.locator('.editor-tab').count()
  const okTabs = tabsAgain === tabsBeforePlan + 1
  console.log(`phase16 the title click reuses the tab it opened ${JSON.stringify({ tabsBeforePlan, tabsAgain })} ${okTabs ? 'PASS' : 'FAIL'}`)
  if (!okTabs) fails++

  // P16d: approving settles the proposal's own mark at once — the decision
  // is what that mark reads, not the execution — and the beats stream
  // beside it: a checklist at each step's start, then one for the finished
  // plan, while the document stays as proposed.
  const frozen = await page.$eval('.editor-doc', (el) => el.textContent)
  await page.click('.plan-ask .btn.primary')
  const decided = await page.$eval('.stream-row[data-kind="plan"] .status-mark', (el) => ({
    cls: el.className,
    glyph: el.textContent.trim(),
    spinner: !!el.querySelector('.spinner'),
  }))
  const okDecided = decided.cls.includes('ok') && decided.glyph === '✓' && !decided.spinner
  console.log(`phase16 approving settles the mark at once ${JSON.stringify(decided)} ${okDecided ? 'PASS' : 'FAIL'}`)
  if (!okDecided) fails++
  // Mid-turn, with only the first beats in: the prose each reported step
  // carried is already settled — plan_update is hidden on the wire, so the
  // trail frame is the only thing that can close it, and without that a
  // step's row would spin until the turn ended.
  await page.waitForFunction(
    () => document.querySelectorAll('.stream-row[data-kind="todo"]').length >= 2,
    { timeout: 8000 },
  )
  const liveProse = await page.$$eval(
    '.stream-row[data-kind="assistant"] .status-mark.running',
    (els) => els.length,
  )
  const okSettled = liveProse === 0
  console.log(`phase16 a reported step's prose stops spinning ${JSON.stringify({ liveProse })} ${okSettled ? 'PASS' : 'FAIL'}`)
  if (!okSettled) fails++

  await page.waitForFunction(
    () => document.querySelectorAll('.stream-row[data-kind="todo"]').length >= 3,
    { timeout: 8000 },
  )
  await page.waitForTimeout(250)
  const trail = await page.$$eval('.stream-row[data-kind="todo"]', (rows) =>
    rows.map((row) =>
      [...row.querySelectorAll('.todo-item')]
        .map((item) => {
          const mark = item.querySelector('.todo-box')?.textContent.trim() ?? ''
          const text = item.querySelector('.todo-text')?.textContent.trim() ?? ''
          return `${mark}|${text}`
        })
        .join('\n'),
    ),
  )
  // Three prints for four plan_update calls: a list at each step's start
  // and one for the finished plan — the step-1 completion moves a single
  // box and is folded (the next start's list shows it).
  const okTrail =
    trail.length === 3 &&
    trail[0] === planTodo(['*', '']) &&
    trail[1] === planTodo(['✓', '*']) &&
    trail[2] === planTodo(['✓', '✓'])
  console.log(`phase16 a step start or the finished plan prints the list ${JSON.stringify(trail)} ${okTrail ? 'PASS' : 'FAIL'}`)
  if (!okTrail) fails++

  // A todo row draws no gutter mark: the boxes inside it carry each step's
  // status, so a mark beside them would be a second reading — one that
  // contradicts them, since the block itself is a settled print.  The row
  // keeps the indent, so the checklist lines up with the prose above.
  const todoRow = await page.$eval('.stream-row[data-kind="todo"]', (row) => {
    const list = row.querySelector('.todo-list').getBoundingClientRect()
    // A marked output row to measure the indent against — the plan line
    // above sits at the same gutter.
    const heading = document
      .querySelector('.stream-row[data-kind="plan"] .tool-call-heading')
      .getBoundingClientRect()
    return {
      marks: row.querySelectorAll(':scope > .status-mark').length,
      left: Math.round(list.left),
      outputLeft: Math.round(heading.left),
    }
  })
  const okTodoMark = todoRow.marks === 0 && todoRow.left === todoRow.outputLeft
  console.log(`phase16 the todo row draws no mark, keeps the indent ${JSON.stringify(todoRow)} ${okTodoMark ? 'PASS' : 'FAIL'}`)
  if (!okTodoMark) fails++

  const docAfter = await page.$eval('.editor-doc', (el) => el.textContent)
  const okFrozen = docAfter === frozen
  console.log(`phase16 the document stays as proposed ${okFrozen ? 'PASS' : 'FAIL'}`)
  if (!okFrozen) fails++

  // A document tab holds frame content, never a disk copy — it must not
  // come back from the tab store.
  const stored = await page.evaluate(() => localStorage.getItem('tod.tabs') ?? '')
  const okStored = !stored.includes('plan:')
  console.log(`phase16 a document tab never persists ${JSON.stringify(stored)} ${okStored ? 'PASS' : 'FAIL'}`)
  if (!okStored) fails++

  // P16e: denying settles the mark the other way — ✗ — and the approved
  // plan's own ✓ stays its row's reading.
  await page.fill('textarea.input-bar-textarea', 'plan the refactor again')
  await page.click('button:has-text("Send")')
  await page.waitForFunction(
    () => document.querySelectorAll('.stream-row[data-kind="plan"]').length >= 2,
    { timeout: 8000 },
  )
  await page.waitForTimeout(250)
  // Deny opens the feedback box; submitting it (empty = a plain reject) is
  // the decision itself.
  await page.click('.plan-ask .btn.danger')
  await page.waitForSelector('.plan-ask-feedback', { timeout: 4000 })
  await page.click('.plan-ask-feedback .btn.danger')
  await page.waitForTimeout(250)
  const marks = await page.$$eval('.stream-row[data-kind="plan"] .status-mark', (els) =>
    els.map((el) => ({ cls: el.className, glyph: el.textContent.trim() })),
  )
  const okDenied =
    marks.length === 2 &&
    marks[0].cls.includes('ok') &&
    marks[0].glyph === '✓' &&
    marks[1].cls.includes('error') &&
    marks[1].glyph === '✗'
  console.log(`phase16 denying settles the mark as failed ${JSON.stringify(marks)} ${okDenied ? 'PASS' : 'FAIL'}`)
  if (!okDenied) fails++

  // P16f: a cancelled turn settles the plan it was carrying as failed, the
  // way it settles an abandoned tool call — the ask is gone, and the plan
  // cannot be resumed — while the plans answered earlier keep their own
  // readings.
  await page.fill('textarea.input-bar-textarea', 'plan one more time')
  await page.click('button:has-text("Send")')
  await page.waitForFunction(
    () => document.querySelectorAll('.stream-row[data-kind="plan"]').length >= 3,
    { timeout: 8000 },
  )
  await page.click('.plan-ask .btn.primary')
  await page.click('button:has-text("Cancel")')
  await page.waitForTimeout(400)
  const cancelled = await page.$$eval(
    '.stream-row[data-kind="plan"] .status-mark',
    (els) => els.map((el) => ({ cls: el.className, glyph: el.textContent.trim() })),
  )
  const okCancelledPlan =
    cancelled.length === 3 &&
    cancelled[2].cls.includes('error') &&
    cancelled[2].glyph === '✗' &&
    cancelled.every((m) => !m.cls.includes('running'))
  console.log(`phase16 cancelling settles the plan as failed ${JSON.stringify(cancelled)} ${okCancelledPlan ? 'PASS' : 'FAIL'}`)
  if (!okCancelledPlan) fails++
}


// --- Phase 17: the gate's card — a title, the call's content, its line ---
// The dock's prompt is a record now rather than a sentence: a title every
// call is asked about, the call's own content where it has any (a shell
// command, whole and untruncated) and the model's line about the call under
// it.  The content is boxed and scrolls past six lines — the one reading of
// the card a one-line command can never show, and the reason the block is
// capped rather than clipped.
{
  // Both measurements read the same two things: what the content block drew,
  // and the height the dock reported to the console under it.
  const measure = () => page.evaluate(() => {
    const card = document.querySelector('.pause-prompt')
    const content = card.querySelector('.pause-prompt-content')
    const desc = card.querySelector('.pause-prompt-description')
    const style = getComputedStyle(content)
    const pane = document.querySelector('.pane-console')
    return {
      title: card.querySelector('.pause-prompt-title')?.textContent.trim() ?? null,
      content: content.textContent,
      desc: desc?.textContent.trim() ?? null,
      lineHeight: parseFloat(style.lineHeight),
      clientHeight: content.clientHeight,
      scrollHeight: content.scrollHeight,
      overflowY: style.overflowY,
      lines: content.textContent.split('\n').length,
      contentBeforeDesc: !!(desc &&
        (content.compareDocumentPosition(desc) & Node.DOCUMENT_POSITION_FOLLOWING)),
      barHeight: parseInt(getComputedStyle(pane).getPropertyValue('--bar-h'), 10),
    }
  })

  // Phase 16 left a cancelled plan whose beats keep arriving — the mock
  // streams them to the end, where a real server stops at the cancel — and
  // the agent_finished that closes them would clear a gate that beat it
  // here.  So the last of that stream is waited out before asking for one.
  await page.waitForTimeout(1000)

  await page.fill('textarea.input-bar-textarea', 'gate: list files')
  await page.click('button:has-text("Send")')
  await page.waitForSelector('.pause-prompt', { timeout: 15000 })
  await page.waitForTimeout(300)
  const short = await measure()

  // One line, so the block drew one line: a max-height, not a height — the
  // cap only ever bites on a command that overflows it.
  const okShort =
    short.title === 'Allow this bash command?' &&
    short.content === 'ls -la' &&
    short.desc === 'list the files' &&
    short.contentBeforeDesc &&
    short.overflowY === 'auto' &&
    short.lines === 1 &&
    Math.abs(short.clientHeight - (short.lineHeight + 16)) <= 1.5 &&
    short.scrollHeight === short.clientHeight
  console.log(`phase17 a one-line command draws one line ${JSON.stringify(short)} ${okShort ? 'PASS' : 'FAIL'}`)
  if (!okShort) fails++

  // The selection outlives the focus, and the arrows follow the click: with
  // the gate up, clicking the theme toggle must leave the card's own mark
  // where it was — a gate never reads as unanswered, or unanswerable,
  // because the pointer was moved — while ↑/↓ belong to whatever was
  // clicked; clicking back on the card is what takes them back.
  await page.keyboard.press('ArrowDown') // selection to Deny
  await page.waitForTimeout(100)
  await page.click('.theme-toggle')
  await page.waitForTimeout(150)
  const clicked = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('.pause-prompt-actions button')]
    return {
      focus: document.activeElement?.className ?? '',
      marked: rows.filter((row) => row.classList.contains('is-selected')).map((row) => row.textContent.trim()),
      tinted: rows.filter((row) => getComputedStyle(row).backgroundColor !== 'rgba(0, 0, 0, 0)').length,
    }
  })
  await page.keyboard.press('ArrowDown')
  await page.waitForTimeout(100)
  const elsewhere = await page.evaluate(() => ({
    focus: document.activeElement?.className ?? '',
    marked: [...document.querySelectorAll('.pause-prompt-actions button.is-selected')]
      .map((row) => row.textContent.trim()),
  }))
  const okSticky =
    clicked.focus === 'theme-toggle' &&
    clicked.marked.join() === 'Deny' && clicked.tinted === 1 &&
    elsewhere.focus === 'theme-toggle' && elsewhere.marked.join() === 'Deny'
  console.log(`phase17 the selection outlives the focus ${JSON.stringify({ clicked, elsewhere })} ${okSticky ? 'PASS' : 'FAIL'}`)
  if (!okSticky) fails++

  // And the dock's edge says where the next keypress lands: the dim border
  // while the toggle has it, the accent while the card does.
  const edge = () => page.evaluate(() => {
    const probe = document.createElement('span')
    const resolve = (name) => {
      probe.style.color = `var(${name})`
      document.body.append(probe)
      const value = getComputedStyle(probe).color
      probe.remove()
      return value
    }
    return {
      border: getComputedStyle(document.querySelector('.console-dock')).borderTopColor,
      accent: resolve('--accent-hover'),
      dim: resolve('--editor-border'),
    }
  })
  const edgeLost = await edge()

  // No frame may take the keyboard back either.  The console's reducer
  // clones the whole state on every action, so `paused` is a new object
  // after any frame at all — the heartbeat's pong included — and a card
  // that re-focused on that object would take the keyboard out of whatever
  // is being read.  That is the bug this pins: it was found by holding an
  // arrow key long enough for a pong to land mid-scroll.  `set_busy` is
  // here only because it makes the mock emit a frame on demand.
  await page.click('.tool-card-copy')
  await page.waitForTimeout(150)
  const beforeFrame = await page.evaluate(() => ({
    focus: document.activeElement?.className ?? '',
    marked: [...document.querySelectorAll('.pause-prompt-actions button.is-selected')]
      .map((row) => row.textContent.trim()),
  }))
  await sendControl('set_busy', { busy: true })
  await sendControl('set_busy', { busy: false })
  await page.waitForTimeout(250)
  const afterFrame = await page.evaluate(() => ({
    focus: document.activeElement?.className ?? '',
    marked: [...document.querySelectorAll('.pause-prompt-actions button.is-selected')]
      .map((row) => row.textContent.trim()),
  }))
  const okQuiet =
    beforeFrame.focus.startsWith('tool-card-copy') &&
    afterFrame.focus === beforeFrame.focus &&
    afterFrame.marked.join() === beforeFrame.marked.join() &&
    afterFrame.marked.length === 1
  console.log(`phase17 a frame does not take the keyboard back ${JSON.stringify({ beforeFrame, afterFrame })} ${okQuiet ? 'PASS' : 'FAIL'}`)
  if (!okQuiet) fails++

  // A click on the card's own body takes the arrows back, and they carry on
  // from the row it marked: ↑ from Deny reaches Approve, where starting
  // over at the ends would have left the selection on Deny (the last row).
  await page.click('.pause-prompt-title')
  await page.waitForTimeout(100)
  const back = await page.evaluate(() => ({
    focus: document.activeElement?.className ?? '',
    marked: [...document.querySelectorAll('.pause-prompt-actions button.is-selected')]
      .map((row) => row.textContent.trim()),
  }))
  await page.keyboard.press('ArrowUp')
  await page.waitForTimeout(100)
  const rearmed = await page.evaluate(() => ({
    focus: document.activeElement?.textContent?.trim() ?? '',
    marked: [...document.querySelectorAll('.pause-prompt-actions button.is-selected')]
      .map((row) => row.textContent.trim()),
  }))
  const okRearmed =
    back.focus === 'pause-prompt' && back.marked.join() === 'Deny' &&
    rearmed.focus === 'Approve' && rearmed.marked.join() === 'Approve'
  console.log(`phase17 clicking the card takes the arrows back ${JSON.stringify({ back, rearmed })} ${okRearmed ? 'PASS' : 'FAIL'}`)
  if (!okRearmed) fails++

  const edgeBack = await edge()
  const okEdge = edgeLost.border === edgeLost.dim && edgeBack.border === edgeBack.accent
  console.log(`phase17 the dock's edge follows the keyboard ${JSON.stringify({ edgeLost, edgeBack })} ${okEdge ? 'PASS' : 'FAIL'}`)
  if (!okEdge) fails++

  // Enter answers the row the card has marked, wherever the focus is in it:
  // the card's own body is focusable (a click lands there), and the edge it
  // lights says the keys are its own.  Deny is marked first so the answer is
  // readable — the mock denies what it is told to deny.
  await page.keyboard.press('ArrowDown') // selection to Deny
  await page.click('.pause-prompt-title') // focus the card's body, mark stays
  await page.waitForTimeout(150)
  const beforeEnter = await page.evaluate(() => ({
    focus: document.activeElement?.className ?? '',
    marked: [...document.querySelectorAll('.pause-prompt-actions button.is-selected')]
      .map((row) => row.textContent.trim()),
  }))
  await page.keyboard.press('Enter')
  await page.waitForSelector('.pause-prompt', { state: 'detached', timeout: 8000 })
  const denied = await page.evaluate(() => document.querySelector('.console-pane').innerText.includes('denied'))
  const okEnter = beforeEnter.focus === 'pause-prompt' && beforeEnter.marked.join() === 'Deny' && denied
  console.log(`phase17 Enter on the card answers the marked row ${JSON.stringify({ beforeEnter, denied })} ${okEnter ? 'PASS' : 'FAIL'}`)
  if (!okEnter) fails++

  await page.click('.theme-toggle') // leave the run in the theme it started in

  await page.fill('textarea.input-bar-textarea', 'script: count the lines')
  await page.click('button:has-text("Send")')
  await page.waitForSelector('.pause-prompt .pause-prompt-content', { timeout: 15000 })
  await page.waitForTimeout(300)
  const long = await measure()
  // The cap is six lines plus the box's own padding; clientHeight measures
  // the content box and its padding, not the border, so the two agree at
  // 6lh + 16px.
  const capped = Math.abs(long.clientHeight - (6 * long.lineHeight + 16)) <= 1.5
  const overflows = long.scrollHeight > long.clientHeight + 1
  const okLong =
    long.lines === 12 &&
    long.overflowY === 'auto' &&
    capped &&
    overflows
  console.log(`phase17 a long command is capped and overflows ${JSON.stringify(long)} ${okLong ? 'PASS' : 'FAIL'}`)
  if (!okLong) fails++

  // The rest of the command is reachable, not hidden: the block is a
  // scroller, so the tail is a scroll away.
  const tail = await page.evaluate(() => {
    const content = document.querySelector('.pause-prompt .pause-prompt-content')
    content.scrollTop = content.scrollHeight
    return {
      scrollTop: content.scrollTop,
      bottomReached: content.scrollTop + content.clientHeight >= content.scrollHeight - 1,
    }
  })
  const okTail = tail.scrollTop > 0 && tail.bottomReached
  console.log(`phase17 the tail of the command scrolls into view ${JSON.stringify(tail)} ${okTail ? 'PASS' : 'FAIL'}`)
  if (!okTail) fails++

  // The card grew, and the console heard about it: --bar-h is the dock's own
  // ResizeObserver handshake, and a taller gate must move the pinned tail up
  // with it or the last line of the transcript sits under the card.
  const okBar = long.barHeight > short.barHeight
  console.log(`phase17 the dock reports its new height ${short.barHeight} → ${long.barHeight} ${okBar ? 'PASS' : 'FAIL'}`)
  if (!okBar) fails++

  await page.click('.ask-close')
  await page.waitForSelector('.pause-prompt', { state: 'detached', timeout: 8000 })
}
