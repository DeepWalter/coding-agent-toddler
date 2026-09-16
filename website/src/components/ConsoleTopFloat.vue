<script setup lang="ts">
import { ref } from 'vue'
import MessageBubble from './MessageBubble.vue'

// The console's top float: the input box the fold has reached, echoed over the
// pane's top edge.  ConsolePane owns *which* box that is — that part is scroll
// geometry — and whether it is open, which belongs to the block rather than to
// either reading of it; the box itself is the transcript's own user box,
// positioned out of the flow here instead of in it.
defineProps<{
  text: string
  /** Whether the box is up.  A hidden one is still in the document — the pane
   *  measures it to know where the next input ends the echo, and a box that
   *  is not rendered has no height to measure. */
  shown: boolean
  /** Whether the message is open, in this reading and in its row alike. */
  expanded: boolean
}>()

const emit = defineEmits<{
  toggle: []
}>()

// The pane measures this box to know where the next input ends the echo, so it
// has to be reachable from outside.  Exposed rather than left to `$el`: the
// comment below makes this a fragment root, and a fragment's `$el` is its first
// node — in a dev build, the comment.
const box = ref<HTMLElement | null>(null)
defineExpose({ box })
</script>

<template>
  <!-- The echo repeats a box the transcript already carries, so the copy stays
       out of the accessibility tree.  Its chip, which is inside it and is the
       one thing here that is not a repeat, is not hidden with it: a focusable
       button under aria-hidden is a trap. -->
  <div ref="box" class="console-top-float" :data-hidden="shown ? undefined : ''">
    <MessageBubble
      :role="'user'"
      :text="text"
      :expanded="expanded"
      aria-hidden="true"
      @toggle="emit('toggle')"
    />
  </div>
</template>
