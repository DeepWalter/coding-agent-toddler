<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { MAX_TITLE_LENGTH } from '../types'

/** The console pane's title row — the live conversation's title, editable
 *  in place.  It renders on the same row as the explorer's and the source
 *  control panel's headers (see .console-header in styles.css). */
const props = defineProps<{
  title: string
  /** False with no conversation to retitle (before the first hello) or with
   *  the socket down — the title then renders as a disabled button: no
   *  hover highlight, no pen, and the click that would edit it does nothing
   *  instead of sending into a void. */
  editable: boolean
}>()

const emit = defineEmits<{ rename: [title: string] }>()

const editing = ref(false)
const draft = ref('')
const inputEl = ref<HTMLInputElement | null>(null)

async function startEdit() {
  draft.value = props.title
  editing.value = true
  await nextTick()
  // Select what is there: replacing a wrong title is the whole point of
  // clicking it, and a click that meant to append can still hit End.
  inputEl.value?.select()
}

/** Enter and blur both commit — clicking away saves, it does not discard. */
function commit() {
  if (!editing.value) return
  editing.value = false
  const title = draft.value.trim()
  // Nothing typed, or nothing changed: leave the server alone.  An empty
  // box is not a title (the server refuses one), so it reverts rather than
  // clearing — a cleared title would be re-derived from the next turn's
  // first words, which is not what clearing one looks like it does.
  if (!title || title === props.title) return
  emit('rename', title)
}

function cancel() {
  editing.value = false
}
</script>

<template>
  <header class="console-header">
    <!-- The sizer is the box's width: in flow, invisible, holding the same
         text in the same font, with the input laid over it — so the box
         hugs the title instead of filling the row, and grows as it is
         typed into. -->
    <span v-if="editing" class="console-title-field">
      <span class="console-title-sizer" aria-hidden="true">{{ draft }}</span>
      <input
        ref="inputEl"
        v-model="draft"
        class="console-title-input"
        type="text"
        :maxlength="MAX_TITLE_LENGTH"
        aria-label="Conversation title"
        @keydown.enter.prevent="commit"
        @keydown.esc.prevent="cancel"
        @blur="commit"
      />
    </span>
    <button
      v-else
      type="button"
      class="console-title"
      :disabled="!editable"
      :title="editable ? `${title} — click to rename` : title"
      @click="startEdit"
    >
      <span class="console-title-text">{{ title }}</span>
      <!-- Rides at the end of the block, always in flow and only faded in
           on hover, so revealing it moves nothing. -->
      <svg
        class="console-title-pen"
        viewBox="0 0 24 24"
        width="11"
        height="11"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
      </svg>
    </button>
  </header>
</template>
