<script setup lang="ts">
import { ref } from 'vue'
import type { Block } from '../types'

const props = defineProps<{ block: Extract<Block, { kind: 'plan' }> }>()
const emit = defineEmits<{
  approve: [planId: string, mode: 'manual' | 'auto']
  reject: [planId: string, feedback: string]
}>()

// Each plan block gets its own component instance (keyed by block id in
// the console), so `chosen` needs no reset: a re-explored plan arrives
// as a new block and a fresh card.
const chosen = ref<'manual' | 'auto' | 'reject' | null>(null)
const showFeedback = ref(false)
const feedback = ref('')

function choose(mode: 'manual' | 'auto') {
  if (chosen.value) return
  chosen.value = mode
  emit('approve', props.block.plan.id, mode)
}

function submitReject() {
  if (chosen.value) return
  chosen.value = 'reject'
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

    <div v-if="!chosen" class="plan-card-actions">
      <button type="button" class="btn primary" @click="choose('manual')">Approve</button>
      <button type="button" class="btn" @click="choose('auto')">Approve + auto</button>
      <button type="button" class="btn danger" @click="showFeedback = true">Deny</button>
    </div>
    <div v-else class="plan-card-note">
      {{
        chosen === 'reject'
          ? 'plan rejected — re-exploring…'
          : chosen === 'auto'
            ? 'approved — running with auto-accept…'
            : 'approved — running…'
      }}
    </div>

    <div v-if="showFeedback && !chosen" class="plan-card-feedback">
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
