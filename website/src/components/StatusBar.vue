<script setup lang="ts">
import { computed } from 'vue'
import type { ConsoleState, GitStatusState } from '../types'

const props = defineProps<{
  state: ConsoleState
  connected: boolean
  connecting: boolean
  git: GitStatusState
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
    <template v-if="git.branch">
      <span class="status-dim status-branch">
        <svg
          class="status-branch-icon"
          viewBox="0 0 24 24"
          width="12"
          height="12"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <line x1="6" y1="6" x2="6" y2="15" />
          <path d="M18 9a9 9 0 0 1-9 9" />
          <circle cx="6" cy="3" r="3" />
          <circle cx="18" cy="6" r="3" />
          <circle cx="6" cy="18" r="3" />
        </svg>
        {{ git.branch }}
      </span>
    </template>
    <span v-if="state.busy" class="status-busy">● running…</span>
  </footer>
</template>
