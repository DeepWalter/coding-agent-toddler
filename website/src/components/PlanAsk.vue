<script setup lang="ts">
import { nextTick, ref } from 'vue'
import type { Block } from '../types'

// Compact decision bar for a plan awaiting approval, shown in the console
// dock.  Keyed by the plan block id where it's used, so every new or
// re-restored ask mounts fresh.  The full plan card stays in the transcript
// (read-only while awaited); this bar carries the decision, which the reducer
// records optimistically — the card unmounts the moment it lands.
const props = defineProps<{ block: Extract<Block, { kind: 'plan' }>; busy: boolean }>()
const emit = defineEmits<{
  approve: [planId: string, mode: 'manual' | 'auto']
  reject: [planId: string, feedback: string]
  cancel: []
}>()

const pending = ref(false)
const showFeedback = ref(false)
const feedback = ref('')
const approveEl = ref<HTMLButtonElement | null>(null)
const feedbackEl = ref<HTMLTextAreaElement | null>(null)

nextTick(() => approveEl.value?.focus())

function approve(mode: 'manual' | 'auto') {
  if (pending.value) return
  pending.value = true
  emit('approve', props.block.plan.id, mode)
}

function submitReject() {
  if (pending.value) return
  pending.value = true
  emit('reject', props.block.plan.id, feedback.value.trim())
}

function openFeedback() {
  showFeedback.value = true
  nextTick(() => feedbackEl.value?.focus())
}
</script>

<template>
  <div class="plan-ask">
    <div class="plan-ask-head">
      <span class="plan-ask-title">
        <span class="plan-ask-label">Plan — </span>{{ block.plan.title }}
      </span>
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
    <div v-if="block.plan.summary" class="plan-ask-summary">
      {{ block.plan.summary }}
      <span class="plan-ask-meta">~{{ block.plan.estimated_files_touched }} file(s) touched</span>
    </div>

    <div v-if="!showFeedback" class="plan-ask-actions">
      <button
        ref="approveEl"
        type="button"
        class="btn primary"
        :disabled="pending"
        @click="approve('manual')"
      >
        Approve
      </button>
      <button type="button" class="btn" :disabled="pending" @click="approve('auto')">
        Approve + auto
      </button>
      <button type="button" class="btn danger" :disabled="pending" @click="openFeedback">
        Deny
      </button>
    </div>

    <div v-else class="plan-card-feedback">
      <textarea
        ref="feedbackEl"
        v-model="feedback"
        rows="2"
        placeholder="Feedback — the agent re-explores with a revised plan"
      />
      <div class="plan-ask-actions">
        <button type="button" class="btn danger" @click="submitReject">Reject with feedback</button>
        <button type="button" class="btn" @click="showFeedback = false">Back</button>
      </div>
    </div>
  </div>
</template>
