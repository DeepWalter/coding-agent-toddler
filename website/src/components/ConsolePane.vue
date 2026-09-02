<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Block } from '../types'
import { renderMarkdown } from '../markdown'
import MessageBubble from './MessageBubble.vue'
import PlanCard from './PlanCard.vue'
import ToolCard from './ToolCard.vue'

const props = defineProps<{ blocks: Block[] }>()
const emit = defineEmits<{
  'approve-plan': [planId: string, mode: 'manual' | 'auto']
  'reject-plan': [planId: string, feedback: string]
  'open-file': [path: string]
}>()

const scroller = ref<HTMLElement | null>(null)
const atBottom = ref(true)

function onScroll() {
  const el = scroller.value
  if (!el) return
  // Within 40px of the bottom counts as pinned to the bottom.
  atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

function scrollToBottom() {
  const el = scroller.value
  if (el && atBottom.value) el.scrollTop = el.scrollHeight
}

// Each reducer pass returns a new blocks array, so identity change fires
// on every frame — streaming text stays pinned while the user is at the
// bottom, and doesn't yank the scrollbar while they scroll up.
watch(() => props.blocks, async () => {
  await nextTick()
  scrollToBottom()
})

// Growing the input card writes a taller --bar-h, which grows this scroller's
// bottom padding and shrinks its content box — a ResizeObserver report with
// an unchanged border box.  That must NOT scroll the console: the card is an
// overlay, so while the user composes it may cover the tail instead of
// shoving the output up (typing never moves what's on screen, pinned or not).
// Streaming content grows the scroll height without resizing this box, so
// the blocks watch above stays the pin mechanism and keeps the newest line
// just above the card.  Re-pin only when the scroller's own height changed
// (border box) — pane resize, the paused strip toggling — keeping the bottom
// edge glued for the user sitting at it.
let boxObserver: ResizeObserver | null = null
let lastBoxHeight = 0

onMounted(() => {
  const el = scroller.value
  if (!el) return
  // Seed with the current height so the first border-box report (barring
  // sub-pixel drift) reads as no change instead of an unrequested scroll.
  lastBoxHeight = el.getBoundingClientRect().height
  boxObserver = new ResizeObserver((entries) => {
    const blockSize = entries[0]?.borderBoxSize[0]?.blockSize
    if (typeof blockSize !== 'number' || Math.abs(blockSize - lastBoxHeight) < 0.5) {
      return
    }
    lastBoxHeight = blockSize
    if (atBottom.value) scrollToBottom()
  })
  boxObserver.observe(el)
})
onBeforeUnmount(() => boxObserver?.disconnect())
</script>

<template>
  <div ref="scroller" class="console-pane" @scroll="onScroll">
    <template v-for="block in blocks" :key="block.id">
      <MessageBubble
        v-if="block.kind === 'user'"
        :role="'user'"
        :text="block.text"
      />
      <MessageBubble
        v-else-if="block.kind === 'assistant'"
        :role="'assistant'"
        :text="block.text"
        @open-file="(path) => emit('open-file', path)"
      />
      <ToolCard v-else-if="block.kind === 'tool'" :block="block" />
      <PlanCard
        v-else-if="block.kind === 'plan'"
        :block="block"
        @approve="(planId, mode) => emit('approve-plan', planId, mode)"
        @reject="(planId, feedback) => emit('reject-plan', planId, feedback)"
      />
      <div v-else-if="block.kind === 'error'" class="stream-line error">
        ⛔ {{ block.message }}
      </div>
      <!-- Notices are slash-command output — markdown by contract
           (the server sends it straight through). -->
      <div
        v-else
        class="stream-line notice markdown"
        v-html="renderMarkdown(block.message)"
      ></div>
    </template>
    <div v-if="!blocks.length" class="console-empty">
      No messages yet — describe a task below.
    </div>
  </div>
</template>
