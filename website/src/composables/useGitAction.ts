import { ref } from 'vue'

/**
 * Shared plumbing for git action buttons — the source-control panel's
 * row actions and commit box, and the diff view's hunk rails: a busy
 * flag that disables every control while a git command runs, an error
 * banner message, and run() that wraps an api call in both, running the
 * caller's post-success hook (refresh, scroll restore) between them.
 */

export function useGitAction() {
  /** Any op in flight — disables every control while a git command runs. */
  const busy = ref(false)
  /** Banner text shown under the header when a call fails. */
  const error = ref<string | null>(null)

  async function run(
    action: () => Promise<unknown>,
    onSuccess?: () => void | Promise<void>,
  ) {
    if (busy.value) return
    busy.value = true
    error.value = null
    try {
      await action()
      await onSuccess?.()
    } catch (err) {
      error.value = (err as Error).message
    } finally {
      busy.value = false
    }
  }

  return { busy, error, run }
}
