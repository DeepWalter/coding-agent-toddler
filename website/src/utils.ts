/**
 * Tiny shared helpers — path identities for the panes (editor tabs,
 * explorer, language lookup) and the token estimate the console labels
 * streamed reasoning with.
 */

import type { TabEntry } from './types'

/** Last path segment; '' for '' (`pop` never returns null for any string). */
export function basename(path: string): string {
  return path.split('/').pop() ?? path
}

/** Rough token estimate for a streamed buffer.  The client has no
 *  tokenizer, so this is the classic four-characters-per-token rule with
 *  non-ASCII counted as a token each (CJK and emoji compress far less
 *  than Latin prose).  Order-of-magnitude honest, never exact — the API's
 *  own `reasoning_tokens` is the number to trust, and it only lands when
 *  the turn ends. */
export function estimateTokens(text: string): number {
  const wide = text.match(/[^\x00-\x7f]/g)?.length ?? 0
  return Math.ceil((text.length - wide) / 4) + wide
}

/** Token counts get long fast, and the label is a glanceable figure rather
 *  than a measurement — so past a thousand it abbreviates to one decimal:
 *  999 stays exact, then a K, then an M. */
export function formatTokens(n: number): string {
  if (n < 1000) return `${n}`
  // Round to the printed decimal *before* choosing the unit, so 999_990
  // reads as 1.0M instead of 1000.0K.
  const thousands = Math.round(n / 100) / 10
  if (thousands < 1000) return `${thousands.toFixed(1)}K`
  return `${(Math.round(n / 100_000) / 10).toFixed(1)}M`
}

/** Identity of a tab-strip entry — the same file can be open as a file
 * tab AND as a staged/unstaged diff, so the kind + side prefix the path. */
export function tabKey(tab: TabEntry): string {
  if (tab.kind === 'file') return `f:${tab.path}`
  return `d:${tab.staged ? 's' : 'u'}:${tab.path}`
}
