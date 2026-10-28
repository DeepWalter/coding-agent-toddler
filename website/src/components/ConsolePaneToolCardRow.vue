<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useOverflow } from '../composables/useOverflow'
import CopyButton from './CopyButton.vue'
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
      :class="{ error: row.error }"
    >{{ row.text }}</pre>
    <!-- Only a row that carries something worth copying offers it: the
         command, never the output.  `.stop` keeps the row's own click from
         opening the editor as well. -->
    <CopyButton
      v-if="row.copy && row.text"
      class="tool-card-copy"
      :text="row.text"
      label="Copy command"
      copied-label="Command copied"
      @click.stop
    />
  </div>
</template>
