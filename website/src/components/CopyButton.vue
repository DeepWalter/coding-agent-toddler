<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { writeClipboard } from '../clipboard'

const props = withDefaults(
  defineProps<{
    text: string
    /** The button's accessible name and its hover title. */
    label?: string
    /** What it reads as while the checkmark shows. */
    copiedLabel?: string
  }>(),
  { label: 'Copy', copiedLabel: 'Copied' },
)

const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | null = null

onBeforeUnmount(() => {
  if (copyTimer) clearTimeout(copyTimer)
})

/** `click` is deliberately not an emit: the caller's `@click.stop` falls
 *  through and merges with this handler, so a button inside a row that has
 *  its own click action can copy without also opening what it sits in. */
async function copy() {
  if (!props.text) return
  try {
    await writeClipboard(props.text)
    copied.value = true
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => {
      copied.value = false
    }, 1200)
  } catch (err) {
    // Nothing to show the reader beyond the button not changing; the reason
    // belongs in the console.
    console.warn(`copying "${props.label}" failed:`, err)
  }
}
</script>

<template>
  <button
    type="button"
    class="copy-button"
    :class="{ copied }"
    :aria-label="copied ? copiedLabel : label"
    :title="label"
    @click="copy"
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
</template>
