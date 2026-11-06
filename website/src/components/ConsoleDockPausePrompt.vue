<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type {
  ChoiceLevel,
  Paused,
  PromptAnswer,
  PromptChoice,
} from '../types'
import { onChoiceFocus, onChoiceKeydown } from '../choiceNav'

const props = defineProps<{ paused: Paused; busy: boolean }>()
const emit = defineEmits<{
  answer: [answers: PromptAnswer[]]
  cancel: []
}>()

const pending = ref(false)
const feedbackOpen = ref(false)
const feedbackText = ref('')
/** The row whose free text the box is collecting, when open. */
const feedbackChoice = ref<number | null>(null)

// The gate's rows.  An ask-user pause never reaches this card (the dock
// renders ConsoleDockQuestion for it), but the type test keeps the payload
// narrowed either way.
const rows = computed<PromptChoice[]>(() => {
  const choices = props.paused.prompt.choices
  return choices.type === 'single' ? choices.data : []
})

const actionsEl = ref<HTMLElement | null>(null)
const feedbackEl = ref<HTMLTextAreaElement | null>(null)

/** The row at *level* — where the keyboard lands, and where it goes back
 *  after a mode change.  A gate may omit a level entirely. */
function rowAt(level: ChoiceLevel): HTMLButtonElement | undefined {
  const selector = `.pause-prompt-actions button[data-level="${level}"]`
  return actionsEl.value?.querySelector<HTMLButtonElement>(selector) ?? undefined
}

function focusChoice() {
  if (!props.paused.tool_id) return
  ;(rowAt('approve') ?? rowAt('deny') ?? rowAt('feedback'))?.focus()
}

function choose(row: PromptChoice, index: number) {
  if (pending.value || !props.paused.tool_id) return
  if (row.level === 'feedback') {
    // The free-text row is not an answer by itself: open the box and let
    // the user say what the model should do instead.
    feedbackChoice.value = index
    feedbackOpen.value = true
    nextTick(() => feedbackEl.value?.focus())
    return
  }
  pending.value = true
  emit('answer', [{ choice: index }])
}

function submitFeedback() {
  if (
    pending.value
    || !props.paused.tool_id
    || feedbackChoice.value === null
  ) {
    return
  }
  const text = feedbackText.value.trim()
  if (!text) return
  pending.value = true
  emit('answer', [{ choice: feedbackChoice.value, content: text }])
}

// Keyboard focus follows the card: land on the first actionable row at
// mount, and again when a new pause replaces the old one — its rows were
// disabled (and blurred) while the previous tool ran.
//
// Keyed on the call being asked about, not on the record: the console's
// reducer clones the whole state on every action, so `paused` is a new
// object after any frame at all — including the heartbeat's pong — and a
// watch on it would put the focus back in this card every 25 seconds,
// under whatever the user was reading.  A new question is what changes
// the id; a replay (`tool_id: null`) asks nothing and takes nothing.
watch(() => props.paused?.tool_id, () => {
  pending.value = false
  feedbackOpen.value = false
  feedbackText.value = ''
  feedbackChoice.value = null
  nextTick(focusChoice)
})

nextTick(focusChoice)
</script>

<template>
  <!-- tabindex so a click on the card's own body lands in the card: the
       arrows are the card's again, from the selection it remembers. -->
  <div
    class="pause-prompt"
    tabindex="-1"
    @keydown="onChoiceKeydown"
    @focusin="onChoiceFocus"
  >
    <div class="pause-prompt-head">
      <span class="pause-prompt-title">{{ paused.prompt.title }}</span>
      <button
        v-if="busy"
        type="button"
        class="ask-close"
        title="Cancel the running turn"
        aria-label="Cancel the running turn"
        @click="emit('cancel')"
      >
        <svg
          viewBox="0 0 24 24"
          width="12"
          height="12"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          aria-hidden="true"
        >
          <path d="M18 6 6 18M6 6l12 12" />
        </svg>
      </button>
    </div>
    <pre
      v-if="paused.prompt.content"
      class="pause-prompt-content"
    >{{ paused.prompt.content }}</pre>
    <span v-if="paused.prompt.description" class="pause-prompt-description">
      {{ paused.prompt.description }}
    </span>
    <span v-if="!paused.tool_id" class="pause-prompt-hint">
      (tool call not in view — reload or cancel)
    </span>
    <div ref="actionsEl" class="pause-prompt-actions">
      <button
        v-for="(row, index) in rows"
        :key="`${row.level}-${index}`"
        type="button"
        class="btn"
        :class="{
          primary: row.level === 'approve',
          danger: row.level === 'deny',
        }"
        :data-level="row.level"
        :disabled="pending || !paused.tool_id"
        @click="choose(row, index)"
      >
        {{ row.title }}
      </button>
    </div>
    <div v-if="feedbackOpen" class="pause-prompt-feedback">
      <textarea
        ref="feedbackEl"
        v-model="feedbackText"
        class="pause-prompt-feedback-input"
        rows="2"
        placeholder="Tell the model what to do instead…"
        :disabled="pending || !paused.tool_id"
        @keydown.enter.exact.prevent="submitFeedback"
      ></textarea>
      <button
        type="button"
        class="btn"
        :disabled="pending || !paused.tool_id || !feedbackText.trim()"
        @click="submitFeedback"
      >
        Send feedback
      </button>
    </div>
  </div>
</template>
