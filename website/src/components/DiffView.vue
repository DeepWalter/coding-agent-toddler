<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { buildRows, type DiffRow } from '../diff'
import { gitBadgeClass } from '../gitStatus'
import type { GitDiffPayload, GitStatusState, TabEntry } from '../types'

/**
 * Side-by-side diff of one file, hosted in the editor pane while a diff
 * tab is active.  Both columns render the same buildRows() output, so
 * equal line-heights make vertical scroll sync exact; horizontal scroll
 * is independent per column.  Remounts per activation (FileEditor keys
 * it), so a diff tab refetches fresh on every switch-to.
 */

const props = defineProps<{
  tab: Extract<TabEntry, { kind: 'diff' }>
  git: GitStatusState
}>()
const emit = defineEmits<{
  'open-file': [path: string]
}>()

const payload = ref<GitDiffPayload | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

// Stale guard: an older in-flight response never overwrites a newer one
// (same idea as FileEditor's fetch tokens).
let seq = 0

async function load() {
  const my = ++seq
  loading.value = true
  error.value = null
  try {
    const p = await api.gitDiff(props.tab.path, props.tab.staged)
    if (my !== seq) return
    payload.value = p
  } catch (err) {
    if (my !== seq) return
    error.value = (err as Error).message
  } finally {
    if (my === seq) loading.value = false
  }
}

onMounted(load)

const rows = computed<DiffRow[]>(() => (payload.value ? buildRows(payload.value.hunks) : []))

/** Row class is the same on both columns — a deletion leaves a red slot
 * on the new side, an addition on the old side. */
function rowClass(row: DiffRow): string {
  return (row.old?.kind ?? row.new?.kind ?? 'ctx')
}

/** The letter badge from the owning section, falling back to the overall
 * file map (an MM file shows the same letter either way). */
const badgeLetter = computed(() => {
  const map = props.tab.staged ? props.git.sections.staged : props.git.sections.unstaged
  return map[props.tab.path] ?? props.git.files[props.tab.path] ?? ''
})

const summary = computed(() => {
  if (!payload.value || payload.value.binary) return ''
  let dels = 0
  let adds = 0
  for (const hunk of payload.value.hunks) {
    for (const line of hunk.lines) {
      if (line.kind === 'del') dels++
      else if (line.kind === 'add') adds++
    }
  }
  const text = `${dels} deleted, ${adds} added`
  return payload.value.truncated ? `${text} (truncated)` : text
})

const statusText = computed(() => {
  if (loading.value && !payload.value) return 'loading…'
  if (!payload.value) return ''
  return payload.value.binary ? 'binary file' : summary.value
})

// Vertical scroll sync: echoing the other column's scrollTop fires its
// own scroll event, so only write when the columns have actually
// drifted (keeps the loop from ping-ponging at rest).
const oldCol = ref<HTMLElement | null>(null)
const newCol = ref<HTMLElement | null>(null)

function onScroll(side: 'old' | 'new') {
  const source = side === 'old' ? oldCol.value : newCol.value
  const target = side === 'old' ? newCol.value : oldCol.value
  if (!source || !target) return
  if (Math.abs(target.scrollTop - source.scrollTop) > 1) {
    target.scrollTop = source.scrollTop
  }
}
</script>

<template>
  <div class="diff">
    <div class="diff-header">
      <span class="diff-title" :title="tab.path">{{ tab.path }}</span>
      <span v-if="badgeLetter" class="git-badge" :class="gitBadgeClass(badgeLetter)">
        {{ badgeLetter }}
      </span>
      <span class="diff-tag" :class="tab.staged ? 'staged' : 'unstaged'">
        {{ tab.staged ? 'staged' : 'unstaged' }}
      </span>
      <button
        type="button"
        class="btn small"
        title="Open the file in the editor"
        @click="emit('open-file', tab.path)"
      >
        Open File
      </button>
      <button
        type="button"
        class="diff-refresh"
        title="Refresh diff"
        :disabled="loading"
        @click="load"
      >
        ↻
      </button>
      <span class="diff-status">{{ statusText }}</span>
    </div>

    <div v-if="error" class="diff-empty error">{{ error }}</div>
    <div v-else-if="loading && !payload" class="diff-empty">loading…</div>
    <div v-else-if="payload?.binary" class="diff-empty">binary file — diff not shown</div>
    <template v-else-if="payload">
      <div v-if="rows.length" class="diff-body">
        <div ref="oldCol" class="diff-col" @scroll.passive="onScroll('old')">
          <div class="diff-col-header">{{ payload.old_path ?? '' }}</div>
          <div
            v-for="(row, i) in rows"
            :key="i"
            class="diff-row"
            :class="rowClass(row)"
          >
            <span class="diff-gutter">{{ row.old?.old_ln ?? '' }}</span>
            <span class="diff-text">
              {{ row.old?.text ?? '' }}
              <span v-if="row.old?.no_newline" class="diff-noeol" title="no newline at end of file">⏎</span>
            </span>
          </div>
        </div>
        <div ref="newCol" class="diff-col" @scroll.passive="onScroll('new')">
          <div class="diff-col-header">{{ payload.new_path ?? '' }}</div>
          <div
            v-for="(row, i) in rows"
            :key="i"
            class="diff-row"
            :class="rowClass(row)"
          >
            <span class="diff-gutter">{{ row.new?.new_ln ?? '' }}</span>
            <span class="diff-text">
              {{ row.new?.text ?? '' }}
              <span v-if="row.new?.no_newline" class="diff-noeol" title="no newline at end of file">⏎</span>
            </span>
          </div>
        </div>
      </div>
      <div v-else class="diff-empty">no changes</div>
    </template>
  </div>
</template>
