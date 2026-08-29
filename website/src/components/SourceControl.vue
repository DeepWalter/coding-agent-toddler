<script setup lang="ts">
import { computed, ref } from 'vue'
import { gitBadgeClass } from '../gitStatus'
import type { GitSectionMap, GitStatusLetter, GitStatusState } from '../types'

/**
 * Source-control panel: staged / unstaged changed files with git letter
 * badges, collapsible sections like VS Code's.  A row click opens that
 * file's side-by-side diff tab — staged rows open the index-vs-HEAD diff,
 * unstaged rows the worktree-vs-index diff.
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
            <button
              v-for="row in rows[section.key]"
              :key="row.path"
              type="button"
              class="sc-row"
              :title="`open diff — ${row.path}`"
              @click="emit('open-diff', row.path, section.staged)"
            >
              <span class="sc-dir">{{ row.dir }}</span>
              <span class="sc-base">{{ row.base }}</span>
              <span class="git-badge" :class="gitBadgeClass(row.letter)">
                {{ row.letter }}
              </span>
            </button>
            <div v-if="!rows[section.key].length" class="sc-empty">
              {{ section.staged ? 'no staged changes' : 'no changes' }}
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>
