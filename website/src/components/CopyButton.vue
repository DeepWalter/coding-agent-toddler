<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { writeClipboard } from '../clipboard'
import { checkIcon, copyIcon } from '../copyIcon'

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
    v-html="copied ? checkIcon() : copyIcon()"
    @click="copy"
  ></button>
</template>
