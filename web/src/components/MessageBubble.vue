<script setup lang="ts">
defineProps<{
  role: 'user' | 'assistant'
  text: string
}>()
</script>

<template>
  <div class="message" :class="role">
    <div class="message-role">{{ role === 'user' ? 'you' : 'assistant' }}</div>
    <div class="message-content" :class="{ streaming: role === 'assistant' && text && !text.endsWith('\n') }">
      {{ text }}
    </div>
  </div>
</template>
