<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { Block } from '../types'
import MessageBubble from './MessageBubble.vue'
import ToolCard from './ToolCard.vue'

const props = defineProps<{ blocks: Block[] }>()

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
      <section v-else-if="block.kind === 'plan'" class="plan-block">
        <div class="plan-block-title">Plan — {{ block.plan.title }}</div>
        <div v-if="block.plan.summary" class="plan-block-summary">{{ block.plan.summary }}</div>
        <ol class="plan-block-steps">
          <li v-for="[id, description, status] in block.steps" :key="id">
            <span class="step-status" :class="`status-${status}`">{{ status }}</span>
            {{ description }}
          </li>
        </ol>
        <div v-if="!block.steps.length" class="plan-block-note">steps tracked as the plan runs…</div>
      </section>
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
