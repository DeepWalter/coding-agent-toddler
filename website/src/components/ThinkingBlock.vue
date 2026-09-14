<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Block } from '../types'

const props = defineProps<{ block: Extract<Block, { kind: 'thinking' }> }>()

// Expanded/collapsed is display state, not transcript state — it lives
// here, never in the block, so reducer passes stay purely data.  A live
// block is closed too: revealing reasoning is always the user's click.
const expanded = ref(false)

const label = computed(() => (props.block.open ? 'Thinking…' : 'Thought'))
</script>

<template>
  <div class="thinking">
    <button
      class="thinking-toggle"
      type="button"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <span aria-hidden="true">💭</span>
      <span class="thinking-label">{{ label }}</span>
      <span class="thinking-chevron" aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
    </button>
    <!-- Plain text, never markdown: chain-of-thought is freeform prose that
         markdown would mangle.  The full text renders — it is bounded
         upstream by the request's max_tokens. -->
    <pre v-if="expanded" class="thinking-quote">{{ block.reasoning }}</pre>
  </div>
</template>
