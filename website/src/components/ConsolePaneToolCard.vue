<script setup lang="ts">
import { computed } from 'vue'
import type { Block, TabEntry } from '../types'
import { blockStatus } from '../blockStatus'
import { toolCard, toolTab, type ToolCardRow } from '../toolCard'
import ConsolePaneToolCardRow from './ConsolePaneToolCardRow.vue'

const props = defineProps<{ block: Extract<Block, { kind: 'tool' }> }>()
const emit = defineEmits<{
  'open-text': [tab: Extract<TabEntry, { kind: 'text' }>]
  'open-file': [path: string]
}>()

const card = computed(() => toolCard(props.block))
const state = computed(() => blockStatus(props.block))

/** The title's second slot, flattened: the file this call is about, or an
 *  empty one to fall through to the call's own description.  A shape rather
 *  than a null keeps the template free of narrowing. */
const path = computed(() => card.value.path ?? { text: '', opens: false })

function open(row: ToolCardRow) {
  emit('open-text', toolTab(props.block, card.value, row))
}

/** A title naming a file opens it — the editor is where a path is read, and
 *  a call that names one has nothing else to offer. */
function openPath() {
  emit('open-file', path.value.text)
}
</script>

<template>
  <div class="tool-call">
    <!-- The call's own line about what it does, read as ordinary output —
         the box is only what ran and what came back.  A call about a file
         names the file instead: it is the half of the title a reader can
         act on, and the one they came to check. -->
    <div class="tool-call-heading">
      <span class="tool-call-name">{{ card.name }}</span>
      <button
        v-if="path.opens"
        type="button"
        class="tool-call-path"
        :title="`Open ${path.text}`"
        @click="openPath"
      >{{ path.text }}</button>
      <!-- A path with nothing behind it yet — a write that failed — reads as
           plain text: the click would have nowhere to land. -->
      <span v-else-if="path.text" class="tool-call-path static">{{ path.text }}</span>
      <span v-else-if="card.description" class="tool-call-desc">{{ card.description }}</span>
    </div>
    <!-- A call can be its title alone, and a file call is: the file opens in
         the editor, so a row of its content here would be a worse copy of
         the pane beside it.  The box appears only when there are rows. -->
    <div v-if="card.rows.length" class="tool-card" :class="state">
      <ConsolePaneToolCardRow
        v-for="row in card.rows"
        :key="row.side"
        :row="row"
        @open="open(row)"
      />
    </div>
  </div>
</template>
