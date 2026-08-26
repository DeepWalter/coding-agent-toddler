<script setup lang="ts">
import { ref } from 'vue'
import ConsolePane from './components/ConsolePane.vue'
import FileEditor from './components/FileEditor.vue'
import FileExplorer from './components/FileExplorer.vue'
import InputBar from './components/InputBar.vue'
import PausePrompt from './components/PausePrompt.vue'
import SessionList from './components/SessionList.vue'
import StatusBar from './components/StatusBar.vue'
import { useConsole } from './composables/useConsole'
import { useWebSocket } from './composables/useWebSocket'

// Transport → state: every websocket frame goes through the console
// reducer; the connection refs drive the status bar and input gating.
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
onFrame(applyFrame)

function toggleMode() {
  const mode = state.session?.permission_mode === 'auto' ? 'manual' : 'auto'
  setMode(mode)
}

// Three split panes: explorer | editor | console.  Each divider drags its
// leading pane's width as a % of the split width, clamped cross-wise so
// every pane keeps MIN_PANE% — the console takes whatever's left.
// Pointer capture keeps the drag going outside the divider.
const MIN_PANE = 12
const openPath = ref<string | null>(null)
const splitEl = ref<HTMLElement | null>(null)
const drag = ref<'explorer' | 'editor' | null>(null)

// The split widths persist in localStorage, written on drag end (not every
// pointermove) and restored on load.  Invalid or unparseable values fall
// back to the defaults; stored widths are re-clamped against MIN_PANE in
// case the limits changed between sessions.
const SPLIT_STORAGE_KEY = 'tod.split'
const DEFAULT_SPLIT = { explorer: 14, editor: 44 }

function loadSplit(): { explorer: number; editor: number } {
  try {
    const raw = localStorage.getItem(SPLIT_STORAGE_KEY)
    if (!raw) return { ...DEFAULT_SPLIT }
    const parsed = JSON.parse(raw)
    if (typeof parsed?.explorer !== 'number' || typeof parsed?.editor !== 'number') {
      return { ...DEFAULT_SPLIT }
    }
    return {
      explorer: Math.min(100 - parsed.editor - MIN_PANE, Math.max(MIN_PANE, parsed.explorer)),
      editor: Math.min(100 - parsed.explorer - MIN_PANE, Math.max(MIN_PANE, parsed.editor)),
    }
  } catch {
    return { ...DEFAULT_SPLIT }
  }
}

function saveSplit() {
  try {
    localStorage.setItem(
      SPLIT_STORAGE_KEY,
      JSON.stringify({ explorer: explorerPct.value, editor: editorPct.value }),
    )
  } catch {
    // storage unavailable (private mode, quota) — the split just won't persist
  }
}

const split = loadSplit()
const explorerPct = ref(split.explorer)
const editorPct = ref(split.editor)

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
          @open-file="openPath = $event"
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
        <FileEditor :path="openPath" />
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
            @open-file="openPath = $event"
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
    />
  </div>
</template>
