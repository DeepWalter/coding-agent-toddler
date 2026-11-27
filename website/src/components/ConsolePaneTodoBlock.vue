<script setup lang="ts">
import type { Block } from '../types'

defineProps<{
  block: Extract<Block, { kind: 'todo' }>
}>()

/** What each status puts inside its box: a star for the item running, a
 *  check for one that finished, nothing for one not started.  `failed` has
 *  no server producer today (TodoStatus is pending / in_progress /
 *  completed); it is spelled out so a status added later can never fall
 *  through to an empty box that reads pending. */
const MARK: Record<string, string> = {
  pending: '',
  in_progress: '*',
  completed: '✓',
  failed: '✗',
}

/** The status as words, for the box's label — the glyph alone carries it
 *  for the eye. */
function label(status: string): string {
  return status.replace('_', ' ')
}
</script>

<template>
  <!-- One list per todo update: the whole list as it stood when the
       update landed, so the console keeps a progress trail instead of
       one rewritten list. -->
  <ul class="todo-list">
    <li
      v-for="(item, index) in block.items"
      :key="index"
      class="todo-item"
    >
      <span
        class="todo-box"
        :class="item.status"
        role="img"
        :aria-label="label(item.status)"
      >{{ MARK[item.status] ?? '' }}</span>
      <span class="todo-text">{{ item.content }}</span>
    </li>
  </ul>
</template>
