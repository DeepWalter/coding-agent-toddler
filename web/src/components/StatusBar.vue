<script setup lang="ts">
import { computed } from 'vue'
import type { ConsoleState } from '../types'

const props = defineProps<{
  state: ConsoleState
  connected: boolean
  connecting: boolean
}>()

const connectionText = computed(() => {
  if (props.connected) return 'connected'
  return props.connecting ? 'connecting…' : 'reconnecting…'
})
</script>

<template>
  <footer class="status-bar">
    <span class="status-dot" :class="connected ? 'on' : 'off'" />
    <span class="status-text">{{ connectionText }}</span>
    <template v-if="state.session">
      <span class="status-sep">·</span>
      <span>{{ state.session.model }}</span>
      <span class="status-sep">·</span>
      <span>{{ state.session.mode_label }} mode</span>
      <span class="status-sep">·</span>
      <span>context {{ state.session.context_usage_pct }}%</span>
      <span v-if="state.session.title" class="status-sep">·</span>
      <span v-if="state.session.title" class="status-dim">{{ state.session.title }}</span>
    </template>
    <span v-if="state.busy" class="status-busy">● running…</span>
  </footer>
</template>
