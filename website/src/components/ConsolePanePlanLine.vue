<script setup lang="ts">
import type { Block, TabEntry } from '../types'
import { planDocTab } from '../utils'

const props = defineProps<{
  block: Extract<Block, { kind: 'plan' }>
}>()
const emit = defineEmits<{
  'open-doc': [tab: Extract<TabEntry, { kind: 'markdown' }>]
}>()

/** The title opens the plan's document — the same tab the proposal already
 *  auto-opened (App.vue), so a click lands on it rather than a copy. */
function open() {
  emit('open-doc', planDocTab(props.block.plan))
}
</script>

<template>
  <!-- One line, read like a file call's: "Plan" over the plan's title, the
       title opening the full plan in the editor.  The steps are not here —
       they stream as todo blocks beside it — and neither is the decision:
       the dock's ask is the one place a plan is approved. -->
  <div class="tool-call-heading">
    <span class="tool-call-name">Plan</span>
    <button
      v-if="block.plan.markdown"
      type="button"
      class="tool-call-path"
      :title="`Open the plan: ${block.plan.title}`"
      @click="open"
    >{{ block.plan.title }}</button>
    <!-- No markdown on the wire (an older server) — the title stays plain
         text: the click would have nowhere to land. -->
    <span v-else class="tool-call-path static">{{ block.plan.title }}</span>
  </div>
</template>
