<script setup lang="ts">
import { computed } from 'vue'
import type { Block, TabEntry } from '../types'
import { blockStatus } from '../blockStatus'
import { toolCard, toolTab, type ToolCardRow } from '../toolCard'
import ConsolePaneToolCardRow from './ConsolePaneToolCardRow.vue'
import ExternalTag from './ExternalTag.vue'
import SymlinkTag from './SymlinkTag.vue'

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
const path = computed(
  () => card.value.path ?? { label: '', target: null, abs: '', external: false, symlink: false, opens: false },
)

function open(row: ToolCardRow) {
  emit('open-text', toolTab(props.block, card.value, row))
}

/** A title naming a file opens it — the editor is where a path is read, and
 *  a call that names one has nothing else to offer.  What it hands over is
 *  the root-relative path, the one every pane opens a file by — or the
 *  absolute path for a file the root cannot hold, which opens read-only;
 *  the heading prints the basename of it and the hover names where the file
 *  really is. */
function openPath() {
  const target = path.value.target
  if (target) emit('open-file', target)
}
</script>

<template>
  <div class="tool-call">
    <!-- The call's own line about what it does, read as ordinary output —
         the box is only what ran and what came back.  A call about a file
         names the file instead: it is the half of the title a reader can
         act on, and the one they came to check.  Named by its basename —
         the directory it sits in is rarely the question, and the whole path
         is a hover away. -->
    <div class="tool-call-heading">
      <span class="tool-call-name">{{ card.name }}</span>
      <button
        v-if="path.opens"
        type="button"
        class="tool-call-path"
        :title="`Open ${path.abs}`"
        @click="openPath"
      >{{ path.label }}</button>
      <!-- A path with nothing behind it — a write that failed — reads as
           plain text: the click would have nowhere to land.  The hover
           still names where the file is, which is what a reader wants most
           about one they cannot open. -->
      <span
        v-else-if="path.label"
        class="tool-call-path static"
        :title="path.abs"
      >{{ path.label }}</span>
      <span v-else-if="card.description" class="tool-call-desc">{{ card.description }}</span>
      <!-- A file outside the served root is named by its absolute path and
           opens read-only — the marker says both, so the basename is never
           read as the only file of that name.  A name that reaches its file
           through a link wears its marker too: the card names one path and
           a click opens another, and without it the two read as a mistake.
           After the title chain, not inside it: the button's text stays the
           basename alone. -->
      <ExternalTag v-if="path.external" />
      <SymlinkTag v-if="path.symlink" />
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
