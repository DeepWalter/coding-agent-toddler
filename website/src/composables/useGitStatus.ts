import { reactive } from 'vue'
import { api } from '../api'
import type { Frame, GitStatusState } from '../types'

/**
 * Shared git working-tree snapshot for the explorer, editor tabs, and
 * status bar.  Git status is a whole-snapshot replace — unlike the
 * console stream there is nothing incremental to reduce, so this is a
 * plain fetch + sequence guard instead of a reducer.
 */

/** Tool names that write files — their tool_call_end frames invalidate. */
const WRITE_TOOLS = new Set(['write_file', 'edit_file'])
const REFRESH_DEBOUNCE_MS = 400

export function useGitStatus() {
  const state = reactive<GitStatusState>({
    branch: null,
    files: {},
    dirs: {},
    sections: { staged: {}, unstaged: {} },
    loading: false,
    error: null,
  })

  // Sequence guard: an older in-flight response never overwrites a newer
  // snapshot (same idea as FileEditor's fetch tokens).
  let seq = 0
  let debounceTimer: ReturnType<typeof setTimeout> | null = null

  async function refresh() {
    const my = ++seq
    state.loading = true
    state.error = null
    try {
      const payload = await api.gitStatus()
      if (my !== seq) return // superseded by a newer refresh
      state.branch = payload.branch
      state.files = payload.files
      state.dirs = payload.dirs
      state.sections = payload.sections
    } catch (err) {
      if (my !== seq) return
      state.error = (err as Error).message // keep last good data
    } finally {
      if (my === seq) state.loading = false
    }
  }

  function scheduleRefresh() {
    if (debounceTimer) clearTimeout(debounceTimer) // coalesce write bursts
    debounceTimer = setTimeout(() => void refresh(), REFRESH_DEBOUNCE_MS)
  }

  /** Frame hook — App.vue routes every WS frame through here. */
  function onFrame(frame: Frame) {
    if (frame.type === 'agent_finished') {
      // Catch-all: shell-tool writes, git add/mv/commit, everything.
      void refresh()
    } else if (
      frame.type === 'tool_call_end' &&
      WRITE_TOOLS.has(frame.tool_name)
    ) {
      scheduleRefresh()
    }
  }

  return { state, refresh, onFrame }
}
