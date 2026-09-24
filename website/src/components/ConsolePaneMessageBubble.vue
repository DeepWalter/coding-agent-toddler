<script setup lang="ts">
import { computed, ref } from 'vue'
import { renderMarkdown } from '../markdown'
import { useOverflow } from '../composables/useOverflow'

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

// A user box is given three lines
// (assets/styles/components/console-pane-message-bubble.css) and asked one
// question: does the message fit them?  Both the fade and the chip turn on
// the answer, and the answer is measured rather than inferred — see
// `useOverflow`.  The transcript and the console's top float read the same
// box, in the transcript's own row and over the pane's top edge, so the
// measure is shared rather than owned by either.
const contentEl = ref<HTMLElement | null>(null)
const { clamped } = useOverflow(contentEl)

/** The chip outlives the overflow it was made for: a box the reader opened
 *  has nothing cut off, and still needs its way back. */
const toggle = computed(() => props.expanded || clamped.value)

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
