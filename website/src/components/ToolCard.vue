<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Block } from '../types'

const props = defineProps<{ block: Extract<Block, { kind: 'tool' }> }>()

const expanded = ref(false)

const state = computed<'running' | 'ok' | 'error' | 'cancelled'>(() => {
  if (props.block.open) return 'running'
  const result = props.block.result
  if (!result) return 'cancelled'
  return result.success ? 'ok' : 'error'
})

const statusLabel = computed(() => {
  switch (state.value) {
    case 'running': return 'running…'
    case 'ok': return 'done'
    case 'error': return 'failed'
    case 'cancelled': return 'cancelled'
  }
})

const signature = computed(() => {
  const input = props.block.input
  const keys = Object.keys(input)
  if (!keys.length) return ''
  return `${props.block.tool_name}(${JSON.stringify(input)})`
})
</script>

<template>
  <div class="tool-card" :class="state" :data-expanded="expanded">
    <button class="tool-card-header" type="button" @click="expanded = !expanded">
      <span v-if="state === 'running'" class="spinner" aria-label="running" />
      <span v-else class="tool-card-status">{{ state === 'ok' ? '✓' : state === 'error' ? '✗' : '·' }}</span>
      <span class="tool-card-name">{{ block.tool_name }}</span>
      <span class="tool-card-state">{{ statusLabel }}</span>
      <span class="tool-card-chevron">{{ expanded ? '▾' : '▸' }}</span>
    </button>
    <div v-if="expanded" class="tool-card-body">
      <div v-if="signature" class="tool-card-section">
        <div class="tool-card-section-title">input</div>
        <pre class="tool-card-pre">{{ signature }}</pre>
      </div>
      <div class="tool-card-section">
        <div class="tool-card-section-title">{{ state === 'error' ? 'error' : 'result' }}</div>
        <pre v-if="state === 'running'" class="tool-card-pre dim">waiting for result…</pre>
        <pre v-else-if="state === 'error'" class="tool-card-pre error-text">{{ block.result?.error ?? 'tool failed' }}</pre>
        <pre v-else-if="state === 'cancelled'" class="tool-card-pre dim">cancelled before execution</pre>
        <pre v-else class="tool-card-pre">{{ block.result?.output ?? '(no output)' }}</pre>
      </div>
    </div>
  </div>
</template>
