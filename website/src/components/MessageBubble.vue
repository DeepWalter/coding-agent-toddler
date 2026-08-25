<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '../markdown'

const props = defineProps<{
  role: 'user' | 'assistant'
  text: string
}>()

const emit = defineEmits<{
  'open-file': [path: string]
}>()

const rendered = computed(() => renderMarkdown(props.text))

// Delegated click handler: repo-relative links (tagged `data-file` by the
// markdown renderer) open the file in the editor pane instead of a tab.
function onClick(event: MouseEvent) {
  const link = (event.target as HTMLElement | null)?.closest?.('a[data-file]') as
    | HTMLAnchorElement
    | null
  if (!link) return
  event.preventDefault()
  let path = link.getAttribute('data-file') ?? ''
  try {
    path = decodeURIComponent(path)
  } catch {
    // Not valid percent-encoding — pass the href through as-is.
  }
  emit('open-file', path)
}
</script>

<template>
  <div class="message" :class="role">
    <div class="message-role">{{ role === 'user' ? 'you' : 'assistant' }}</div>
    <div
      v-if="role === 'assistant'"
      class="message-content markdown"
      :class="{ streaming: text && !text.endsWith('\n') }"
      v-html="rendered"
      @click="onClick"
    ></div>
    <div v-else class="message-content">{{ text }}</div>
  </div>
</template>
