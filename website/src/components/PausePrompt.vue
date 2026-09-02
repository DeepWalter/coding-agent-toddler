<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { Paused } from '../types'

const props = defineProps<{ paused: Paused; busy: boolean }>()
const emit = defineEmits<{ approve: []; deny: []; cancel: [] }>()

const pending = ref(false)

watch(() => props.paused, () => {
  pending.value = false
})

const showApprove = () => props.paused.choices == null || props.paused.choices.includes('approve')
const showDeny = () => props.paused.choices == null || props.paused.choices.includes('deny')

function choose(action: 'approve' | 'deny') {
  if (pending.value || !props.paused.tool_id) return
  pending.value = true
  if (action === 'approve') emit('approve')
  else emit('deny')
}

// Keyboard focus follows the card swap: land on the first actionable button.
const approveEl = ref<HTMLButtonElement | null>(null)
const denyEl = ref<HTMLButtonElement | null>(null)
nextTick(() => {
  if (showApprove() && props.paused.tool_id) approveEl.value?.focus()
  else if (showDeny() && props.paused.tool_id) denyEl.value?.focus()
})
</script>

<template>
  <div class="pause-prompt">
    <div class="pause-prompt-head">
      <span class="pause-prompt-icon" aria-hidden="true">⚠</span>
      <span class="pause-prompt-title">{{ paused.prompt }}</span>
      <button
        v-if="busy"
        type="button"
        class="ask-close"
        title="Cancel the running turn"
        aria-label="Cancel the running turn"
        @click="emit('cancel')"
      >
        <svg
          viewBox="0 0 24 24"
          width="12"
          height="12"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          aria-hidden="true"
        >
          <path d="M18 6 6 18M6 6l12 12" />
        </svg>
      </button>
    </div>
    <span v-if="!paused.tool_id" class="pause-prompt-hint">
      (tool call not in view — reload or cancel)
    </span>
    <div class="pause-prompt-actions">
      <button
        v-if="showApprove()"
        ref="approveEl"
        type="button"
        class="btn primary"
        :disabled="pending || !paused.tool_id"
        @click="choose('approve')"
      >
        Approve
      </button>
      <button
        v-if="showDeny()"
        ref="denyEl"
        type="button"
        class="btn danger"
        :disabled="pending || !paused.tool_id"
        @click="choose('deny')"
      >
        Deny
      </button>
    </div>
  </div>
</template>
