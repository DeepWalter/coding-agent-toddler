/**
 * Arrow-key (up/down) roving over an ask card's choice rows (tool gate,
 * plan ask).  Bind as a keydown handler on the card's root element — the
 * same handler serves every card (decision rows, feedback rows).  Rows are
 * the buttons nested in a .*-actions block (the head's ✕ close is not a
 * choice); they stay plain buttons, so Tab still walks them one by one —
 * this only adds the up/down moves on top.
 *
 * The arrows are the card's only while the card has the focus: click the
 * console and ↑/↓ scroll the transcript again, which is what a click there
 * asked for.  What that click must not take away is the *selection* — the
 * row the card would answer with stays marked (onChoiceFocus), so a gate
 * never looks answered, or unanswerable, because the pointer was somewhere
 * else.  Clicking back onto the card takes the arrows back with it, and
 * they carry on from the row it has marked.
 *
 * Enter answers that row, wherever the focus sits inside the card: the
 * card's own body is focusable (a click lands there), and a card wearing
 * the edge that says the keys are its own has to do something with them.
 *
 * Keys pressed inside an editable element (the plan ask's feedback
 * textarea) are left alone, so the caret keeps its own arrow behavior.
 * When every row is disabled the arrows fall through to their default
 * (scrolling) — nothing on the card is actionable anyway.
 */

/** Where the choice rows live inside an ask card — each alternative is a
 * complete selector (a comma never shares a descendant part). */
const ROW_SELECTOR = '.pause-prompt-actions button, .plan-ask-actions button'

/** The mark on the row the card would answer with.  Worn by one row at a
 * time, and kept while the focus is elsewhere. */
export const SELECTED_CLASS = 'is-selected'

export function onChoiceKeydown(event: KeyboardEvent) {
  const key = event.key
  if (key !== 'ArrowDown' && key !== 'ArrowUp' && key !== 'Enter') return
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
  const focused = rows.indexOf(document.activeElement as HTMLButtonElement)
  // From a row, the move is from that row.  From anywhere else in the card
  // — its own body, where a click landed, or the ✕ while busy — it
  // continues from the row the card has marked, so leaving and coming back
  // does not lose the place.  With nothing marked it starts at the end the
  // key came from; either way it wraps.
  const marked = rows.findIndex((row) => row.classList.contains(SELECTED_CLASS))
  const from = focused !== -1 ? focused : marked

  if (key === 'Enter') {
    // A row answers its own Enter.  The card's body is focusable too (a
    // click lands there), and a card that lights up its edge — the one
    // reading that says the keys are its — has to answer: Enter answers
    // with the row it has marked.
    if (focused !== -1) return
    event.preventDefault()
    rows[from === -1 ? 0 : from].click()
    return
  }

  event.preventDefault()
  const step = key === 'ArrowDown' ? 1 : -1
  const index =
    from === -1
      ? step === 1
        ? 0
        : rows.length - 1
      : (from + step + rows.length) % rows.length
  rows[index].focus()
}

/** Move the selection mark to whatever row the focus landed on.
 *
 * Bound to the card's `focusin`, so it follows a Tab, an arrow, or a click
 * on a row — and does nothing when the focus arrives from outside the card
 * or leaves it: the mark is the card's memory of where it stands, and a
 * click on the page around it is not an answer to it.
 */
export function onChoiceFocus(event: FocusEvent): void {
  const host = event.currentTarget
  if (!(host instanceof HTMLElement)) return
  const target = event.target
  const row = target instanceof HTMLElement ? target.closest('button') : null
  // Only a choice moves the mark.  The head's ✕ is a button too, and not
  // one: tabbing to it, or clicking it, leaves the card standing where it
  // was — and the cancel it fires is about the turn, not the selection.
  if (!row || !host.contains(row) || !row.matches(ROW_SELECTOR)) return
  for (const marked of host.querySelectorAll(`.${SELECTED_CLASS}`)) {
    if (marked !== row) marked.classList.remove(SELECTED_CLASS)
  }
  row.classList.add(SELECTED_CLASS)
}
