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
//            reaches the wire exactly once, obeys the busy gate, follows
//            another tab's switch into an open menu, replays the pill and
//            the picked row from the server after a reload, stops claiming a
//            conversation whose slot was retargeted, and excludes the mode
//            popup from the keyboard
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
    const output = rows.filter((r) => r.dataset.kind !== 'user')
    return {
      rows: rows.length,
      output: output.length,
      // the doc's contract: exactly one mark per output row, none on a user
      // row, and every mark left of the block it belongs to
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
// closed the answer block on tool_call_start, this row span forever.
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
      // the mark moved out of the card: nothing inline in its header
      inline: document.querySelectorAll(
        '.tool-card-header .spinner, .tool-card-header .tool-card-status',
      ).length,
    }
  })
  const ok =
    live.live === 1 &&
    live.lastKind === 'tool' &&
    live.toolLive &&
    live.answerSettled &&
    live.inline === 0
  console.log(`phase6 gate holds one live mark, its answer settled ${JSON.stringify(live)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}
await page.click('.ask-close')
await page.waitForSelector('.pause-prompt', { state: 'detached', timeout: 8000 })

// P6c: a cancelled call settles as · — not a spinner that never stops.
{
  const mark = await page.evaluate(() =>
    document
      .querySelector('.stream-row[data-kind="tool"] .status-mark.cancelled')
      ?.textContent.trim() ?? null,
  )
  console.log(`phase6 cancelled call reads · ${JSON.stringify(mark)} ${mark === '·' ? 'PASS' : 'FAIL'}`)
  if (mark !== '·') fails++
}

// P6d: a failed call is the one producer of ✗.
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

// P11h: the busy gate.  A turn starting anywhere takes the picker's gate
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

// P11i: another tab's switch.  The broadcast updates the pill AND the menu
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

// P11j: this tab's own two switches ride the same session_info, so the pill
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

// P11k: a slot retargeted underneath a live conversation.  The conversation
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

// P11l: a tier the scale does not hold.  `TODDLER_EFFORT_LEVEL` is never
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

// P11m: the two popups exclude each other on the keyboard path as well.
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

await browser.close()
process.exit(fails ? 1 : 0)
