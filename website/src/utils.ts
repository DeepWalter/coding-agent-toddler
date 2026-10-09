/**
 * Tiny shared helpers — path identities for the panes (editor tabs,
 * explorer, language lookup) and the token estimate the console labels
 * streamed reasoning with.
 */

import type { Plan, TabEntry } from './types'

/** Last path segment; '' for '' (`pop` never returns null for any string). */
export function basename(path: string): string {
  return path.split('/').pop() ?? path
}

/** A path the app addresses absolutely — a file outside the served root,
 *  which has no root-relative name.  The leading slash is the whole of the
 *  convention: internal paths are always relative, so the two namespaces
 *  never collide in `tabKey` or the editor's per-path record, and an
 *  external file opens read-only (the write endpoint stays root-scoped). */
export function isExternal(path: string): boolean {
  return path.startsWith('/')
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
 * tab AND as a staged/unstaged diff, so the kind + side prefix the path.
 * A text or markdown tab has no path; its id is what names it. */
export function tabKey(tab: TabEntry): string {
  if (tab.kind === 'file') return `f:${tab.path}`
  if (tab.kind === 'diff') return `d:${tab.staged ? 's' : 'u'}:${tab.path}`
  if (tab.kind === 'markdown') return `md:${tab.id}`
  return `t:${tab.id}`
}

/** The editor tab a plan opens as: one per plan id, holding the server's
 *  pre-rendered display markdown.  Both producers go through here — the
 *  console line's title click and the auto-open on proposal — so the two
 *  can never name the same plan differently. */
export function planDocTab(plan: Plan): Extract<TabEntry, { kind: 'markdown' }> {
  return {
    kind: 'markdown',
    id: `plan:${plan.id}`,
    title: plan.title,
    content: plan.markdown,
  }
}
