<script setup lang="ts">
import type { BlockStatus } from '../blockStatus'

defineProps<{
  /** The block's gutter reading — null for user input, the one row the
   *  console leaves flush left and unmarked. */
  status: BlockStatus | null
}>()

/** One character per settled state.  Running has no glyph — the spinner
 *  takes that branch first, and its empty entry here keeps the table
 *  exhaustive over the vocabulary. */
const GLYPHS: Record<BlockStatus, string> = {
  running: '',
  ok: '✓',
  error: '✗',
  cancelled: '·',
}
</script>

<template>
  <span v-if="status" class="status-mark" :class="status">
    <!-- Only the live mark is labelled: it is the one reading a screen
         reader cannot get from the block itself.  The settled glyphs are
         decoration — the block they sit beside already says what it is,
         and a live region per row would announce the whole transcript. -->
    <span v-if="status === 'running'" class="spinner" aria-label="running" />
    <span v-else aria-hidden="true">{{ GLYPHS[status] }}</span>
  </span>
</template>
