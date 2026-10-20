import type { DiffHunk, DiffLine, DiffLineKind } from './types'

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
  /** The owning hunk — set on the first row of each hunk.  Rows never
   * span hunks (flush() runs at context lines and hunk ends), so each
   * hunk boundary lands on exactly one row. */
  hunk?: DiffHunk
}

export function buildRows(hunks: DiffHunk[]): DiffRow[] {
  const rows: DiffRow[] = []
  for (const hunk of hunks) {
    const dels: DiffLine[] = []
    const adds: DiffLine[] = []
    let first = true
    const flush = () => {
      if (!dels.length && !adds.length) return
      const n = Math.max(dels.length, adds.length)
      for (let i = 0; i < n; i++) {
        rows.push({ old: dels[i] ?? null, new: adds[i] ?? null, hunk: first ? hunk : undefined })
        first = false
      }
      dels.length = 0
      adds.length = 0
    }
    for (const line of hunk.lines) {
      if (line.kind === 'ctx') {
        flush()
        rows.push({ old: line, new: line, hunk: first ? hunk : undefined })
        first = false
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

// ---------------------------------------------------------------------------
// An edit call's snippet diff
// ---------------------------------------------------------------------------

/** One row of the diff a file edit's card shows under its title: the lines
 *  the call replaced, the lines it wrote, and enough unchanged lines around
 *  them to read the change in place.  A `gap` row stands for an unchanged run
 *  too long to print, the way a unified diff elides it.
 *
 *  No line numbers: this diff is built from the call's two strings, not from
 *  the file, so the only numbers available would be positions inside the
 *  snippet — which read as file lines and are not. */
export interface EditDiffRow {
  kind: DiffLineKind
  text: string
  gap?: boolean
}

/** How many rows the card prints before the rest moves to the editor. */
export const MAX_DIFF_ROWS = 12

/** Unchanged lines kept on each side of a change. */
const CONTEXT_LINES = 3

/** Above this many DP cells a snippet is diffed coarsely rather than at
 *  line-pair precision — a whole-file old_string must not freeze the card. */
const LCS_CELL_LIMIT = 250_000

/** Split a call's string argument into display lines.  A trailing newline
 *  ends the last line rather than starting an empty one, and the CR of a
 *  CRLF pair is display noise. */
function splitLines(text: string): string[] {
  if (!text) return []
  const lines = text.split('\n')
  if (lines[lines.length - 1] === '') lines.pop()
  return lines.map((line) => (line.endsWith('\r') ? line.slice(0, -1) : line))
}

interface DiffOp {
  kind: DiffLineKind
  text: string
}

/** Longest-common-subsequence edit script for two line lists. */
function lcsOps(a: string[], b: string[]): DiffOp[] {
  const n = a.length
  const m = b.length
  // dp[i][j] is the LCS length of a[i:] against b[j:].  Uint32 keeps the
  // table compact enough for the cell cap above.
  const dp: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const ops: DiffOp[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ kind: 'ctx', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ kind: 'del', text: a[i] })
      i++
    } else {
      ops.push({ kind: 'add', text: b[j] })
      j++
    }
  }
  while (i < n) ops.push({ kind: 'del', text: a[i++] })
  while (j < m) ops.push({ kind: 'add', text: b[j++] })
  return ops
}

/** The edit script, with every unchanged run trimmed to the context a reader
 *  needs: the lines next to a change stay, the middle of a long run becomes a
 *  gap row. */
function withContext(ops: DiffOp[]): EditDiffRow[] {
  const out: EditDiffRow[] = []
  let i = 0
  while (i < ops.length) {
    if (ops[i].kind !== 'ctx') {
      out.push(ops[i])
      i++
      continue
    }
    let end = i
    while (end < ops.length && ops[end].kind === 'ctx') end++
    const run = ops.slice(i, end)
    if (i === 0) {
      // The opening run is all context: keep the lines nearest the change.
      out.push(...run.slice(Math.max(0, run.length - CONTEXT_LINES)))
    } else if (end === ops.length) {
      // The closing run: keep what follows the last change.
      out.push(...run.slice(0, CONTEXT_LINES))
    } else if (run.length > CONTEXT_LINES * 2) {
      out.push(...run.slice(0, CONTEXT_LINES))
      out.push({ kind: 'ctx', text: '', gap: true })
      out.push(...run.slice(run.length - CONTEXT_LINES))
    } else {
      out.push(...run)
    }
    i = end
  }
  return out
}

/** The diff an `edit_file` call makes: old_string against new_string, as the
 *  card prints it.  Null when the two are line-identical — a no-op edit has
 *  nothing to draw. */
export function editDiff(oldText: string, newText: string): EditDiffRow[] | null {
  const oldLines = splitLines(oldText)
  const newLines = splitLines(newText)

  // The common head and tail are the snippet's own context and would swamp
  // the LCS table with cells that can only match.  They are put back below.
  let start = 0
  while (
    start < oldLines.length &&
    start < newLines.length &&
    oldLines[start] === newLines[start]
  ) {
    start++
  }
  let endOld = oldLines.length
  let endNew = newLines.length
  while (
    endOld > start &&
    endNew > start &&
    oldLines[endOld - 1] === newLines[endNew - 1]
  ) {
    endOld--
    endNew--
  }

  const oldMid = oldLines.slice(start, endOld)
  const newMid = newLines.slice(start, endNew)
  const midOps: DiffOp[] = oldMid.length * newMid.length > LCS_CELL_LIMIT
    ? [
        ...oldMid.map((text): DiffOp => ({ kind: 'del', text })),
        ...newMid.map((text): DiffOp => ({ kind: 'add', text })),
      ]
    : lcsOps(oldMid, newMid)

  const ops: DiffOp[] = [
    ...oldLines.slice(0, start).map((text): DiffOp => ({ kind: 'ctx', text })),
    ...midOps,
    ...oldLines.slice(endOld).map((text): DiffOp => ({ kind: 'ctx', text })),
  ]
  if (!ops.some((op) => op.kind !== 'ctx')) return null
  return withContext(ops)
}

/** The same rows as unified-diff text, for the editor's diff grammar: the
 *  leading `-`/`+`/space is what carries the line's kind, and a gap is the
 *  ellipsis the card shows.  No `@@` headers — there are no file line numbers
 *  to put in them. */
export function editDiffText(rows: EditDiffRow[]): string {
  return rows
    .map((row) => {
      if (row.gap) return '⋯'
      if (row.kind === 'del') return `-${row.text}`
      if (row.kind === 'add') return `+${row.text}`
      return ` ${row.text}`
    })
    .join('\n')
}
