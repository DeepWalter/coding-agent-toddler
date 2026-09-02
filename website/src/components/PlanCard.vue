<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Block } from '../types'

const props = defineProps<{
  block: Extract<Block, { kind: 'plan' }>
  /** True while THIS block is the plan the console dock is asking about —
   *  the decision lives in the dock, so the card renders read-only. */
  awaiting?: boolean
}>()
const emit = defineEmits<{
  approve: [planId: string, mode: 'manual' | 'auto']
  reject: [planId: string, feedback: string]
}>()

// The decision is recorded on the block by the reducer (dock ask or inline
// card) the moment a button is clicked, so no component-local state is
// needed — and a card rebuilt from a hello snapshot that arrives with steps
// already running (the plan was approved from another tab) reads resolved
// from the steps alone and never re-offers approval.
const chosen = computed(() => props.block.decision)
const resolved = computed(
  () =>
    chosen.value !== null
    || props.block.steps.some(([, , status]) => status !== 'pending'),
)

// These are transient editor state, not decisions — local is fine.
const showFeedback = ref(false)
const feedback = ref('')

function choose(mode: 'manual' | 'auto') {
  emit('approve', props.block.plan.id, mode)
}

function submitReject() {
  emit('reject', props.block.plan.id, feedback.value.trim())
}
</script>

<template>
  <section class="plan-card">
    <div class="plan-card-title">Plan — {{ block.plan.title }}</div>
    <div v-if="block.plan.summary" class="plan-card-summary">{{ block.plan.summary }}</div>

    <ol class="plan-card-steps">
      <li v-for="[id, description, status] in block.steps" :key="id">
        <span class="step-status" :class="`status-${status}`">{{ status }}</span>
        {{ description }}
      </li>
    </ol>
    <div v-if="!block.steps.length" class="plan-card-note">
      steps tracked as the plan runs…
    </div>

    <div v-if="block.plan.rationale" class="plan-card-section">
      <div class="tool-card-section-title">Rationale</div>
      <div>{{ block.plan.rationale }}</div>
    </div>
    <div v-if="block.plan.risks && block.plan.risks.length" class="plan-card-section">
      <div class="tool-card-section-title">Risks</div>
      <ul>
        <li v-for="risk in block.plan.risks" :key="risk">{{ risk }}</li>
      </ul>
    </div>
    <div class="plan-card-note">
      ~{{ block.plan.estimated_files_touched }} file(s) touched
    </div>

    <div v-if="awaiting" class="plan-card-note">
      awaiting your decision — approve in the bar below…
    </div>
    <div v-else-if="!resolved" class="plan-card-actions">
      <button type="button" class="btn primary" @click="choose('manual')">Approve</button>
      <button type="button" class="btn" @click="choose('auto')">Approve + auto</button>
      <button type="button" class="btn danger" @click="showFeedback = true">Deny</button>
    </div>
    <div v-else class="plan-card-note">
      {{
        chosen === 'rejected'
          ? 'plan rejected'
          : chosen === 'auto'
            ? 'approved — running with auto-accept…'
            : 'approved — running…'
      }}
    </div>

    <div v-if="showFeedback && !resolved && !awaiting" class="plan-card-feedback">
      <textarea
        v-model="feedback"
        rows="2"
        placeholder="Feedback — the agent re-explores with a revised plan"
      />
      <div class="plan-card-actions">
        <button type="button" class="btn primary" @click="submitReject">Reject with feedback</button>
        <button type="button" class="btn" @click="showFeedback = false">Cancel</button>
      </div>
    </div>
  </section>
</template>
