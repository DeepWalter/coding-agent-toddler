<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Block } from '../types'
import { renderMarkdown } from '../markdown'
import MessageBubble from './MessageBubble.vue'
import PlanCard from './PlanCard.vue'
import ToolCard from './ToolCard.vue'

const props = defineProps<{
  blocks: Block[]
  /** The plan block whose decision the console dock is asking about — its
   *  inline card renders read-only (actions live in the dock). */
  awaitingPlanId: number | null
  /** True while the console dock floats an ask (tool gate or plan decision)
   *  over the input bar. */
  askVisible: boolean
}>()
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

// When an ask (tool gate / plan decision) pops into the dock, jump the
// console to the bottom: the content it asks about (the gated tool call,
// the proposed plan) ends up above the card — even if the user had scrolled
// up.  The dock publishes its height as --bar-h in a ResizeObserver pass
// shortly after it mounts, so scroll again next frame: the tail then sits
// just above the card instead of behind it.
// The dock publishes its height as --bar-h on the pane only after layout
// (ResizeObserver), and each change grows this scroller's bottom padding —
// so after an ask pops, keep re-pinning across a few frames until that
// settles.  Once pinned, the blocks watch keeps newer output glued to the
// bottom.  Cancelled when the ask resolves (a newer watch run owns the pin).
let askPin = 0
watch(
  () => props.askVisible,
  (visible) => {
    askPin += 1
    if (!visible) return
    const pin = askPin
    let frames = 3
    const force = () => {
      if (pin !== askPin || frames-- <= 0) return
      const el = scroller.value
      if (el) el.scrollTop = el.scrollHeight // deliberate: ask wins over atBottom
      requestAnimationFrame(force)
    }
    force()
  },
)

// Growing the dock card writes a taller --bar-h, which grows this scroller's
// bottom padding and shrinks its content box — a ResizeObserver report with
// an unchanged border box.  That must NOT scroll the console: the card is an
// overlay, so while the user composes it may cover the tail instead of
// shoving the output up (typing never moves what's on screen, pinned or not).
// Streaming content grows the scroll height without resizing this box, so
// the blocks watch above stays the pin mechanism and keeps the newest line
// just above the card.  Re-pin only when the scroller's own height changed
// (border box) — pane resize — keeping the bottom edge glued for the user
// sitting at it.  (The old in-flow pause strip used to resize the wrap on
// gate toggles; confirmations now live in the floating dock, so the border
// box only changes on pane resize — gate resolution never jumps the content.)
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
        :awaiting="awaitingPlanId === block.id"
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
