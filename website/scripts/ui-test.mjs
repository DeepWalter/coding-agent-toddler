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
//  phase 5 — thinking cards: the seeded reasoning replays collapsed and
//            expands to its verbatim body; a live `think:` turn opens a
//            second card above its bubble reading "Thinking…" while
//            streaming, "Thought" after agent_finished — and a reload
//            replays the same card, body byte-identical
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

// --- Phase 5: thinking cards — collapsed, verbatim, replayable ---
// The mock seeds one reasoning-bearing transcript entry (SEED_REASONING in
// mock-server.mjs) and streams a second on a `think:` turn.
const SEED_REASONING =
  'seed thought: check the pane scrolls before answering\n' +
  'second line — rendered verbatim, never markdown'

// Document positions of the newest thinking card and newest assistant
// bubble — the card must sit above the bubble its answer lands in.
const cardAboveBubble = () =>
  page.evaluate(() => {
    const nodes = [...document.querySelector('.console-pane').children]
    const cards = nodes.filter((el) => el.classList.contains('thinking-card'))
    const bubbles = nodes.filter(
      (el) => el.classList.contains('message') && el.classList.contains('assistant'),
    )
    const card = nodes.indexOf(cards[cards.length - 1])
    const bubble = nodes.indexOf(bubbles[bubbles.length - 1])
    return { card, bubble, ok: card >= 0 && bubble >= 0 && card < bubble }
  })

// P5a: the replayed card starts collapsed — "Thought" + ▸, no body.
{
  const card = page.locator('.thinking-card').first()
  await card.waitFor({ timeout: 15000 })
  const info = {
    label: (await card.locator('.thinking-card-label').textContent()).trim(),
    chevron: (await card.locator('.thinking-card-chevron').textContent()).trim(),
    bodies: await card.locator('.thinking-card-pre').count(),
  }
  const ok = info.label === 'Thought' && info.chevron === '▸' && info.bodies === 0
  console.log(`phase5 seeded card collapsed ${JSON.stringify(info)} ${ok ? 'PASS' : 'FAIL'}`)
  if (!ok) fails++
}

// P5b: click expands to the verbatim body; a second click collapses it.
{
  const card = page.locator('.thinking-card').first()
  await card.locator('.thinking-card-header').click()
  const body = await card.locator('.thinking-card-pre').textContent()
  const ok = body === SEED_REASONING
  console.log(`phase5 expand shows verbatim body ${ok ? 'PASS' : `FAIL (${JSON.stringify(body)})`}`)
  if (!ok) fails++
  await card.locator('.thinking-card-header').click()
  const collapsed = (await card.locator('.thinking-card-pre').count()) === 0
  console.log(`phase5 second click collapses ${collapsed ? 'PASS' : 'FAIL'}`)
  if (!collapsed) fails++
}

// P5c: a live think turn opens one more card, reading "Thinking…" while the
// reasoning streams (the mock holds before the answer for this window).
// Counts are relative: the mock's turnLog grows across runs, so asserting
// a literal card count would only hold for a freshly started mock.
const cardCount = () => page.locator('.thinking-card').count()
const cardsBefore = await cardCount()
{
  await page.fill('textarea.input-bar-textarea', 'think: about it')
  await page.click('button:has-text("Send")')
  let sawOpen = true
  try {
    await page.waitForFunction((want) => {
      const cards = [...document.querySelectorAll('.thinking-card')]
      const last = cards[cards.length - 1]
      return (
        cards.length === want &&
        last?.querySelector('.thinking-card-label')?.textContent?.trim() === 'Thinking…'
      )
    }, cardsBefore + 1, { timeout: 15000 })
  } catch {
    sawOpen = false
  }
  console.log(`phase5 live card open while streaming ${sawOpen ? 'PASS' : 'FAIL'}`)
  if (!sawOpen) fails++
}

// P5d: after agent_finished it reads "Thought" and sits above its bubble.
{
  let closed = true
  try {
    await page.waitForFunction((want) => {
      const cards = [...document.querySelectorAll('.thinking-card')]
      const last = cards[cards.length - 1]
      return (
        cards.length === want &&
        last?.querySelector('.thinking-card-label')?.textContent?.trim() === 'Thought'
      )
    }, cardsBefore + 1, { timeout: 15000 })
  } catch {
    closed = false
  }
  console.log(`phase5 live card closes to "Thought" ${closed ? 'PASS' : 'FAIL'}`)
  if (!closed) fails++

  const order = await cardAboveBubble()
  console.log(`phase5 live card above its bubble ${JSON.stringify(order)} ${order.ok ? 'PASS' : 'FAIL'}`)
  if (!order.ok) fails++
}

// P5e: reload replays the same cards; the live card's body is byte-identical
// to the replayed one, still above its bubble.
{
  const live = page.locator('.thinking-card').last()
  await live.locator('.thinking-card-header').click()
  const liveBody = await live.locator('.thinking-card-pre').textContent()
  const okLive = liveBody.startsWith('live thought:')
  console.log(`phase5 live card body is the streamed reasoning ${okLive ? 'PASS' : `FAIL (${JSON.stringify(liveBody)})`}`)
  if (!okLive) fails++

  await page.reload()
  let replayed = null
  let count = -1
  try {
    await page.waitForFunction(
      (want) => document.querySelectorAll('.thinking-card').length === want,
      cardsBefore + 1,
      { timeout: 15000 },
    )
    const card = page.locator('.thinking-card').last()
    await card.locator('.thinking-card-header').click()
    replayed = await card.locator('.thinking-card-pre').textContent()
  } catch {
    // replayed stays null — the mismatch below reports it
  }
  count = await cardCount()
  const ok = count === cardsBefore + 1 && replayed === liveBody
  console.log(
    `phase5 reload replays the card byte-identically (count=${count}) ${ok ? 'PASS' : `FAIL (${JSON.stringify(replayed)})`}`,
  )
  if (!ok) fails++

  const order = await cardAboveBubble()
  console.log(`phase5 replayed card above its bubble ${JSON.stringify(order)} ${order.ok ? 'PASS' : 'FAIL'}`)
  if (!order.ok) fails++
}

await browser.close()
process.exit(fails ? 1 : 0)
