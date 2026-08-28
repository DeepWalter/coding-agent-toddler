import type { GitStatusLetter } from './types'

/**
 * Pure helpers for git-status badges and status-bar counts — no Vue deps,
 * shared by FileExplorer, FileEditor, StatusBar, and useGitStatus.
 */

/** Letter → CSS modifier class (colors in styles.css, both themes). */
export const GIT_LETTER_CLASS: Record<GitStatusLetter, string> = {
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

export interface GitCounts {
  modified: number
  added: number
  deleted: number
  renamed: number
  untracked: number
  conflict: number
}

/** Status-bar category counts, derived from the files map. */
export function countStatus(files: Record<string, GitStatusLetter>): GitCounts {
  const c: GitCounts = {
    modified: 0,
    added: 0,
    deleted: 0,
    renamed: 0,
    untracked: 0,
    conflict: 0,
  }
  for (const letter of Object.values(files)) {
    if (letter === 'M' || letter === 'T') c.modified++
    else if (letter === 'A') c.added++
    else if (letter === 'D') c.deleted++
    else if (letter === 'R') c.renamed++
    else if (letter === 'U') c.untracked++
    else c.conflict++
  }
  return c
}
