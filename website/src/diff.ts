import type { DiffHunk, DiffLine } from './types'

/**
 * Turns parsed unified-diff hunks into side-by-side display rows.  One
 * row always renders the same position in both columns, so the two
 * scroll containers stay in lockstep by construction: context lines
 * pair as themselves, and each run of del/add lines flushes as
 * positional pairs (old side and new side share the row when counts
 * differ, leaving the short side's cell empty).
 */

export interface DiffRow {
  old: DiffLine | null
  new: DiffLine | null
}

export function buildRows(hunks: DiffHunk[]): DiffRow[] {
  const rows: DiffRow[] = []
  for (const hunk of hunks) {
    const dels: DiffLine[] = []
    const adds: DiffLine[] = []
    const flush = () => {
      if (!dels.length && !adds.length) return
      const n = Math.max(dels.length, adds.length)
      for (let i = 0; i < n; i++) {
        rows.push({ old: dels[i] ?? null, new: adds[i] ?? null })
      }
      dels.length = 0
      adds.length = 0
    }
    for (const line of hunk.lines) {
      if (line.kind === 'ctx') {
        flush()
        rows.push({ old: line, new: line })
      } else if (line.kind === 'del') {
        dels.push(line)
      } else {
        adds.push(line)
      }
    }
    flush()
  }
  return rows
}
