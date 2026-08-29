/**
 * Tiny path helpers shared across panes (editor tabs, explorer, language
 * lookup) — one copy of the basename logic instead of three.
 */

import type { TabEntry } from './types'

/** Last path segment; '' for '' (`pop` never returns null for any string). */
export function basename(path: string): string {
  return path.split('/').pop() ?? path
}

/** Identity of a tab-strip entry — the same file can be open as a file
 * tab AND as a staged/unstaged diff, so the kind + side prefix the path. */
export function tabKey(tab: TabEntry): string {
  if (tab.kind === 'file') return `f:${tab.path}`
  return `d:${tab.staged ? 's' : 'u'}:${tab.path}`
}
