/**
 * Arrow-key (up/down) roving over an ask card's choice rows (tool gate,
 * plan ask).  Bind as a keydown handler on the card's root element — the
 * same handler serves every card (decision rows, feedback rows).  Rows are
 * the buttons nested in a .*-actions block (the head's ✕ close is not a
 * choice); they stay plain buttons, so Tab still walks them one by one —
 * this only adds the up/down moves on top.
 *
 * Keys pressed inside an editable element (the plan ask's feedback
 * textarea) are left alone, so the caret keeps its own arrow behavior.
 * When every row is disabled the arrows fall through to their default
 * (scrolling) — nothing on the card is actionable anyway.
 */

/** Where the choice rows live inside an ask card — each alternative is a
 * complete selector (a comma never shares a descendant part). */
const ROW_SELECTOR = '.pause-prompt-actions button, .plan-ask-actions button'

export function onChoiceKeydown(event: KeyboardEvent) {
  const key = event.key
  if (key !== 'ArrowDown' && key !== 'ArrowUp') return
  const target = event.target
  if (target instanceof HTMLElement && target.closest('textarea, input, select, [contenteditable]')) {
    return
  }
  const host = event.currentTarget as HTMLElement | null
  if (!host) return
  const rows = [...host.querySelectorAll<HTMLButtonElement>(ROW_SELECTOR)].filter(
    (row) => !row.disabled,
  )
  if (!rows.length) return
  event.preventDefault()
  const step = key === 'ArrowDown' ? 1 : -1
  const active = rows.indexOf(document.activeElement as HTMLButtonElement)
  // Moves wrap at the list's ends; Down / Up from anywhere else in the
  // card (the head, e.g. the ✕ close while busy) lands on the first /
  // last row.
  const index =
    active === -1
      ? step === 1
        ? 0
        : rows.length - 1
      : (active + step + rows.length) % rows.length
  rows[index].focus()
}
