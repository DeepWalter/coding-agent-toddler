<script setup lang="ts">
import type { ToolCardQuestion } from '../toolCard'

// The durable reading of an ask_user call: each question, its choices, and
// the chosen one marked.  This is a record, not a control — the interactive
// ask is the dock card; this card is what stays in the transcript and what
// replays after a reload.
defineProps<{ questions: ToolCardQuestion[] }>()
</script>

<template>
  <div class="tool-card-questions">
    <div
      v-for="(question, qi) in questions"
      :key="qi"
      class="tool-card-question"
    >
      <div class="tool-card-question-title">{{ question.title }}</div>
      <div class="tool-card-question-content">{{ question.content }}</div>
      <div class="tool-card-question-choices">
        <div
          v-for="(choice, ci) in question.choices"
          :key="ci"
          class="tool-card-choice"
          :class="{ 'is-chosen': question.answer === ci }"
        >
          <span class="tool-card-choice-mark" aria-hidden="true"></span>
          <div class="tool-card-choice-body">
            <div class="tool-card-choice-title">
              {{ choice.title }}
              <span v-if="ci === 0" class="tool-card-recommended">
                Recommended
              </span>
            </div>
            <div class="tool-card-choice-content">
              {{ question.answer === ci && choice.takes_input
                ? (question.answerText ?? '')
                : choice.content }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
