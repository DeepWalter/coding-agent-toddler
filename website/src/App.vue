<script setup lang="ts">
import { ref } from 'vue'
import ConsolePane from './components/ConsolePane.vue'
import FileEditor from './components/FileEditor.vue'
import FileExplorer from './components/FileExplorer.vue'
import InputBar from './components/InputBar.vue'
import PausePrompt from './components/PausePrompt.vue'
import StatusBar from './components/StatusBar.vue'
import { useConsole } from './composables/useConsole'
import { useWebSocket } from './composables/useWebSocket'

// Transport → state: every websocket frame goes through the console
// reducer; the connection refs drive the status bar and input gating.
const { connected, connecting, send, onFrame } = useWebSocket()
const { state, applyFrame, sendTurn, cancelTurn, approveTool, denyTool } = useConsole(send)
onFrame(applyFrame)

// Split pane: files on the left (explorer + editor stacked), console on
// the right.  The divider drags the left pane between 20% and 80% of the
// split width; pointer capture keeps the drag going outside the divider.
const openPath = ref<string | null>(null)
const splitPct = ref(45)
const splitEl = ref<HTMLElement | null>(null)
const dragging = ref(false)

function onDividerDown(event: PointerEvent) {
  dragging.value = true
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}

function onDividerMove(event: PointerEvent) {
  if (!dragging.value || !splitEl.value) return
  const rect = splitEl.value.getBoundingClientRect()
  const pct = ((event.clientX - rect.left) / rect.width) * 100
  splitPct.value = Math.min(80, Math.max(20, pct))
}

function onDividerUp(event: PointerEvent) {
  dragging.value = false
  ;(event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId)
}
</script>

<template>
  <div class="app">
    <header class="topbar">
      <span class="status-dot" :class="connected ? 'on' : 'off'" />
      <span class="topbar-title">tod</span>
      <span v-if="state.session" class="topbar-meta">{{ state.session.cwd }}</span>
    </header>

    <div ref="splitEl" class="split">
      <aside class="pane pane-left" :style="{ width: splitPct + '%' }">
        <FileExplorer
          :root="state.session?.cwd ?? null"
          @open-file="openPath = $event"
        />
        <FileEditor :path="openPath" />
      </aside>

      <div
        class="divider"
        :class="{ dragging }"
        @pointerdown="onDividerDown"
        @pointermove="onDividerMove"
        @pointerup="onDividerUp"
      />

      <section class="pane pane-right">
        <div class="console-wrap">
          <ConsolePane :blocks="state.blocks" />
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
