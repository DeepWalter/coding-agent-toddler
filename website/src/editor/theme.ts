import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'
import { EditorView } from '@codemirror/view'

/**
 * Editor chrome + token colors.  Token colors mirror the highlight.js →
 * palette mapping in styles.css (`.message-content.markdown .hljs-*`), and
 * the theme uses the same CSS variables, so the whole app stays on one
 * palette even if it is retuned.
 */

// keywords/titles/names → --accent; strings/types → --green;
// numbers/literals/builtins/variables → --yellow; comments → --text-dim
// italic; deletions → --red.  Params (tags.local) are intentionally left
// unstyled — the default --text, matching `.hljs-params`.
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
  { tag: [tags.typeName, tags.className, tags.namespace], color: 'var(--green)' },
  {
    tag: [
      tags.number,
      tags.bool,
      tags.atom,
      tags.literal,
      // "builtin" tokens (hljs built_in) map to variableName.standard
      tags.standard(tags.variableName),
      tags.variableName,
      tags.propertyName,
      tags.definition(tags.variableName), // "def" tokens
    ],
    color: 'var(--yellow)',
  },
  { tag: [tags.comment, tags.quote, tags.meta], color: 'var(--text-dim)', fontStyle: 'italic' },
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
