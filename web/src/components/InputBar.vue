<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{
  busy: boolean
  connected: boolean
}>()

const emit = defineEmits<{ send: [text: string]; cancel: [] }>()

const text = ref('')

function submit() {
  const trimmed = text.value.trim()
  if (!trimmed || props.busy || !props.connected) return
  emit('send', trimmed)
  text.value = ''
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    submit()
  }
}
</script>

<template>
  <div class="input-bar">
    <textarea
      v-model="text"
      class="input-bar-textarea"
      rows="2"
      placeholder="Describe a task…"
      :disabled="!connected"
      @keydown="onKeydown"
    />
    <button
      v-if="busy"
      type="button"
      class="btn danger"
      @click="emit('cancel')"
    >
      Cancel
    </button>
    <button
      v-else
      type="button"
      class="btn primary"
      :disabled="!connected || !text.trim()"
      @click="submit"
    >
      Send
    </button>
  </div>
</template>
