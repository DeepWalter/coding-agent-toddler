<script setup lang="ts">
import { ref, watch } from 'vue'
import type { Paused } from '../types'

const props = defineProps<{ paused: Paused }>()
const emit = defineEmits<{ approve: []; deny: [] }>()

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
</script>

<template>
  <div class="pause-prompt">
    <span class="pause-prompt-icon">⚠</span>
    <span class="pause-prompt-text">{{ paused.prompt }}</span>
    <span v-if="!paused.tool_id" class="pause-prompt-hint">(tool call not in view — reload or cancel)</span>
    <span class="pause-prompt-actions">
      <button
        v-if="showApprove()"
        type="button"
        class="btn primary"
        :disabled="pending || !paused.tool_id"
        @click="choose('approve')"
      >
        Approve
      </button>
      <button
        v-if="showDeny()"
        type="button"
        class="btn danger"
        :disabled="pending || !paused.tool_id"
        @click="choose('deny')"
      >
        Deny
      </button>
    </span>
  </div>
</template>
