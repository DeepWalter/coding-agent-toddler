<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { Block } from '../types'
import MessageBubble from './MessageBubble.vue'
import PlanCard from './PlanCard.vue'
import ToolCard from './ToolCard.vue'

const props = defineProps<{ blocks: Block[] }>()
const emit = defineEmits<{
  'approve-plan': [planId: string, mode: 'manual' | 'auto']
  'reject-plan': [planId: string, feedback: string]
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
      <div v-else class="stream-line notice">{{ block.message }}</div>
    </template>
    <div v-if="!blocks.length" class="console-empty">
      No messages yet — describe a task below.
    </div>
  </div>
</template>
