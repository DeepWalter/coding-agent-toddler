<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '../markdown'

const props = defineProps<{
  role: 'user' | 'assistant'
  text: string
}>()

const rendered = computed(() => renderMarkdown(props.text))
</script>

<template>
  <div class="message" :class="role">
    <div class="message-role">{{ role === 'user' ? 'you' : 'assistant' }}</div>
    <div
      v-if="role === 'assistant'"
      class="message-content markdown"
      :class="{ streaming: text && !text.endsWith('\n') }"
      v-html="rendered"
    ></div>
    <div v-else class="message-content">{{ text }}</div>
  </div>
</template>
