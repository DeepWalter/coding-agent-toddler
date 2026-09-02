// Headless repro: console scrolled to the middle → a tool gate pops → does
// the console pin to the bottom?  Prints PASS/FAIL plus diagnostics.
import { chromium } from 'playwright'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 700 } })
page.on('pageerror', (e) => console.log('[pageerror]', e.message))
page.on('console', (m) => console.log('[page]', m.text()))

const BASE = process.env.BASE_URL ?? 'http://127.0.0.1:5199/'
await page.goto(BASE)

// Wait for the seeded console content to be tall enough to scroll.
await page.waitForFunction(() => {
  const el = document.querySelector('.console-pane')
  return el && el.scrollHeight - el.clientHeight > 800
}, { timeout: 15000 })
await page.waitForTimeout(400)

// Log every scroll position from here on, so a wrong move is visible.
await page.evaluate(() => {
  const el = document.querySelector('.console-pane')
  window.__scrollLog = []
  el.addEventListener('scroll', () => window.__scrollLog.push(Math.round(el.scrollTop)))
})

const mid = await page.evaluate(() => {
  const el = document.querySelector('.console-pane')
  el.scrollTop = Math.round((el.scrollHeight - el.clientHeight) / 2)
  return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
})
console.log('scrolled to middle:', JSON.stringify(mid))
await page.waitForTimeout(300)

const before = await page.evaluate(() => {
  const inst = (sel) => {
    let n = document.querySelector(sel)
    while (n) {
      if (n.__vueParentComponent) return n.__vueParentComponent
      n = n.parentElement
    }
    return null
  }
  const dock = inst('.console-dock')
  const pane = inst('.console-pane')
  const app = dock?.parent
  const dockProps = dock?.props ?? {}
  const readVal = (i, key) => {
    try {
      const v = i?.setupState?.[key]
      return v === null || v === undefined || typeof v === 'object' && 'value' in v ? null : v
    } catch {
      return '?'
    }
  }
  return {
    scrollTop: document.querySelector('.console-pane').scrollTop,
    appPaused: !!app?.setupState?.state?.paused,
    askVisibleValue: app ? app.setupState.askVisible : 'no-app',
    sameParentPaneDock: pane?.parent === app,
    paneAwaitingPlanId: pane?.props?.awaitingPlanId,
    dockPaused: dockProps.paused ?? null,
    dockAwaitingPlan: dockProps.awaitingPlan ?? null,
  }
})
console.log('pre-gate state:', JSON.stringify(before))

await page.fill('textarea.input-bar-textarea', 'run a task')
await page.click('button:has-text("Send")')
await page.waitForSelector('.pause-prompt', { timeout: 15000 })
await page.waitForTimeout(800)

const result = await page.evaluate(() => {
  const el = document.querySelector('.console-pane')
  // Climb to the nearest Vue component instance for each key element.
  const inst = (sel) => {
    let n = document.querySelector(sel)
    while (n) {
      if (n.__vueParentComponent) return n.__vueParentComponent
      n = n.parentElement
    }
    return null
  }
  const dockInst = inst('.console-dock')
  const paneInst = inst('.console-pane')
  const appInst = dockInst?.parent
  return {
    scrollTop: el.scrollTop,
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
    gateVisible: !!document.querySelector('.pause-prompt'),
    logTail: window.__scrollLog.slice(-15),
    app: appInst
      ? {
          askVisible: appInst.setupState.askVisible?.value,
          paused: !!appInst.setupState.state?.paused,
          hasPaneWatch: !!appInst.subTree?.children?.some?.((c) => {
            try {
              return c.component?.type?.__name === 'ConsolePane'
            } catch {
              return false
            }
          }),
        }
      : null,
    pane: paneInst
      ? {
          name: paneInst.type?.__name,
          askVisibleProp: paneInst.props?.askVisible,
          askVisibleInitial: paneInst.props?.askVisible,
        }
      : null,
  }
})
console.log('after gate:', JSON.stringify(result, null, 1))
const pinned = result.gateVisible && result.scrollTop + result.clientHeight >= result.scrollHeight - 5
console.log(pinned ? 'PASS: pinned to bottom on gate' : 'FAIL: NOT pinned to bottom')

await browser.close()
process.exit(pinned ? 0 : 1)
