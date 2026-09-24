<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useOverflow } from '../composables/useOverflow'
import type { ToolCardRow } from '../toolCard'

const props = defineProps<{ row: ToolCardRow }>()
const emit = defineEmits<{ open: [] }>()

// The row is clipped to three lines in CSS and measured (useOverflow): the
// clip is what decides whether a row has more to show, and therefore whether
// it is a control at all.
const textEl = ref<HTMLElement | null>(null)
const flow = useOverflow(textEl)
const canOpen = computed(() => !!props.row.text && flow.clamped.value)

// Text can change in place — a command still streaming, a result landing
// while the row is on screen — and a swap that leaves the box the same
// height raises no resize, so ask again after each change.
watch(
  () => props.row.text,
  () => nextTick(() => flow.measure()),
)

function open() {
  if (canOpen.value) emit('open')
}

// --- copy -----------------------------------------------------------------

/** Write *text* to the clipboard.  The clipboard API needs a secure context,
 *  and this app is reachable over the LAN — fall back to the textarea trick
 *  rather than failing silently on plain HTTP. */
async function writeClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const scratch = document.createElement('textarea')
  scratch.value = text // never innerHTML — the text is the model's
  scratch.setAttribute('readonly', '')
  scratch.style.position = 'fixed'
  scratch.style.top = '-1000px'
  document.body.appendChild(scratch)
  scratch.select()
  const ok = document.execCommand('copy')
  scratch.remove()
  if (!ok) throw new Error('the browser refused the copy')
}

const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | null = null
onBeforeUnmount(() => {
  if (copyTimer) clearTimeout(copyTimer)
})

async function copyText() {
  const text = props.row.text
  if (!text) return
  try {
    await writeClipboard(text)
    copied.value = true
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => {
      copied.value = false
    }, 1200)
  } catch (err) {
    // Nothing to show the reader beyond the button not changing; the reason
    // belongs in the console.
    console.warn('copying the command failed:', err)
  }
}
</script>

<template>
  <div
    class="tool-card-row"
    :data-row="row.side"
    :data-clamped="canOpen ? '' : undefined"
    :role="canOpen ? 'button' : undefined"
    :tabindex="canOpen ? 0 : undefined"
    @click="open"
    @keydown.enter.prevent="open"
    @keydown.space.prevent="open"
  >
    <div class="tool-card-section-title">{{ row.label }}</div>
    <pre
      ref="textEl"
      class="tool-card-text"
      :class="{ note: row.note, error: row.error }"
    >{{ row.text }}</pre>
    <!-- Only a row that carries something worth copying offers it: the
         command, never the output. -->
    <button
      v-if="row.copy && row.text"
      type="button"
      class="tool-card-copy"
      :class="{ copied }"
      :aria-label="copied ? 'Command copied' : 'Copy command'"
      title="Copy command"
      @click.stop="copyText"
    >
      <svg
        viewBox="0 0 24 24"
        width="13"
        height="13"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <template v-if="copied">
          <polyline points="20 6 9 17 4 12" />
        </template>
        <template v-else>
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </template>
      </svg>
    </button>
  </div>
</template>
