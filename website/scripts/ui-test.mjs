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
        resolve()
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

await browser.close()
process.exit(fails ? 1 : 0)
