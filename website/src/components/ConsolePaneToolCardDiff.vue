<script setup lang="ts">
import { computed } from 'vue'
import { MAX_DIFF_ROWS, type EditDiffRow } from '../diff'

const props = defineProps<{ rows: EditDiffRow[] }>()
const emit = defineEmits<{ open: [] }>()

// The card is a glance; the editor is where a long change is read.  The
// cut is made on rows rather than by a CSS height so the footer's "more"
// count and the clip can never disagree — and a wrapped long line does not
// change how many lines the cap lets through.
const visible = computed(() => props.rows.slice(0, MAX_DIFF_ROWS))
const hidden = computed(() => Math.max(0, props.rows.length - MAX_DIFF_ROWS))
const clipped = computed(() => hidden.value > 0)

/** Only a clipped diff is a control, matching the card's rows: what fits is
 *  plain text, and what is cut says where the rest is. */
function open() {
  if (clipped.value) emit('open')
}
</script>

<template>
  <div
    class="tool-card-diff"
    :data-clamped="clipped ? '' : undefined"
    :role="clipped ? 'button' : undefined"
    :tabindex="clipped ? 0 : undefined"
    :title="clipped ? 'Open the full diff' : undefined"
    @click="open"
    @keydown.enter.prevent="open"
    @keydown.space.prevent="open"
  >
    <div
      v-for="(row, i) in visible"
      :key="i"
      class="tool-card-diff-row"
      :class="row.gap ? 'gap' : row.kind"
    >
      <span class="tool-card-diff-marker">{{ row.gap ? '⋯' : row.kind === 'del' ? '−' : row.kind === 'add' ? '+' : '' }}</span>
      <span class="tool-card-diff-text">{{ row.text }}</span>
    </div>
    <div v-if="clipped" class="tool-card-diff-more">
      ⋯ {{ hidden }} more {{ hidden === 1 ? 'line' : 'lines' }} — open full diff
    </div>
  </div>
</template>
