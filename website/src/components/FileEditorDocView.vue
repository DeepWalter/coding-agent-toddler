<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '../markdown'
import type { TabEntry } from '../types'

/**
 * Rendered markdown document hosted in the editor pane while a document
 * tab is active — the app holds the text (an approved plan's display
 * markdown today), so nothing is fetched and nothing is written back:
 * the pane draws the same renderer the console's prose uses.  Remounts
 * per activation (FileEditor keys it), so the scroll always starts at the
 * top of the document arriving.
 */

const props = defineProps<{
  tab: Extract<TabEntry, { kind: 'markdown' }>
}>()
const emit = defineEmits<{
  'open-file': [path: string]
}>()

const rendered = computed(() => renderMarkdown(props.tab.content))

// Delegated click handler: repo-relative links (tagged `data-file` by the
// markdown renderer) open the file in the editor pane rather than
// navigating the SPA — the same interception the message bubble makes.
// A plan's own text carries none today; anything the model wrote into a
// step description can.
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
  <div class="doc-view">
    <div class="editor-header">
      <span class="editor-path" :title="tab.title">{{ tab.title }}</span>
      <span class="editor-status">read-only · rendered markdown</span>
    </div>
    <div
      class="editor-doc markdown"
      @click="onClick"
      v-html="rendered"
    ></div>
  </div>
</template>
