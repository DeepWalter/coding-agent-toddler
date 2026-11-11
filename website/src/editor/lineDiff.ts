import type { Text } from '@codemirror/state'

/**
 * Line-oriented helpers for the editor's SCM decorations.  The diff
 * engine itself is CodeMirror's `Chunk.build` (see `scm.ts`); this module
 * turns its line-boundary chunks into the 1-based ranges the gutter and
 * overview ruler consume.  Kept free of runtime imports so the Node test
 * can exercise it directly.
 */

/** A contiguous change on the current (worktree) side.  For `del` the
 *  range is a point at the line where the removed content used to be —
 *  the line that moved up into the gap.  All lines are 1-based. */
export interface ScmLineChange {
  kind: 'add' | 'mod' | 'del'
  start: number
  end: number
}

/** The fields of a CodeMirror merge `Chunk` this module reads.  A plain
 *  object with these fields is structurally compatible, which keeps the
 *  conversion unit-testable without dragging in the diff engine. */
export interface LineChunk {
  readonly fromA: number
  readonly toA: number
  readonly fromB: number
  readonly toB: number
}

/** Split *text* the way CodeMirror does — `\n` separators, a trailing
 *  newline produces a final empty line — while dropping the `\r` of CRLF
 *  so an LF↔CRLF-only difference never reads as a change. */
export function splitLines(text: string): string[] {
  return text
    .split('\n')
    .map((line) => (line.endsWith('\r') ? line.slice(0, -1) : line))
}

function lineAt(doc: Text, pos: number): number {
  return doc.lineAt(Math.max(0, Math.min(pos, doc.length))).number
}

/** The first and last 1-based lines a side covers.  A side's range is
 *  [from, to); `to - 1` lands on its last changed character, and a
 *  position at a line's newline belongs to that line (Text.lineAt). */
function lineRange(doc: Text, from: number, to: number): [number, number] {
  const first = lineAt(doc, from)
  if (to <= from) return [first, first]
  return [first, Math.max(first, lineAt(doc, to - 1))]
}

/** Convert merge chunks (character offsets in the HEAD and current
 *  documents) into change ranges on the current side.  An empty HEAD
 *  side is an insertion, an empty current side is a deletion, and a
 *  chunk with both sides is a modification. */
export function chunksToLineChanges(
  chunks: readonly LineChunk[],
  current: Text,
): ScmLineChange[] {
  const changes: ScmLineChange[] = []
  for (const chunk of chunks) {
    if (chunk.fromA === chunk.toA && chunk.fromB === chunk.toB) continue
    const [start, end] = lineRange(current, chunk.fromB, chunk.toB)
    if (chunk.fromA === chunk.toA) {
      changes.push({ kind: 'add', start, end })
    } else if (chunk.fromB === chunk.toB) {
      changes.push({ kind: 'del', start, end: start })
    } else {
      changes.push({ kind: 'mod', start, end })
    }
  }
  return changes
}
