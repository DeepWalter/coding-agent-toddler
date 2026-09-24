import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

/** Whether the box the caller points at is taller than the box it is drawn
 *  in — the one question a clipped preview has to answer before it can show
 *  a fade or offer a way past it.  Measured, never inferred from the text:
 *  whether three lines of *this* width overflow is not something a content
 *  rule can know, and the answer is per box even when two boxes read the
 *  same string.
 *
 *  The clip itself lives in the caller's CSS (a `max-height`); this only
 *  reports whether it is biting, so `data-clamped` and everything gated on
 *  it follow the measurement rather than the text. */
export interface Overflow {
  /** True while the content is taller than the clipped box. */
  clamped: Ref<boolean>
  /** Re-measure now.  The observer covers content and pane resizes; a
   *  caller whose text changes in place without changing the box's height
   *  can ask again. */
  measure: () => void
}

export function useOverflow(el: Ref<HTMLElement | null>): Overflow {
  const clamped = ref(false)

  function measure() {
    const box = el.value
    if (box) clamped.value = box.scrollHeight - box.clientHeight > 1
  }

  let observer: ResizeObserver | null = null

  onMounted(() => {
    measure()
    // The box is sized by its pane, so a resize reflows it — and a reflow can
    // change how much fits, which a one-time measure would miss.
    observer = new ResizeObserver(measure)
    if (el.value) observer.observe(el.value)
  })
  onBeforeUnmount(() => observer?.disconnect())

  return { clamped, measure }
}
