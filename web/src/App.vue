<script setup lang="ts">
import ConsolePane from './components/ConsolePane.vue'
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
</script>

<template>
  <div class="app">
    <header class="topbar">
      <span class="status-dot" :class="connected ? 'on' : 'off'" />
      <span class="topbar-title">tod</span>
      <span v-if="state.session" class="topbar-meta">{{ state.session.cwd }}</span>
    </header>

    <main class="console-wrap">
      <ConsolePane :blocks="state.blocks" />
      <PausePrompt
        v-if="state.paused"
        :paused="state.paused"
        @approve="approveTool"
        @deny="denyTool"
      />
    </main>

    <InputBar
      :busy="state.busy"
      :connected="connected"
      @send="sendTurn"
      @cancel="cancelTurn"
    />

    <StatusBar
      :state="state"
      :connected="connected"
      :connecting="connecting"
    />
  </div>
</template>
