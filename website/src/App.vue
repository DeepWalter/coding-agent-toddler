<script setup lang="ts">
import { ref, watch } from 'vue'
import ConsolePane from './components/ConsolePane.vue'
import FileEditor from './components/FileEditor.vue'
import FileExplorer from './components/FileExplorer.vue'
import InputBar from './components/InputBar.vue'
import PausePrompt from './components/PausePrompt.vue'
import SessionList from './components/SessionList.vue'
import StatusBar from './components/StatusBar.vue'
import { useConsole } from './composables/useConsole'
import { useGitStatus } from './composables/useGitStatus'
import { useWebSocket } from './composables/useWebSocket'

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

function toggleMode() {
  const mode = state.session?.permission_mode === 'auto' ? 'manual' : 'auto'
  setMode(mode)
}

// Three split panes: explorer | editor | console.  Each divider drags its
// leading pane's width as a % of the split width, clamped cross-wise so
// every pane keeps MIN_PANE% — the console takes whatever's left.
// Pointer capture keeps the drag going outside the divider.
const MIN_PANE = 12
const splitEl = ref<HTMLElement | null>(null)
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

// Open-file tabs for the editor pane.  The path list + active tab persist
// in localStorage (content never persists — files refetch on reload, and a
// deleted file shows the tab's error state).  Persisted paths are trusted,
// not re-validated against /api/tree — that listing is depth-limited and
// would wrongly drop valid paths.  Tabs are scoped to the session's repo:
// the stored `root` (cwd) must match, or the paths resolve against the
// wrong tree (see the cwd watcher below).
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

const openFiles = ref<string[]>(restoredTabs.open)
const activePath = ref<string | null>(restoredTabs.active)

// Tabs belong to a repo.  If the session's cwd differs from the root the
// tabs were restored for (server restarted elsewhere, session switched),
// drop them — stale buffers for the wrong tree must never be edited back.
watch(
  () => state.session?.cwd,
  (cwd) => {
    if (!cwd || restoredTabs.root === cwd) return
    openFiles.value = []
    activePath.value = null
    persistTabs()
  },
  { immediate: true },
)

function persistTabs() {
  writeStored(TABS_STORAGE_KEY, {
    open: openFiles.value,
    active: activePath.value,
    root: state.session?.cwd ?? null,
  })
}

/** Open a file in the editor: focus the existing tab, or add a new one. */
function openFile(path: string) {
  if (openFiles.value.includes(path)) {
    activePath.value = path // already open → focus the existing tab
  } else if (openFiles.value.length < MAX_OPEN_TABS) {
    openFiles.value.push(path)
    activePath.value = path
  }
  // At the tab cap the file simply doesn't open (existing tabs keep working).
  persistTabs()
  void git.refresh() // the disk may have moved under us — refresh the badge
}

function closeFile(path: string) {
  const idx = openFiles.value.indexOf(path)
  if (idx === -1) return
  openFiles.value.splice(idx, 1)
  if (activePath.value === path) {
    // Prefer the tab to the right (same index after splice), fall back left.
    activePath.value = openFiles.value[idx] ?? openFiles.value[idx - 1] ?? null
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
  const pct = ((event.clientX - rect.left) / rect.width) * 100
  if (drag.value === 'explorer') {
    explorerPct.value = Math.min(100 - editorPct.value - MIN_PANE, Math.max(MIN_PANE, pct))
  } else {
    // The editor's right edge is the dragged divider, so its width is the
    // mouse position minus the explorer's width — not the raw position.
    editorPct.value = Math.min(
      100 - explorerPct.value - MIN_PANE,
      Math.max(MIN_PANE, pct - explorerPct.value),
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
        <button
          type="button"
          class="mode-toggle"
          :class="state.session?.permission_mode ?? 'manual'"
          :disabled="!connected"
          :title="`permission mode — click to switch to ${state.session?.permission_mode === 'auto' ? 'manual' : 'auto'}`"
          @click="toggleMode"
        >
          {{ state.session?.permission_mode ?? 'manual' }}
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
      <aside class="pane pane-explorer" :style="{ width: explorerPct + '%' }">
        <FileExplorer
          :root="state.session?.cwd ?? null"
          :active-path="activePath"
          :git="git.state"
          @open-file="openFile"
          @refresh-git="git.refresh"
        />
      </aside>

      <div
        class="divider"
        :class="{ dragging: drag === 'explorer' }"
        @pointerdown="onDividerDown('explorer', $event)"
        @pointermove="onDividerMove"
        @pointerup="onDividerUp"
      />

      <section class="pane pane-editor" :style="{ width: editorPct + '%' }">
        <FileEditor
          :files="openFiles"
          :active="activePath"
          :git="git.state"
          @activate-file="openFile"
          @close-file="closeFile"
          @file-saved="git.refresh"
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
            @approve-plan="approvePlan"
            @reject-plan="rejectPlan"
            @open-file="openFile"
          />
          <PausePrompt
            v-if="state.paused"
            :paused="state.paused"
            @approve="approveTool"
            @deny="denyTool"
          />
        </div>
        <InputBar
          :busy="state.busy"
          :connected="connected"
          @send="sendTurn"
          @cancel="cancelTurn"
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
