<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import type { PromptAnswer, Question, QuestionChoice } from '../types'
import { onChoiceKeydown } from '../choiceNav'

// The ask_user card: one tab per question, one single-choice answer per
// question, one free-text Other row, and one manual submit.  The parent
// keys the card by the paused call id, so a new ask mounts a fresh card.
const props = defineProps<{
  questions: Question[]
  toolId: string
  busy: boolean
}>()
const emit = defineEmits<{
  submit: [toolId: string, answers: PromptAnswer[]]
  cancel: []
}>()

const total = props.questions.length
const answers = ref<(number | null)[]>(
  props.questions.map(() => null),
)
/** The typed Other text, one box per question, kept across tab switches. */
const texts = ref<string[]>(props.questions.map(() => ''))
const active = ref(0)

const activeQuestion = computed(() => props.questions[active.value])

/** Whether a choice row is the free-text Other row. */
function takesInput(choice: QuestionChoice | undefined): boolean {
  return choice?.takes_input === true
}

/** Whether question *index* has an answer: a choice is set and, for the
 *  Other row, the user has typed something. */
function answered(index: number): boolean {
  const choice = answers.value[index]
  if (choice == null) return false
  const row = props.questions[index]?.choices[choice]
  if (!row) return false
  return !takesInput(row) || (texts.value[index] ?? '').trim() !== ''
}

const answeredCount = computed(
  () => props.questions.filter((_, index) => answered(index)).length,
)
const allAnswered = computed(() => answeredCount.value === total)

const choicesEl = ref<HTMLElement | null>(null)
const submitEl = ref<HTMLButtonElement | null>(null)

/** Focus the first choice of the active question — where the keyboard
 *  starts, and where it lands after an answer advances the tab. */
function focusChoice() {
  nextTick(() => {
    choicesEl.value
      ?.querySelector<HTMLButtonElement>('.question-choice')
      ?.focus()
  })
}

/** Focus the active question's Other box. */
function focusOther() {
  nextTick(() => {
    choicesEl.value
      ?.querySelector<HTMLInputElement>('.question-other-input')
      ?.focus()
  })
}

/** The next question still without an answer, wrapping past the end;
 *  -1 when every question is answered. */
function nextUnanswered(after: number): number {
  for (let step = 1; step <= total; step++) {
    const index = (after + step) % total
    if (!answered(index)) return index
  }
  return -1
}

/** Move on after question *index* has its answer. */
function advance(index: number) {
  const next = nextUnanswered(index)
  if (next === -1) {
    nextTick(() => submitEl.value?.focus())
  } else {
    active.value = next
    focusChoice()
  }
}

function choose(choice: number) {
  if (!props.busy) return
  answers.value[active.value] = choice
  advance(active.value)
}

/** Clicking or focusing the Other row selects it without advancing: the
 *  answer is only complete once there are words in the box. */
function selectOther(index: number) {
  if (!props.busy) return
  answers.value[active.value] = index
}

function openOther(index: number) {
  selectOther(index)
  focusOther()
}

/** Enter in the Other box commits the typed answer and advances. */
function commitOther(index: number) {
  if (!props.busy) return
  if ((texts.value[active.value] ?? '').trim() === '') return
  answers.value[active.value] = index
  advance(active.value)
}

function openTab(index: number) {
  if (!props.busy) return
  active.value = index
  focusChoice()
}

function submit() {
  if (!props.busy || !allAnswered.value) return
  emit(
    'submit',
    props.toolId,
    answers.value.map((choice, index) => {
      const row = props.questions[index]?.choices[choice as number]
      return takesInput(row)
        ? {
          choice: choice as number,
          content: (texts.value[index] ?? '').trim(),
        }
        : { choice: choice as number }
    }),
  )
}

nextTick(focusChoice)
</script>

<template>
  <div class="question-card" tabindex="-1">
    <div class="question-card-head">
      <span class="question-card-title">Questions</span>
      <span class="question-card-progress">
        {{ answeredCount }} of {{ total }} answered
      </span>
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

    <div class="question-tabs" role="tablist" aria-label="Questions">
      <button
        v-for="(question, index) in questions"
        :key="index"
        type="button"
        role="tab"
        class="question-tab"
        :class="{ active: index === active, answered: answered(index) }"
        :aria-selected="index === active"
        @click="openTab(index)"
      >
        <span class="question-tab-title">{{ question.title }}</span>
        <span
          v-if="answered(index)"
          class="question-tab-check"
          aria-hidden="true"
        >✓</span>
      </button>
    </div>

    <div class="question-panel" role="tabpanel">
      <p class="question-content">{{ activeQuestion.content }}</p>
      <div
        ref="choicesEl"
        class="question-choices"
        @keydown="onChoiceKeydown"
      >
        <template
          v-for="(choice, index) in activeQuestion.choices"
          :key="index"
        >
          <button
            v-if="!choice.takes_input"
            type="button"
            class="question-choice"
            :class="{ 'is-chosen': answers[active] === index }"
            :aria-pressed="answers[active] === index"
            @click="choose(index)"
          >
            <span class="question-choice-mark" aria-hidden="true"></span>
            <span class="question-choice-body">
              <span class="question-choice-title">
                {{ choice.title }}
                <span v-if="index === 0" class="question-recommended">
                  Recommended
                </span>
              </span>
              <span class="question-choice-content">{{ choice.content }}</span>
            </span>
          </button>
          <div
            v-else
            class="question-choice-other"
            :class="{ 'is-chosen': answers[active] === index }"
            @click="openOther(index)"
          >
            <span class="question-choice-mark" aria-hidden="true"></span>
            <span class="question-choice-body">
              <span class="question-choice-title">{{ choice.title }}</span>
              <input
                v-model="texts[active]"
                type="text"
                class="question-other-input"
                placeholder="Type your own answer"
                :aria-label="choice.title"
                autocomplete="off"
                :disabled="!busy"
                @focus="selectOther(index)"
                @keydown.enter.exact.prevent="commitOther(index)"
              />
            </span>
          </div>
        </template>
      </div>
    </div>

    <div class="question-actions">
      <button
        ref="submitEl"
        type="button"
        class="btn primary question-submit"
        :disabled="!allAnswered || !busy"
        @click="submit"
      >
        Submit
      </button>
    </div>
  </div>
</template>
