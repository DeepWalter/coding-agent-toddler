/**
 * The click handler for the copy button every rendered code block carries in
 * the console (markdown.ts).  The blocks are cached HTML strings inserted
 * with `v-html`, so their button cannot be the CopyButton component — it
 * carries the same classes and feedback contract, driven from here instead.
 */

import { writeClipboard } from './clipboard'

/** How long the checkmark shows, matching CopyButton.vue. */
const COPIED_MS = 1200

const timers = new WeakMap<HTMLElement, ReturnType<typeof setTimeout>>()

/**
 * Handle a click that landed on a code block's copy button.  Returns false
 * when it did not, so the hosting component can fall through to its own
 * click handling (the repo-relative link interception).
 */
export function handleCodeCopyClick(event: MouseEvent): boolean {
  const target = event.target as HTMLElement | null
  const button = target?.closest?.('[data-code-copy]') as HTMLElement | null
  if (!button) return false

  event.preventDefault()
  event.stopPropagation()

  // The button is the wrapper's first child; the code is the only node
  // under it.  `textContent` is the block's source with the highlights'
  // spans stripped back out, and the newline markdown-it appends with the
  // fence's final line is part of that source.
  const text =
    button.closest('.code-block')?.querySelector('pre code')?.textContent ?? ''
  if (!text) return true

  void writeClipboard(text)
    .then(() => flashCopied(button))
    .catch((err) => {
      // Same silence as CopyButton: the button not changing is the only
      // feedback the reader gets; the reason belongs in the console.
      console.warn('copying code failed:', err)
    })
  return true
}

/** Swap the glyph for the checkmark and back, the way CopyButton does.  The
 *  block may be replaced by the next streaming frame first; the timer then
 *  finishes on a detached node, which is harmless. */
function flashCopied(button: HTMLElement): void {
  button.classList.add('copied')
  button.setAttribute('aria-label', 'Code copied')

  const timer = timers.get(button)
  if (timer) clearTimeout(timer)
  timers.set(
    button,
    setTimeout(() => {
      button.classList.remove('copied')
      button.setAttribute('aria-label', 'Copy code')
      timers.delete(button)
    }, COPIED_MS),
  )
}
