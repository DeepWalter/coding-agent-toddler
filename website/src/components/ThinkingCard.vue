<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Block } from '../types'

const props = defineProps<{ block: Extract<Block, { kind: 'thinking' }> }>()

// Expanded/collapsed is display state, not transcript state — it lives
// here, never in the block, so reducer passes stay purely data.
const expanded = ref(false)

const label = computed(() => (props.block.open ? 'Thinking…' : 'Thought'))
</script>

<template>
  <div class="thinking-card" :data-expanded="expanded">
    <button class="thinking-card-header" type="button" @click="expanded = !expanded">
      <span class="thinking-card-icon" aria-hidden="true">💭</span>
      <span class="thinking-card-label">{{ label }}</span>
      <span class="thinking-card-chevron">{{ expanded ? '▾' : '▸' }}</span>
    </button>
    <!-- Plain text, never markdown: chain-of-thought is freeform prose that
         markdown would mangle.  The full text renders — it is bounded
         upstream by the request's max_tokens. -->
    <div v-if="expanded" class="thinking-card-body">
      <pre class="thinking-card-pre">{{ block.reasoning }}</pre>
    </div>
  </div>
</template>
