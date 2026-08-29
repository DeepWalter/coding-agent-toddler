<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api'
import { useGitAction } from '../composables/useGitAction'
import { gitBadgeClass } from '../gitStatus'
import type { GitFileAction, GitSectionMap, GitStatusLetter, GitStatusState } from '../types'

/**
 * Source-control panel: staged / unstaged changed files with git letter
 * badges, collapsible sections like VS Code's, plus row-level git actions
 * (stage / unstage / discard) and a commit box above the staged section.
 * A row click opens that file's side-by-side diff tab — staged rows open
 * the index-vs-HEAD diff, unstaged rows the worktree-vs-index diff.
 */

const props = defineProps<{
  git: GitStatusState
}>()
const emit = defineEmits<{
  'open-diff': [path: string, staged: boolean]
  'refresh-git': []
}>()

/** Section collapse state — local only (like the explorer's expansion:
 * a reload returns to the default expanded view). */
const collapsed = ref<Record<string, boolean>>({})

function toggle(section: string) {
  collapsed.value = { ...collapsed.value, [section]: !collapsed.value[section] }
}

const SECTIONS: { key: keyof GitSectionMap; title: string; staged: boolean }[] = [
  { key: 'staged', title: 'Staged Changes', staged: true },
  { key: 'unstaged', title: 'Changes', staged: false },
]

interface ScRow {
  path: string
  /** Path up to and including the last slash ('' for a top-level file). */
  dir: string
  base: string
  letter: GitStatusLetter
}

/** Per-section rows: keys sorted, paths split so the directory prefix can
 * render dimmed behind a full-weight basename. */
const rows = computed<Record<keyof GitSectionMap, ScRow[]>>(() => {
  const build = (map: Record<string, GitStatusLetter>): ScRow[] =>
    Object.keys(map)
      .sort((a, b) => a.localeCompare(b))
      .map((path) => {
        const i = path.lastIndexOf('/')
        return {
          path,
          dir: i === -1 ? '' : path.slice(0, i + 1),
          base: i === -1 ? path : path.slice(i + 1),
          letter: map[path],
        }
      })
  return {
    staged: build(props.git.sections.staged),
    unstaged: build(props.git.sections.unstaged),
  }
})

const commitMessage = ref('')
const { busy, error, run } = useGitAction()

/** Staged letters — commit allowed only with a message and at least one
 * non-conflict row staged (any C row makes git refuse the commit). */
const stagedLetters = computed(() => Object.values(props.git.sections.staged))
const canCommit = computed(
  () =>
    commitMessage.value.trim() !== '' &&
    stagedLetters.value.length > 0 &&
    stagedLetters.value.every((letter) => letter !== 'C') &&
    !busy.value,
)

async function onCommit() {
  if (!canCommit.value) return
  const message = commitMessage.value.trim()
  await run(
    async () => {
      await api.gitCommit({ message })
      commitMessage.value = '' // clear after success (VS Code behavior)
    },
    () => emit('refresh-git'),
  )
}

async function onFileAction(row: ScRow, action: GitFileAction) {
  if (busy.value) return
  // Discard destroys the worktree copy — confirm, and warn that an
  // untracked file has no HEAD copy to recover from.
  if (
    action === 'discard' &&
    !window.confirm(
      row.letter === 'U'
        ? `Discard ${row.path}? The untracked file will be deleted and cannot be recovered.`
        : `Discard changes to ${row.path}? The change cannot be undone.`,
    )
  ) {
    return
  }
  await run(
    () => api.gitApplyFile({ path: row.path, action }),
    () => emit('refresh-git'),
  )
}

function onRefresh() {
  emit('refresh-git')
}
</script>

<template>
  <div class="sc">
    <div class="sc-header">
      <span class="sc-title">Source Control</span>
      <span class="sc-branch" :title="git.branch ?? undefined">
        {{ git.branch ?? 'no repository' }}
      </span>
      <button
        type="button"
        class="sc-refresh"
        title="Refresh — re-run git status"
        :disabled="git.loading"
        @click="onRefresh"
      >
        ↻
      </button>
    </div>

    <div v-if="git.branch !== null" class="sc-commit">
      <textarea
        v-model="commitMessage"
        class="sc-commit-input"
        rows="2"
        placeholder="Commit message"
        :disabled="busy"
        @keydown.ctrl.enter.prevent="onCommit"
        @keydown.meta.enter.prevent="onCommit"
      ></textarea>
      <button
        type="button"
        class="btn small sc-commit-btn"
        :disabled="!canCommit"
        @click="onCommit"
      >
        Commit
      </button>
    </div>
    <div v-if="error" class="sc-error">{{ error }}</div>

    <div class="sc-list">
      <template v-if="git.branch === null">
        <div class="sc-empty">not a git repository</div>
      </template>
      <template v-else>
        <section v-for="section in SECTIONS" :key="section.key" class="sc-section">
          <button type="button" class="sc-section-head" @click="toggle(section.key)">
            <span class="sc-chevron">{{ collapsed[section.key] ? '▸' : '▾' }}</span>
            <span class="sc-section-title">{{ section.title }}</span>
            <span class="sc-count">{{ rows[section.key].length }}</span>
          </button>
          <div v-if="!collapsed[section.key]" class="sc-section-body">
            <div v-for="row in rows[section.key]" :key="row.path" class="sc-row">
              <button
                type="button"
                class="sc-row-name"
                :title="`open diff — ${row.path}`"
                @click="emit('open-diff', row.path, section.staged)"
              >
                <span class="sc-dir">{{ row.dir }}</span>
                <span class="sc-base">{{ row.base }}</span>
              </button>
              <span class="sc-row-actions">
                <button
                  v-if="section.staged && row.letter !== 'C'"
                  type="button"
                  class="sc-row-action"
                  title="Unstage"
                  :disabled="busy"
                  @click.stop="onFileAction(row, 'unstage')"
                >
                  −
                </button>
                <template v-else-if="row.letter !== 'C'">
                  <button
                    type="button"
                    class="sc-row-action discard"
                    title="Discard changes"
                    :disabled="busy"
                    @click.stop="onFileAction(row, 'discard')"
                  >
                    ↶
                  </button>
                  <button
                    type="button"
                    class="sc-row-action stage"
                    title="Stage changes"
                    :disabled="busy"
                    @click.stop="onFileAction(row, 'stage')"
                  >
                    +
                  </button>
                </template>
              </span>
              <span class="git-badge" :class="gitBadgeClass(row.letter)">
                {{ row.letter }}
              </span>
            </div>
            <div v-if="!rows[section.key].length" class="sc-empty">
              {{ section.staged ? 'no staged changes' : 'no changes' }}
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>
