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
// Prints PASS/FAIL plus diagnostics; exits 1 on any failure.
import { chromium } from 'playwright'

const BASE = process.env.BASE_URL ?? 'http://127.0.0.1:5199/'

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

await browser.close()
process.exit(fails ? 1 : 0)
