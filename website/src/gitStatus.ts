import type { GitStatusLetter } from './types'

/**
 * Pure helpers for git-status badges — no Vue deps, shared by
 * FileExplorer, FileEditor, DiffView, and SourceControl.
 */

/** Letter → CSS modifier class (colors in styles.css, both themes). */
const GIT_LETTER_CLASS: Record<GitStatusLetter, string> = {
  M: 'mod',
  A: 'add',
  D: 'del',
  R: 'rename',
  U: 'untracked',
  T: 'mod',
  C: 'conflict',
}

export function gitBadgeClass(letter: string): string {
  return GIT_LETTER_CLASS[letter as GitStatusLetter] ?? ''
}
