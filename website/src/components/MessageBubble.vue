<script setup lang="ts">
import { computed, ref } from 'vue'
import { renderMarkdown } from '../markdown'

const props = defineProps<{
  role: 'user' | 'assistant'
  text: string
}>()

const emit = defineEmits<{
  'open-file': [path: string]
}>()

const rendered = computed(() => renderMarkdown(props.text))

// User content is immutable once pushed, so the toggle rule is content-based
// rather than measured: anything that would visibly clip when collapsed gets
// a button.
const needsToggle = computed(
  () => props.text.includes('\n') || props.text.length > 240,
)
const expanded = ref(false)

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
    <div
      v-if="role === 'assistant'"
      class="message-content markdown"
      v-html="rendered"
      @click="onClick"
    ></div>
    <template v-else>
      <!-- User content is the only boxed text in the console — the border
           is what marks it as something the human typed. -->
      <div
        class="message-content"
        :class="{ collapsed: needsToggle && !expanded, 'has-toggle': needsToggle }"
      >{{ text }}<button
        v-if="needsToggle"
        type="button"
        class="message-toggle"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >{{ expanded ? 'less ▴' : 'more ▾' }}</button></div>
    </template>
  </div>
</template>
