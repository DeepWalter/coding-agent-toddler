import { computed, ref } from 'vue'

/**
 * Which input-bar popup is open, by id — module-scoped, because the two
 * pickers (mode, model+effort) share one input bar and must not both be
 * open at once.
 *
 * Each picker's own outside-`pointerdown` handler is enough for the
 * pointer path: clicking one trigger is always outside the other's root,
 * so it closes the other on the way past.  The keyboard path has no such
 * event — focus the mode pill, Enter, Tab to the model pill, Enter — and
 * with two independent `open` refs both popups end up open, a few dozen
 * pixels apart and overlapping.  One shared ref closes that hole without
 * either picker knowing about the other.
 */

const openId = ref<string | null>(null)

export function usePicker(id: string) {
  const open = computed(() => openId.value === id)

  /** Open if closed, close if open.  *allowed* is the picker's own gate
   *  (connected, idle): a disabled trigger must not toggle. */
  function toggle(allowed = true) {
    if (!allowed) return
    openId.value = open.value ? null : id
  }

  function close() {
    if (open.value) openId.value = null
  }

  return { open, toggle, close }
}
