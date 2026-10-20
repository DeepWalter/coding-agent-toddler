<script setup lang="ts">
import { computed } from 'vue'
import type { Block, EditSelection, TabEntry } from '../types'
import { blockStatus } from '../blockStatus'
import { editSelection, toolBodyTab, toolCard, toolTab, type ToolCardRow } from '../toolCard'
import ConsolePaneToolCardDiff from './ConsolePaneToolCardDiff.vue'
import ConsolePaneToolCardRow from './ConsolePaneToolCardRow.vue'
import ExternalTag from './ExternalTag.vue'
import SymlinkTag from './SymlinkTag.vue'

const props = defineProps<{ block: Extract<Block, { kind: 'tool' }> }>()
const emit = defineEmits<{
  'open-text': [tab: Extract<TabEntry, { kind: 'text' }>]
  'open-file': [path: string, selection?: EditSelection]
}>()

const card = computed(() => toolCard(props.block))
const state = computed(() => blockStatus(props.block))

/** The title's second slot, flattened: the file this call is about, or an
 *  empty one to fall through to the call's own description.  A shape rather
 *  than a null keeps the template free of narrowing. */
const path = computed(
  () => card.value.path ?? { label: '', target: null, abs: '', resolved: '', external: false, symlink: false, exists: false },
)

function open(row: ToolCardRow) {
  emit('open-text', toolTab(props.block, card.value, row))
}

/** The clipped body's click: the change or content whole, in the editor,
 *  colored by its own diff grammar.  A card with no body has no such tab. */
function openBody() {
  const tab = toolBodyTab(props.block, card.value)
  if (tab) emit('open-text', tab)
}

/** A title naming a file opens it — the editor is where a path is read, and
 *  a call that names one has nothing else to offer.  What it hands over is
 *  the root-relative path, the one every pane opens a file by — or the
 *  absolute path for a file the root cannot hold, which opens read-only;
 *  the heading prints the basename of it and the hover names where the file
 *  really is.  A call that names a file with nothing behind it does not get
 *  here: the server said so, and the title reads as plain text. */
function openPath() {
  const target = path.value.target
  if (target) emit('open-file', target, editSelection(props.block) ?? undefined)
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
        v-if="path.exists"
        type="button"
        class="tool-call-path"
        :title="`Open ${path.abs}`"
        @click="openPath"
      >{{ path.label }}</button>
      <!-- A path with no file behind it — a write that never landed, a
           file since deleted — reads as plain text: the click would have
           nowhere to land.  The hover still names where the file is, which
           is what a reader wants most about one they cannot open. -->
      <span
        v-else-if="path.label"
        class="tool-call-path static"
        :title="path.abs"
      >{{ path.label }}</span>
      <span v-else-if="card.description" class="tool-call-desc">{{ card.description }}</span>
      <!-- The markers ride after the title chain, not inside it: the
           button's text stays the basename alone.  Symlink first — it
           explains the path, which is what the two that follow it are
           about — then the one that says a file outside the served root is
           named by its absolute path and opens read-only, so the basename
           is never read as the only file of that name. -->
      <SymlinkTag v-if="path.symlink" :resolved="path.resolved" />
      <ExternalTag v-if="path.external" />
    </div>
    <!-- A read can be its title alone: the file opens in the editor, so a
         row of its content here would be a worse copy of the pane beside it.
         A write and an edit are the exception — the content it wrote, or the
         change it made, is the body, drawn above the failure row when it has
         one.  The box appears only when there is something to put in it. -->
    <div v-if="card.body || card.rows.length" class="tool-card" :class="state">
      <ConsolePaneToolCardDiff
        v-if="card.body"
        :rows="card.body"
        :full-label="card.bodyKind === 'content' ? 'content' : 'diff'"
        @open="openBody"
      />
      <ConsolePaneToolCardRow
        v-for="row in card.rows"
        :key="row.side"
        :row="row"
        @open="open(row)"
      />
    </div>
  </div>
</template>
