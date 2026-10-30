import { countColumn, type EditorState, type Extension } from '@codemirror/state'
import { keymap } from '@codemirror/view'
import {
  StreamLanguage, foldGutter, foldKeymap, foldService, matchBrackets, syntaxTree,
  type Language,
} from '@codemirror/language'
import { importFoldInfo } from './languages'

/**
 * Editor folding for every kind of buffer.  Tree grammars (lang-python,
 * lang-javascript, …) carry their own foldNodeProp ranges; legacy stream
 * grammars and plain text have none, so they fold indented blocks instead —
 * the same fallback an editor without syntax info uses.  Two services add
 * what the grammars miss: a leading import run and multi-line bracket pairs.
 * The gutter and its keymap are shared, which is what lets read-only buffers
 * fold: a fold is an effect, not a doc change, so EditorState.readOnly does
 * not block it.
 */

// The fold gutter asks `foldable` for every viewport line on every rebuild.
// Without a cap, one block scan could walk the rest of a huge document per
// line; past it the line simply reads as unfoldable.
const MAX_FOLD_LINES = 2000

const SVG_NS = 'http://www.w3.org/2000/svg'
const MARKER_SIZE = 16

/** A fixed square marker for both fold states: one chevron path, turned a
 *  quarter when the fold is closed, so toggling never moves its center.
 *  `markerDOM` is handed only the state (no view), so the titles repeat
 *  foldGutter's defaults rather than asking the state for its phrases. */
function markerDOM(open: boolean): HTMLElement {
  const span = document.createElement('span')
  span.className = open ? 'cm-foldMarker' : 'cm-foldMarker cm-foldMarkerClosed'
  span.title = open ? 'Fold line' : 'Unfold line'
  const svg = document.createElementNS(SVG_NS, 'svg')
  svg.setAttribute('viewBox', `0 0 ${MARKER_SIZE} ${MARKER_SIZE}`)
  svg.setAttribute('width', `${MARKER_SIZE}`)
  svg.setAttribute('height', `${MARKER_SIZE}`)
  svg.setAttribute('aria-hidden', 'true')
  const path = document.createElementNS(SVG_NS, 'path')
  path.setAttribute('d', 'M4 6l4 4 4-4')
  path.setAttribute('fill', 'none')
  path.setAttribute('stroke', 'currentColor')
  path.setAttribute('stroke-width', '2')
  path.setAttribute('stroke-linecap', 'round')
  path.setAttribute('stroke-linejoin', 'round')
  svg.appendChild(path)
  span.appendChild(svg)
  return span
}

// Leading whitespace as columns, tabs measured at their tab stops.  The
// editor renders tabs with `state.tabSize` (the view writes it inline), so
// the same measure decides what "deeper indented" means.
function indentOf(text: string, tabSize: number): number {
  return countColumn(/^[ \t]*/.exec(text)?.[0] ?? '', tabSize)
}

/** Fold the indented block a line heads, for grammars with no fold info.
 *  `from` is the header's line end and `to` the last block line's end: both
 *  newlines stay visible, so the next sibling keeps its own line. */
function indentFold(
  state: EditorState,
  lineStart: number,
): { from: number; to: number } | null {
  const header = state.doc.lineAt(lineStart)
  if (header.number >= state.doc.lines || !header.text.trim()) return null
  const base = indentOf(header.text, state.tabSize)
  const stop = Math.min(state.doc.lines, header.number + MAX_FOLD_LINES)
  // Reaching the document end closes a block; reaching the cap does not — a
  // partial range would hide the wrong lines, so it yields no fold at all.
  let closed = stop === state.doc.lines
  let last = -1
  for (let n = header.number + 1; n <= stop; n++) {
    const line = state.doc.line(n)
    if (!line.text.trim()) continue // blank lines inside a block belong to it
    if (indentOf(line.text, state.tabSize) <= base) {
      closed = true
      break
    }
    last = line.to
  }
  return last >= 0 && closed ? { from: header.to, to: last } : null
}

// How far the bracket scan may walk when looking for the closing bracket.
// It only binds when the pair is very large or the opener never closes.
const BRACKET_SCAN = 50_000

/** A bracket opens a fold only from code — a bracket inside a string or
 *  comment must not, or the plain scan would pair it with a bracket in
 *  another string.  A tree grammar makes a code bracket its own one-character
 *  token, so anything else (a string, comment, regex, or HTML text node) is
 *  rejected outright.  Stream grammars wrap lines in coarse tokens, so there
 *  the token's string-like name is what separates code from prose. */
function isCodeBracket(
  state: EditorState,
  pos: number,
  ch: string,
  stream: boolean,
): boolean {
  const node = syntaxTree(state).resolveInner(pos, 1)
  if (!stream) {
    return node.from === pos && node.to === pos + 1 && node.type.name === ch
  }
  return !/string|comment|char|regexp|regex|quote|template|jsx|text/i.test(node.type.name)
}

/** Fold a bracket pair's content when the closing bracket is on a later line,
 *  keeping both brackets visible (`call(…)`).  Only the first multi-line
 *  opener on the queried line is offered; every other line returns null, so
 *  the grammar's own folds keep working. */
function bracketFold(
  state: EditorState,
  lineStart: number,
  stream: boolean,
): { from: number; to: number } | null {
  const line = state.doc.lineAt(lineStart)
  for (let i = 0; i < line.length; i++) {
    const ch = line.text[i]
    if (ch !== '(' && ch !== '[' && ch !== '{') continue
    const pos = line.from + i
    if (!isCodeBracket(state, pos, ch, stream)) continue
    const match = matchBrackets(state, pos, 1, { maxScanDistance: BRACKET_SCAN })
    if (!match?.matched || !match.end) continue
    if (state.doc.lineAt(match.end.from).number === line.number) continue
    return { from: match.start.to, to: match.end.from }
  }
  return null
}

/** Fold the leading run of top-level imports.  Only the first import's line
 *  answers with a range, so the block carries a single marker; every other
 *  line returns null and keeps its grammar's own folds.  Comments and blank
 *  lines inside the run fold with it; a leading module docstring (Python) or
 *  directive (JS) may precede it, and any other statement ends it — which is
 *  also what keeps a function-local import out of the block. */
function importFold(nodeName: string, importLine: RegExp) {
  return (state: EditorState, lineStart: number): { from: number; to: number } | null => {
    const line = state.doc.lineAt(lineStart)
    if (!importLine.test(line.text)) return null
    let firstStart = -1
    let lastEnd = -1
    for (let node = syntaxTree(state).topNode.firstChild; node; node = node.nextSibling) {
      const name = node.type.name
      if (name === nodeName) {
        if (firstStart < 0) firstStart = node.from
        lastEnd = node.to
        continue
      }
      if (name === 'Comment' || name === 'LineComment' || name === 'BlockComment') continue
      const inner = node.firstChild
      if (
        firstStart < 0 && name === 'ExpressionStatement' &&
        inner?.type.name === 'String' && !inner.nextSibling
      ) continue
      break
    }
    if (firstStart < 0) return null
    const start = state.doc.lineAt(firstStart)
    const end = state.doc.lineAt(lastEnd)
    if (line.from !== start.from || end.number === start.number) return null
    return { from: start.to, to: end.to }
  }
}

/** The folding extensions every buffer shares — editable, read-only, and the
 *  empty state alike.  `language` picks the sources: a tree grammar folds by
 *  syntax, plus its leading import run and multi-line bracket pairs; a stream
 *  grammar by bracket or indentation; plain text by indentation alone. */
export function foldingExtensions(language: Language | null): Extension[] {
  const imports = importFoldInfo(language)
  const stream = language instanceof StreamLanguage
  return [
    // Also installs codeFolding()/foldState — adding it again would duplicate
    // the state field.  The square markers (and the hidden width spacer)
    // reserve one fixed column on every line.
    foldGutter({ markerDOM }),
    // foldGutter does not install the chords; the read-only branch has no
    // keymap of its own, so this is the only way to fold one by keyboard.
    keymap.of(foldKeymap),
    // The import service only answers on the first import's line and returns
    // null elsewhere, so it never shadows the grammar's own folds.
    ...(imports ? [foldService.of(importFold(imports.node, imports.line))] : []),
    // Multi-line bracket pairs, in any grammar-backed buffer; prose (plain
    // text) keeps none.  Registered before the indentation fallback so a
    // brace block folds to its bracket rather than to its indent.
    ...(language
      ? [foldService.of((state, lineStart) => bracketFold(state, lineStart, stream))]
      : []),
    // A foldService is consulted before syntax folders, so registering the
    // indentation fallback for a tree grammar would shadow its real folds.
    ...(language === null || language instanceof StreamLanguage
      ? [foldService.of(indentFold)]
      : []),
  ]
}
