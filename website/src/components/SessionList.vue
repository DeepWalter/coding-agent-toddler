<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api } from '../api'
import type { SessionInfo, SessionSummary } from '../types'

const props = defineProps<{
  current: SessionInfo | null
  connected: boolean
}>()

const emit = defineEmits<{ select: [sessionId: string] }>()

const sessions = ref<SessionSummary[]>([])
const selected = ref('')

async function refresh() {
  try {
    const res = await api.sessions()
    sessions.value = res.sessions
    selected.value = props.current?.id ?? ''
  } catch {
    // Keep the stale list; the next hello/connect retries.
  }
}

// Track the active session: hello on connect and every switch.  A session
// activated outside this tab also gets pulled into the list.
watch(
  () => props.current?.id,
  () => {
    const current = props.current
    selected.value = current?.id ?? ''
    if (current && !sessions.value.some((s) => s.id === current.id)) {
      refresh()
    }
  },
)

watch(
  () => props.connected,
  (connected) => {
    if (connected) refresh()
  },
)

onMounted(refresh)

function onChange(event: Event) {
  const id = (event.target as HTMLSelectElement).value
  if (id) emit('select', id)
}
</script>

<template>
  <label class="session-select" title="Switch session">
    <select
      :value="selected"
      :disabled="!connected"
      @change="onChange"
    >
      <option v-for="s in sessions" :key="s.id" :value="s.id">
        {{ s.title || s.id.slice(0, 8) }} — {{ s.message_count }} msg{{ s.message_count === 1 ? '' : 's' }}
      </option>
    </select>
  </label>
</template>
