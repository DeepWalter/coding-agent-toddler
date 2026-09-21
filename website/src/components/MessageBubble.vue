<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { renderMarkdown } from '../markdown'

const props = defineProps<{
  role: 'user' | 'assistant'
  text: string
  /** Whether the box is showing the whole message.  Owned by the pane and
   *  keyed on the block, because the transcript's row and the console's top
   *  float are one message read twice: opening either has to open both. */
  expanded?: boolean
}>()

const emit = defineEmits<{
  'open-file': [path: string]
  toggle: []
}>()

const rendered = computed(() => renderMarkdown(props.text))

// A user box is given three lines (styles.css) and asked one question: does
// the message fit them?  Measured, never inferred from the text — whether
// three lines of *this* width overflows is not something a content rule can
// know, and both the fade and the chip turn on the answer.  The transcript and
// the console's top float read the same box, in the transcript's own row and
// over the pane's top edge, so the rule lives here rather than in either.
//
// The measure is per box, not per message: it is a question about this box's
// width, and the float's box is the same width as the row's only by design.
const contentEl = ref<HTMLElement | null>(null)
const clamped = ref(false)

/** The chip outlives the overflow it was made for: a box the reader opened
 *  has nothing cut off, and still needs its way back. */
const toggle = computed(() => props.expanded || clamped.value)

let observer: ResizeObserver | null = null

function measure() {
  const el = contentEl.value
  if (el) clamped.value = el.scrollHeight - el.clientHeight > 1
}

onMounted(() => {
  measure()
  // The box is sized by its pane, so a resize reflows it — and a reflow can
  // change how much of the message fits, which a one-time measure would miss.
  observer = new ResizeObserver(measure)
  if (contentEl.value) observer.observe(contentEl.value)
})
onBeforeUnmount(() => observer?.disconnect())

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
      @click="onClick"
      v-html="rendered"
    ></div>
    <template v-else>
      <!-- User content is the only boxed text in the console — the border
           is what marks it as something the human typed.  `collapsed` is the
           three-line reading and carries the fade with it; a box that fits
           inside those three lines has no fade to show. -->
      <div
        ref="contentEl"
        class="message-content"
        :class="{ collapsed: !expanded, 'has-toggle': toggle }"
        :data-clamped="clamped && !expanded ? '' : undefined"
      >{{ text }}<button
        v-if="toggle"
        type="button"
        class="message-toggle"
        :aria-expanded="expanded"
        @click="emit('toggle')"
      >{{ expanded ? 'less ▴' : 'more ▾' }}</button></div>
    </template>
  </div>
</template>
