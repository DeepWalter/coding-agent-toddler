import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'
import { EditorView } from '@codemirror/view'

/**
 * Editor chrome + token colors.  Token colors read the palette variables in
 * styles.css, and the console's markdown highlighter (`markdown.ts`) renders
 * with this same HighlightStyle — editor and console share one token→color
 * mapping, and the whole app stays on one palette even if it is retuned.
 */

// keywords/titles/names → --accent; strings → --green; types → --violet;
// numbers/literals/builtins/variables → --yellow; comments → --text-dim
// italic; decorators/at-rules (tags.meta) → --violet italic; deletions →
// --red.  Params (tags.local) are intentionally left unstyled — the
// default --text.
export const highlightStyle = HighlightStyle.define([
  {
    tag: [
      tags.keyword,
      tags.controlKeyword,
      tags.moduleKeyword,
      tags.operatorKeyword,
      tags.definitionKeyword,
    ],
    color: 'var(--accent)',
  },
  {
    tag: [tags.string, tags.special(tags.string), tags.regexp, tags.character, tags.escape],
    color: 'var(--green)',
  },
  { tag: [tags.typeName, tags.className, tags.namespace], color: 'var(--violet)' },
  {
    tag: [
      tags.number,
      tags.bool,
      tags.atom,
      tags.literal,
      // legacy-mode "builtin" tokens map to variableName.standard
      tags.standard(tags.variableName),
      tags.variableName,
      tags.propertyName,
      tags.definition(tags.variableName), // "def" tokens
    ],
    color: 'var(--yellow)',
  },
  { tag: [tags.comment, tags.quote], color: 'var(--text-dim)', fontStyle: 'italic' },
  // Python decorators (@dataclass), CSS at-rules (@media), yaml `---` —
  // "meta" in the legacy grammars.  Violet italic reads as "annotation",
  // distinct from the gray of a real comment.
  { tag: [tags.meta], color: 'var(--violet)', fontStyle: 'italic' },
  { tag: [tags.deleted], color: 'var(--red)' },
  // html/xml tags and attributes
  { tag: [tags.tagName, tags.attributeName], color: 'var(--accent)' },
])

// Matches the old `.editor-textarea` look (styles.css): --bg background,
// --text color, 13px/1.5 mono, 10px 12px padding, tab-size 2.  The line
// number gutter sits on --bg-hover with dim --text-dim numerals.
export const editorTheme = EditorView.theme({
  '&': {
    height: '100%',
    backgroundColor: 'var(--bg)',
    color: 'var(--text)',
    fontSize: '13px',
    lineHeight: '1.5',
  },
  '.cm-content': {
    padding: '10px 12px',
    fontFamily: 'var(--mono)',
    caretColor: 'var(--accent)',
    tabSize: '2',
  },
  '.cm-scroller': {
    fontFamily: 'var(--mono)',
  },
  '.cm-gutters': {
    backgroundColor: 'var(--bg-hover)',
    color: 'var(--text-dim)',
    borderRight: 'none',
  },
  '.cm-lineNumbers .cm-gutterElement': {
    padding: '0 8px 0 12px',
  },
  '&.cm-focused': { outline: 'none' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--accent)' },
  '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {
    backgroundColor: 'var(--bg-hover)',
  },
})

export const highlightExt = syntaxHighlighting(highlightStyle)
