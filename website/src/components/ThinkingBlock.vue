<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Block } from '../types'
import { renderReasoning } from '../reasoning'
import { estimateTokens, formatTokens } from '../utils'

const props = defineProps<{ block: Extract<Block, { kind: 'thinking' }> }>()

// Expanded/collapsed is display state, not transcript state — it lives
// here, never in the block, so reducer passes stay purely data.  A live
// block is closed too: revealing reasoning is always the user's click.
const expanded = ref(false)

// The clock is display state for the same reason — the reducer stays a
// pure function of the frames.  A block that arrives already closed has
// never had a start time (a replayed transcript, or a reconnect
// mid-turn), so it keeps the bare "Thought": the seconds are a live-only
// reading and deliberately do not survive a reload.  `open` closes by
// five routes — the answer starting, a tool call, the turn ending,
// a fatal error, a cancel — and watching it catches all of them.
const startedAt = props.block.open ? Date.now() : null
const elapsedMs = ref<number | null>(null)
watch(
  () => props.block.open,
  (open) => {
    if (!open && startedAt !== null && elapsedMs.value === null) {
      elapsedMs.value = Date.now() - startedAt
    }
  },
)

const label = computed(() => {
  if (props.block.open) return 'Thinking…'
  if (elapsedMs.value === null) return 'Thought'
  return `Thought for ${formatSpan(elapsedMs.value)}`
})

/** The estimated tokens so far — the duration replaces this once the
 *  thought closes, since length stops being the interesting number then. */
const detail = computed(() => {
  if (!props.block.open) return ''
  const n = estimateTokens(props.block.reasoning)
  return `${formatTokens(n)} ${n === 1 ? 'token' : 'tokens'}`
})

/** The reasoning, rendered once per delta and cached upstream: the prose
 *  stays literal, only its code is lifted out. */
const rendered = computed(() => renderReasoning(props.block.reasoning))

/** Sub-second thoughts would round to a nonsensical "0 seconds", and a
 *  long one reads better in minutes than as "125 seconds". */
function formatSpan(ms: number): string {
  const secs = Math.round(ms / 1000)
  if (secs < 1) return 'less than a second'
  if (secs < 60) return `${secs} second${secs === 1 ? '' : 's'}`
  const mins = Math.floor(secs / 60)
  const rest = secs % 60
  const minutes = `${mins} minute${mins === 1 ? '' : 's'}`
  return rest ? `${minutes} ${rest} seconds` : minutes
}
</script>

<template>
  <div class="thinking">
    <button
      class="thinking-toggle"
      type="button"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <!-- No 💭 here: the gutter's mark carries the block's status, and
           the label carries its identity. -->
      <span class="thinking-label">{{ label }}</span>
      <span v-if="detail" class="thinking-detail">· {{ detail }}</span>
      <span class="thinking-chevron" aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
    </button>
    <!-- Never markdown: chain-of-thought is freeform prose that markdown
         would mangle, so the text is escaped verbatim and only its code —
         fenced blocks and `inline` spans — is lifted out and highlighted.
         The full text renders: it is bounded upstream by max_tokens. -->
    <pre v-if="expanded" class="thinking-quote" v-html="rendered"></pre>
  </div>
</template>
