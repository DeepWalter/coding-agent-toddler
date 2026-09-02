<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import ConsoleDock from './components/ConsoleDock.vue'
import ConsolePane from './components/ConsolePane.vue'
import FileEditor from './components/FileEditor.vue'
import FileExplorer from './components/FileExplorer.vue'
import SessionList from './components/SessionList.vue'
import SourceControl from './components/SourceControl.vue'
import StatusBar from './components/StatusBar.vue'
import { useConsole } from './composables/useConsole'
import { useGitStatus } from './composables/useGitStatus'
import { useWebSocket } from './composables/useWebSocket'
import type { Block, Mode, TabEntry } from './types'
import { tabKey } from './utils'

// Transport → state: every websocket frame goes through the console
// reducer; the connection refs drive the status bar and input gating.
// onFrame stores a SINGLE handler, so the git-status hook rides the
// same fan-out — frames that change the working tree invalidate badges.
const { connected, connecting, send, onFrame } = useWebSocket()
const {
  state,
  applyFrame,
  sendTurn,
  cancelTurn,
  approveTool,
  denyTool,
  approvePlan,
  rejectPlan,
  setMode,
  newConversation,
  switchSession,
} = useConsole(send)
const git = useGitStatus()
onFrame((frame) => {
  applyFrame(frame)
  git.onFrame(frame)
})
void git.refresh()

// The gate the pill falls back to when no plan is on.  Plan is not a
// gating mode — it flags the next turn to run in plan mode (gating drops
// to manual) — so the pill shows the gate, except while mode_label
// reports PLAN (a plan armed while idle, or a plan turn in flight), when
// it displays "plan" instead.  In the dropdown a plan armed on an idle
// agent is itself a selectable state: picking manual/auto there leaves
// plan mode (the server clears the flag).
const pillMode = computed<Mode>(() =>
  state.session?.permission_mode === 'auto' ? 'auto' : 'manual',
)
const inPlan = computed(() =>
  (state.session?.mode_label ?? '').toUpperCase() === 'PLAN',
)

// The plan block awaiting THIS tab's decision, if any — the console dock
// floats its decision bar over the input bar while it exists.  busy is
// required: an undecided plan while idle keeps its inline actions and must
// not float (the user can still type).  Only the LAST plan block counts —
// at most one undecided plan exists in practice (rejected/approved blocks
// are decided, new proposals supersede decided ones).
const awaitingPlan = computed<Extract<Block, { kind: 'plan' }> | null>(() => {
  if (!state.busy) return null
  for (let i = state.blocks.length - 1; i >= 0; i--) {
    const b = state.blocks[i]
    if (b.kind !== 'plan') continue
    if (b.decision === null && b.steps.every(([, , st]) => st === 'pending')) return b
    return null // a later, decided plan supersedes any earlier undecided one
  }
  return null
})
const awaitingPlanId = computed(() => awaitingPlan.value?.id ?? null)
// True while the console dock floats an ask (tool gate or awaiting plan)
// over the input bar — the console pins to the bottom then, so the content
// being asked about sits right above the card.
const askVisible = computed(() => state.paused !== null || awaitingPlanId.value !== null)

// Three split panes: explorer | editor | console.  Each divider drags its
// leading pane's width as a % of the split width, clamped cross-wise so
// every pane keeps MIN_PANE% — the console takes whatever's left.
// Pointer capture keeps the drag going outside the divider.
const MIN_PANE = 12
const splitEl = ref<HTMLElement | null>(null)
const activityBarEl = ref<HTMLElement | null>(null)
const drag = ref<'explorer' | 'editor' | null>(null)

// localStorage is unreliable (private mode, quota) and stores untrusted
// strings — every read and write is guarded, bad values fall back.
function readStored<T>(key: string, parse: (raw: string) => T | null, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    return parse(raw) ?? fallback
  } catch {
    return fallback
  }
}

function writeStored(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // storage unavailable (private mode, quota) — the value just won't persist
  }
}

// Color theme: tokyo-night (dark, default) or github-light, applied as
// named <html data-theme> values matching the CSS theme blocks.  The choice
// persists in localStorage and every var(--*) in styles.css — including
// the CodeMirror theme, which reads the same variables — follows the
// switch.  Stored values from earlier lineups ('"dark"'/'"light"', then
// '"solarized-dark"'/'"solarized-light"') map to the current pair;
// anything else falls back to dark.
const THEME_STORAGE_KEY = 'tod.theme'
const theme = ref<'tokyo-night' | 'github-light'>(
  readStored<'tokyo-night' | 'github-light'>(
    THEME_STORAGE_KEY,
    (raw) => {
      switch (raw) {
        case '"tokyo-night"':
        case '"solarized-dark"':
        case '"dark"':
          return 'tokyo-night'
        case '"github-light"':
        case '"solarized-light"':
        case '"light"':
          return 'github-light'
        default:
          return null
      }
    },
    'tokyo-night',
  ),
)
watch(
  theme,
  (t) => {
    document.documentElement.dataset.theme = t
    // Persist on change — and on boot, which also repairs an absent or
    // invalid entry back to a valid stored value.
    writeStored(THEME_STORAGE_KEY, t)
  },
  { immediate: true },
)

// The split widths persist in localStorage, written on drag end (not every
// pointermove) and restored on load.  Invalid or unparseable values fall
// back to the defaults; stored widths are re-clamped against MIN_PANE in
// case the limits changed between sessions.
const SPLIT_STORAGE_KEY = 'tod.split'
const DEFAULT_SPLIT = { explorer: 14, editor: 44 }

const split = readStored(
  SPLIT_STORAGE_KEY,
  (raw) => {
    const parsed = JSON.parse(raw) as { explorer?: unknown; editor?: unknown }
    if (typeof parsed?.explorer !== 'number' || typeof parsed?.editor !== 'number') return null
    return {
      explorer: Math.min(100 - parsed.editor - MIN_PANE, Math.max(MIN_PANE, parsed.explorer)),
      editor: Math.min(100 - parsed.explorer - MIN_PANE, Math.max(MIN_PANE, parsed.editor)),
    }
  },
  { ...DEFAULT_SPLIT },
)

function saveSplit() {
  writeStored(SPLIT_STORAGE_KEY, { explorer: explorerPct.value, editor: editorPct.value })
}

const explorerPct = ref(split.explorer)
const editorPct = ref(split.editor)

// Activity bar: which panel occupies the left pane ('explorer' | 'sc'),
// or null when the pane is closed (clicking the active icon toggles it,
// VSCode-style).  Persisted — an accidentally closed pane should not
// reset the layout every reload.
const LEFTPANE_STORAGE_KEY = 'tod.leftpane'
type LeftPane = 'explorer' | 'sc'
const leftPane = ref<LeftPane | null>(
  readStored<LeftPane | null>(
    LEFTPANE_STORAGE_KEY,
    (raw) => (raw === '"sc"' || raw === '"explorer"' ? JSON.parse(raw) : null),
    'explorer',
  ),
)

function selectPane(kind: LeftPane) {
  leftPane.value = leftPane.value === kind ? null : kind
  writeStored(LEFTPANE_STORAGE_KEY, leftPane.value)
}

// Collapse/expand must not resize the console: the left pane's width is
// taken from — or given back to — the editor, never from the console's
// share (the console keeps exactly the width its divider drag set).  The
// clamps only bind when the editor has no room left to absorb/give up.
// Switching which panel occupies the open pane ('explorer' ↔ 'sc') is not
// a collapse — it must leave the widths untouched.
watch(leftPane, (pane, prev) => {
  if (!!pane === !!prev) return // open→open / closed→closed: no width moves
  const x = explorerPct.value
  editorPct.value = pane
    ? Math.max(MIN_PANE, editorPct.value - x) // reopening — pane takes back its width
    : Math.min(100 - MIN_PANE, editorPct.value + x) // collapsing — editor absorbs it
})

/** Changed-file count for the source-control icon badge. */
const scCount = computed(
  () =>
    Object.keys(git.state.sections.staged).length +
    Object.keys(git.state.sections.unstaged).length,
)

/** Unsaved-editor-files count for the explorer icon badge; FileEditor
 * reports it whenever a tab's dirty set changes. */
const dirtyCount = ref(0)

// Editor-pane tabs.  A tab is either a file (editable, persists in
// localStorage) or a diff (read-only side-by-side view, ephemeral — never
// persisted; a reload must restore only real files).  The file path list +
// active tab persist as before (content never persists — files refetch on
// reload, and a deleted file shows the tab's error state).  Persisted paths
// are trusted, not re-validated against /api/tree — that listing is
// depth-limited and would wrongly drop valid paths.  Tabs are scoped to the
// session's repo: the stored `root` (cwd) must match, or the paths resolve
// against the wrong tree (see the cwd watcher below).
const TABS_STORAGE_KEY = 'tod.tabs'
const MAX_OPEN_TABS = 50

interface PersistedTabs {
  open: string[]
  active: string | null
  root: string | null // cwd the tabs were opened against
}

const restoredTabs = readStored<PersistedTabs>(
  TABS_STORAGE_KEY,
  (raw) => {
    const parsed = JSON.parse(raw) as { open?: unknown; active?: unknown; root?: unknown }
    if (!Array.isArray(parsed.open)) return null
    const open: string[] = []
    for (const item of parsed.open) {
      if (typeof item === 'string' && item.length > 0 && !open.includes(item)) {
        open.push(item)
        if (open.length >= MAX_OPEN_TABS) break
      }
    }
    const active =
      typeof parsed.active === 'string' && open.includes(parsed.active)
        ? parsed.active
        : (open[0] ?? null)
    return { open, active, root: typeof parsed.root === 'string' ? parsed.root : null }
  },
  { open: [], active: null, root: null },
)

const openTabs = ref<TabEntry[]>(restoredTabs.open.map((p) => ({ kind: 'file', path: p })))
const activeTab = ref<TabEntry | null>(
  restoredTabs.active ? { kind: 'file', path: restoredTabs.active } : null,
)

/** The file path of the active tab, if it is a file tab — the explorer's
 * selection highlight and FileEditor's per-file machinery only care about
 * file tabs. */
const activeFilePath = computed(() =>
  activeTab.value?.kind === 'file' ? activeTab.value.path : null,
)

// Tabs belong to a repo.  If the session's cwd differs from the root the
// tabs were restored for (server restarted elsewhere, session switched),
// drop them — stale buffers for the wrong tree must never be edited back.
watch(
  () => state.session?.cwd,
  (cwd) => {
    if (!cwd || restoredTabs.root === cwd) return
    openTabs.value = []
    activeTab.value = null
    persistTabs()
  },
  { immediate: true },
)

function persistTabs() {
  writeStored(TABS_STORAGE_KEY, {
    open: openTabs.value
      .filter((t) => t.kind === 'file')
      .map((t) => t.path), // diff tabs are ephemeral — never persisted
    active: activeTab.value?.kind === 'file' ? activeTab.value.path : null,
    root: state.session?.cwd ?? null,
  })
}

/** Open a file in the editor: focus the existing tab, or add a new one. */
function openFile(path: string) {
  const existing = openTabs.value.find((t) => t.kind === 'file' && t.path === path)
  if (existing) {
    activeTab.value = existing // already open → focus the existing tab
  } else if (openTabs.value.length < MAX_OPEN_TABS) {
    activeTab.value = { kind: 'file', path }
    openTabs.value.push(activeTab.value)
  }
  // At the tab cap the file simply doesn't open (existing tabs keep working).
  persistTabs()
  void git.refresh() // the disk may have moved under us — refresh the badge
}

/** Open the side-by-side diff of *path* (staged or unstaged) as a tab. */
function openDiff(path: string, staged: boolean) {
  const entry: TabEntry = { kind: 'diff', path, staged }
  const existing = openTabs.value.find((t) => tabKey(t) === tabKey(entry))
  if (existing) {
    activeTab.value = existing
  } else if (openTabs.value.length < MAX_OPEN_TABS) {
    activeTab.value = entry
    openTabs.value.push(entry)
  }
  persistTabs()
}

function closeTab(tab: TabEntry) {
  const idx = openTabs.value.indexOf(tab)
  if (idx === -1) return
  openTabs.value.splice(idx, 1)
  if (activeTab.value === tab) {
    // Prefer the tab to the right (same index after splice), fall back left.
    activeTab.value = openTabs.value[idx] ?? openTabs.value[idx - 1] ?? null
  }
  persistTabs()
}

function onDividerDown(kind: 'explorer' | 'editor', event: PointerEvent) {
  drag.value = kind
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}

function onDividerMove(event: PointerEvent) {
  if (!drag.value || !splitEl.value) return
  const rect = splitEl.value.getBoundingClientRect()
  // The panes begin after the fixed-width activity bar, but their widths
  // are percentages of the whole split.  The cursor must therefore be
  // measured against the pane region — otherwise the divider lands one
  // activity-bar-width to the right of the cursor the moment the drag
  // starts.
  const barWidth = activityBarEl.value?.getBoundingClientRect().width ?? 0
  const pct = ((event.clientX - rect.left - barWidth) / rect.width) * 100
  if (drag.value === 'explorer') {
    explorerPct.value = Math.min(100 - editorPct.value - MIN_PANE, Math.max(MIN_PANE, pct))
  } else {
    // The editor's right edge is the dragged divider, so its width is the
    // mouse position minus the left pane's width — not the raw position.
    // A closed pane occupies nothing (its stored width stays for reopen).
    const left = leftPane.value ? explorerPct.value : 0
    editorPct.value = Math.min(
      100 - left - MIN_PANE,
      Math.max(MIN_PANE, pct - left),
    )
  }
}

function onDividerUp(event: PointerEvent) {
  drag.value = null
  ;(event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId)
  saveSplit()
}
</script>

<template>
  <div class="app">
    <header class="topbar">
      <span class="status-dot" :class="connected ? 'on' : 'off'" />
      <span class="topbar-title">tod</span>
      <span v-if="state.session" class="topbar-meta">{{ state.session.cwd }}</span>
      <div class="topbar-actions">
        <button
          type="button"
          class="theme-toggle"
          :title="theme === 'tokyo-night' ? 'Switch to light theme' : 'Switch to dark theme'"
          @click="theme = theme === 'tokyo-night' ? 'github-light' : 'tokyo-night'"
        >
          <!-- dark theme → sun (switch to light); light theme → moon (switch to dark) -->
          <svg
            v-if="theme === 'tokyo-night'"
            viewBox="0 0 24 24"
            width="14"
            height="14"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
          >
            <circle cx="12" cy="12" r="4" />
            <path
              d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"
            />
          </svg>
          <svg
            v-else
            viewBox="0 0 24 24"
            width="14"
            height="14"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
          </svg>
        </button>
        <SessionList
          :current="state.session"
          :connected="connected"
          @select="switchSession"
        />
        <button
          type="button"
          class="btn small"
          :disabled="!connected || state.busy"
          title="Start a new conversation (archives the current one)"
          @click="newConversation"
        >
          ＋ New
        </button>
      </div>
    </header>

    <div ref="splitEl" class="split">
      <nav ref="activityBarEl" class="activity-bar" aria-label="sidebar">
        <button
          type="button"
          class="activity-btn"
          :class="{ active: leftPane === 'explorer' }"
          title="Explorer"
          @click="selectPane('explorer')"
        >
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
            <path d="M3 10h18" />
          </svg>
          <span v-if="dirtyCount" class="activity-badge" title="unsaved files">{{ dirtyCount }}</span>
        </button>
        <button
          type="button"
          class="activity-btn"
          :class="{ active: leftPane === 'sc' }"
          title="Source Control"
          @click="selectPane('sc')"
        >
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="6" cy="6" r="2" />
            <circle cx="6" cy="18" r="2" />
            <circle cx="18" cy="8" r="2" />
            <path d="M6 8v8" />
            <path d="M18 10c0 5-8 3-8 8" />
          </svg>
          <span v-if="scCount" class="activity-badge" title="changed files">{{ scCount }}</span>
        </button>
      </nav>

      <aside v-if="leftPane" class="pane pane-left" :style="{ width: explorerPct + '%' }">
        <FileExplorer
          v-if="leftPane === 'explorer'"
          :root="state.session?.cwd ?? null"
          :active-path="activeFilePath"
          :git="git.state"
          @open-file="openFile"
          @refresh-git="git.scheduleRefresh"
        />
        <SourceControl
          v-else
          :git="git.state"
          @open-diff="openDiff"
          @refresh-git="git.scheduleRefresh"
        />
      </aside>

      <div
        v-if="leftPane"
        class="divider"
        :class="{ dragging: drag === 'explorer' }"
        @pointerdown="onDividerDown('explorer', $event)"
        @pointermove="onDividerMove"
        @pointerup="onDividerUp"
      />

      <section class="pane pane-editor" :style="{ width: editorPct + '%' }">
        <FileEditor
          :files="openTabs"
          :active="activeTab"
          :git="git.state"
          @activate-tab="(t) => (activeTab = t)"
          @close-tab="closeTab"
          @open-file="openFile"
          @file-saved="git.scheduleRefresh"
          @dirty-count="(n) => (dirtyCount = n)"
          @refresh-git="git.scheduleRefresh"
        />
      </section>

      <div
        class="divider"
        :class="{ dragging: drag === 'editor' }"
        @pointerdown="onDividerDown('editor', $event)"
        @pointermove="onDividerMove"
        @pointerup="onDividerUp"
      />

      <section class="pane pane-console">
        <div class="console-wrap">
          <ConsolePane
            :blocks="state.blocks"
            :awaiting-plan-id="awaitingPlanId"
            :ask-visible="askVisible"
            @approve-plan="approvePlan"
            @reject-plan="rejectPlan"
            @open-file="openFile"
          />
        </div>
        <!-- One floating card occupies the bottom slot: the tool gate, an
             awaiting plan's decision bar, or the input bar.  The dock is
             positioned against this pane and reports --bar-h to it. -->
        <ConsoleDock
          :paused="state.paused"
          :awaiting-plan="awaitingPlan"
          :busy="state.busy"
          :connected="connected"
          :mode="pillMode"
          :gating-editable="state.session?.gating_editable ?? false"
          :in-plan="inPlan"
          :model="state.session?.model ?? ''"
          :context-pct="state.session?.context_usage_pct ?? 0"
          @send="sendTurn"
          @cancel="cancelTurn"
          @set-mode="setMode"
          @approve-tool="approveTool"
          @deny-tool="denyTool"
          @approve-plan="approvePlan"
          @reject-plan="rejectPlan"
        />
      </section>
    </div>

    <StatusBar
      :state="state"
      :connected="connected"
      :connecting="connecting"
      :git="git.state"
    />
  </div>
</template>
