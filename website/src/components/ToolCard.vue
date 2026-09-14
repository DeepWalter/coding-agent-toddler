<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Block } from '../types'
import { blockStatus } from '../blockStatus'

const props = defineProps<{ block: Extract<Block, { kind: 'tool' }> }>()

const expanded = ref(false)

// The console-wide vocabulary, not a card-local one: the header's trailing
// word and the gutter's mark are two readings of the same state.
const state = computed(() => blockStatus(props.block))

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
    <!-- No status mark in the header: the gutter carries it for every
         block kind, and a second one here would read as a second state. -->
    <button class="tool-card-header" type="button" @click="expanded = !expanded">
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
