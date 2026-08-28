<script setup lang="ts">
import { computed } from 'vue'
import { countStatus } from '../gitStatus'
import type { ConsoleState, GitStatusState } from '../types'

const props = defineProps<{
  state: ConsoleState
  connected: boolean
  connecting: boolean
  git: GitStatusState
}>()

const gitCounts = computed(() => {
  const c = countStatus(props.git.files)
  return [
    { letter: 'M', n: c.modified, cls: 'mod' },
    { letter: 'A', n: c.added, cls: 'add' },
    { letter: 'D', n: c.deleted, cls: 'del' },
    { letter: 'R', n: c.renamed, cls: 'rename' },
    { letter: 'U', n: c.untracked, cls: 'untracked' },
    { letter: 'C', n: c.conflict, cls: 'conflict' },
  ].filter((row) => row.n > 0)
})

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
    <template v-if="git.branch">
      <span class="status-sep">·</span>
      <span class="status-dim">{{ git.branch }}</span>
      <template v-for="c in gitCounts" :key="c.letter">
        <span class="status-sep">·</span>
        <span :class="['git-count', c.cls]">{{ c.letter }} {{ c.n }}</span>
      </template>
    </template>
    <span v-if="state.busy" class="status-busy">● running…</span>
  </footer>
</template>
