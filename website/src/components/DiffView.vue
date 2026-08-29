<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import { buildRows, type DiffRow } from '../diff'
import { gitBadgeClass } from '../gitStatus'
import type {
  DiffHunk,
  GitDiffPayload,
  GitStatusState,
  HunkApplyAction,
  TabEntry,
} from '../types'

/**
 * Side-by-side diff of one file, hosted in the editor pane while a diff
 * tab is active.  Both columns render the same buildRows() output, so
 * equal line-heights make vertical scroll sync exact; horizontal scroll
 * is independent per column.  Per hunk, a VS Code-style button pair
 * (stage/unstage + revert) sits in a thin rail between the two sides,
 * pinned to the hunk's vertical center — offsets come from the uniform
 * row height, and the columns themselves stay a pure identical row
 * stream the scroll sync depends on.  Remounts per activation
 * (FileEditor keys it), so a diff tab refetches fresh on every
 * switch-to.
 */

const props = defineProps<{
  tab: Extract<TabEntry, { kind: 'diff' }>
  git: GitStatusState
}>()
const emit = defineEmits<{
  'open-file': [path: string]
  'refresh-git': []
}>()

const payload = ref<GitDiffPayload | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const applying = ref(false)
const applyError = ref<string | null>(null)

// Stale guard: an older in-flight response never overwrites a newer one
// (same idea as FileEditor's fetch tokens).
let seq = 0

async function load() {
  const my = ++seq
  loading.value = true
  error.value = null
  applyError.value = null
  try {
    const p = await api.gitDiff(props.tab.path, props.tab.staged)
    if (my !== seq) return
    payload.value = p
    await nextTick()
    measureLayout()
  } catch (err) {
    if (my !== seq) return
    error.value = (err as Error).message
  } finally {
    if (my === seq) loading.value = false
  }
}

onMounted(() => {
  load()
  window.addEventListener('resize', measureLayout)
  // Fonts can finish loading after first paint, breaking the uniform
  // row-height assumption — re-measure once they are ready.
  void document.fonts?.ready?.then(measureLayout)
})

onUnmounted(() => {
  // The component remounts per tab activation — leaked listeners would
  // stack, so remove what onMounted added.
  window.removeEventListener('resize', measureLayout)
})

const rows = computed<DiffRow[]>(() => (payload.value ? buildRows(payload.value.hunks) : []))

/** Row class is the same on both columns — a deletion leaves a red slot
 * on the new side, an addition on the old side. */
function rowClass(row: DiffRow): string {
  return (row.old?.kind ?? row.new?.kind ?? 'ctx')
}

// ---------------------------------------------------------------------------
// Per-hunk actions
// ---------------------------------------------------------------------------

/** Actions hide only on the unstaged untracked tab — an untracked file
 * has nothing in the index to stage against and no HEAD version to
 * revert to.  Truncated diffs also hide them (the last hunk is partial
 * and could never apply).  The staged tab shows its own unstage/revert
 * pair. */
const showActions = computed(() => {
  if (!payload.value || payload.value.truncated) return false
  if (!props.tab.staged && payload.value.old_path === null) return false
  return true
})

/** Fixed column metrics, read once per render — the offset math treats
 * every row as exactly rowH tall, which the CSS pins (single-line rows
 * with white-space: pre never wrap). */
const headerH = ref(0)
const rowH = ref(0)

function measureLayout() {
  const col = oldCol.value
  if (!col) return
  // The header's border-box is the offset where content begins.
  headerH.value = col.firstElementChild?.getBoundingClientRect().height ?? 0
  rowH.value = col.querySelector('.diff-row')?.getBoundingClientRect().height ?? 0
}

interface HunkAction {
  hunk: DiffHunk
  actions: HunkApplyAction[]
  /** Content px of the hunk's vertical center, unscrolled. */
  top: number
}

/** Action sets per tab: a staged file's hunks move back to the worktree
 * (unstage) or are discarded from index and worktree (revert); an
 * unstaged file's hunks go into the index (stage) or are discarded from
 * the worktree (revert). */
const STAGED_ACTIONS: HunkApplyAction[] = ['unstage', 'revert']
const UNSTAGED_ACTIONS: HunkApplyAction[] = ['revert', 'stage']

const ACTION_TITLES: Record<HunkApplyAction, string> = {
  stage: 'Add this block to the index',
  unstage: 'Move this block back to the worktree',
  revert: 'Discard this block',
}

const ACTION_LABELS: Record<HunkApplyAction, string> = {
  stage: '+',
  unstage: '−',
  revert: '→',
}

/** One floating button pair per hunk, stacked in the rail: the
 * destructive revert on top, the transfer action below.  A hunk's row
 * span is counted from its marker to the next one — buildRows pairs
 * del/add runs into shared rows, so the span is not the line count.
 * Offsets are cumulative row heights, so no DOM walking is needed. */
const hunkButtons = computed<HunkAction[]>(() => {
  if (!showActions.value) return []
  const out: HunkAction[] = []
  const actions = props.tab.staged ? STAGED_ACTIONS : UNSTAGED_ACTIONS
  for (let i = 0; i < rows.value.length; i++) {
    const row = rows.value[i]
    if (!row.hunk) continue
    let span = 1
    while (i + span < rows.value.length && !rows.value[i + span].hunk) span++
    out.push({
      hunk: row.hunk,
      actions,
      top: headerH.value + (i + span / 2) * rowH.value,
    })
    i += span - 1
  }
  return out
})

async function applyBlock(action: HunkApplyAction, hunk: DiffHunk) {
  const p = payload.value
  if (!p || applying.value) return
  if (
    action === 'revert' &&
    !window.confirm('Revert this block? The change is discarded and cannot be undone.')
  ) {
    return
  }
  applying.value = true
  applyError.value = null
  // Keep the columns where they are while the diff refetches — the
  // browser clamps the stored scrollTop if the content shrank.
  const oldTop = oldCol.value?.scrollTop ?? 0
  const newTop = newCol.value?.scrollTop ?? 0
  try {
    await api.gitApplyHunk({
      path: props.tab.path,
      staged: props.tab.staged,
      action,
      old_path: p.old_path,
      new_path: p.new_path,
      hunk,
    })
    await load()
    if (oldCol.value) oldCol.value.scrollTop = oldTop
    if (newCol.value) newCol.value.scrollTop = newTop
    // The browser clamps scrollTop without firing a scroll event —
    // re-pin the rail to the clamped value so the buttons land where
    // their hunks are.
    if (oldCol.value) {
      actionsEl.value?.style.setProperty('--scroll-top', `${oldCol.value.scrollTop}px`)
    }
    emit('refresh-git')
  } catch (err) {
    applyError.value = (err as Error).message
  } finally {
    applying.value = false
  }
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
// drifted (keeps the loop from ping-ponging at rest).  The rail's
// buttons are pinned by the same scrollTop — written directly to a CSS
// var, so a scroll tick never re-renders the row stream.
const oldCol = ref<HTMLElement | null>(null)
const newCol = ref<HTMLElement | null>(null)
const actionsEl = ref<HTMLElement | null>(null)

function onScroll(side: 'old' | 'new') {
  const source = side === 'old' ? oldCol.value : newCol.value
  const target = side === 'old' ? newCol.value : oldCol.value
  if (!source || !target) return
  // With no buttons the rail has nothing to pin — skip the write so
  // untracked/truncated tabs never touch the CSS var.
  if (hunkButtons.value.length) {
    actionsEl.value?.style.setProperty('--scroll-top', `${source.scrollTop}px`)
  }
  if (Math.abs(target.scrollTop - source.scrollTop) > 1) {
    target.scrollTop = source.scrollTop
  }
}

/** Wheel over the rail has nothing scrollable beneath it — forward the
 *  delta to the old column, which syncs the new one (the target's own
 *  scroll event re-pins the buttons).  Line-mode deltas (Firefox) are
 *  scaled to pixels, page-mode deltas (rare trackpads) to the column
 *  height. */
function onMidWheel(e: WheelEvent) {
  const col = oldCol.value
  if (!col) return
  const scale = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? col.clientHeight : 1
  col.scrollTop += e.deltaY * scale
  col.scrollLeft += e.deltaX * scale
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

    <div v-if="applyError" class="diff-apply-error">{{ applyError }}</div>
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
        <!-- The action rail (styled in styles.css): one stacked button
             pair per hunk — → revert above, +/− transfer below —
             pinned to the hunk's middle by --hunk-top minus
             --scroll-top.  It is always rendered so the columns never
             shift when actions appear or disappear; wheel over it
             scrolls the diff. -->
        <div ref="actionsEl" class="diff-mid" @wheel.prevent="onMidWheel">
          <div
            v-for="(b, i) in hunkButtons"
            :key="i"
            class="diff-action-group"
            :style="{ '--hunk-top': b.top + 'px' }"
          >
            <button
              v-for="a in b.actions"
              :key="a"
              type="button"
              class="diff-action-btn"
              :class="a === 'revert' ? 'revert' : 'stage'"
              :disabled="applying"
              :title="ACTION_TITLES[a]"
              @click="applyBlock(a, b.hunk)"
            >
              {{ ACTION_LABELS[a] }}
            </button>
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
