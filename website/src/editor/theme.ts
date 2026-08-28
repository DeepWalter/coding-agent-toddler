import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'
import { EditorView } from '@codemirror/view'

/**
 * Editor chrome + token colors.  Token colors read the palette variables in
 * styles.css (each theme defines a --keyword/--function/... set), and the
 * console's markdown highlighter (`markdown.ts`) renders with this same
 * HighlightStyle — editor and console share one token→color mapping, and the
 * whole app stays on one palette even if it is retuned.
 */

// One rule per granular syntax variable in styles.css: keywords → --keyword,
// calls/defs → --function, strings → --string, numbers → --number, booleans
// and atoms/builtins → --constant, comments → --comment italic, plain
// identifiers → --variable, operators → --operator, html tag names → --tag,
// attribute names → --attribute, types → --type, decorators/at-rules
// (tags.meta) → --type italic, links → --accent, diff deletions → --error.
// Params (tags.local) are intentionally left unstyled — the default --text.
export const highlightStyle = HighlightStyle.define([
  {
    tag: [
      tags.keyword,
      tags.controlKeyword,
      tags.moduleKeyword,
      tags.operatorKeyword,
      tags.definitionKeyword,
    ],
    color: 'var(--keyword)',
  },
  // Function calls and definitions — the tree-based grammars (lang-python,
  // lang-javascript, …) tag these function(variableName) etc., more specific
  // than plain variableName.  The legacy stream grammars never emit the
  // function modifier, so in fallback languages calls/defs fall through to
  // the --variable rule below.
  {
    tag: [
      tags.function(tags.variableName),
      tags.function(tags.propertyName),
      tags.function(tags.definition(tags.variableName)),
    ],
    color: 'var(--function)',
  },
  {
    tag: [tags.string, tags.special(tags.string), tags.regexp, tags.character, tags.escape],
    color: 'var(--string)',
  },
  { tag: [tags.number], color: 'var(--number)' },
  // True/None/atoms and legacy-mode "builtin" tokens (variableName.standard,
  // e.g. self) — separated from numbers.
  {
    tag: [tags.bool, tags.atom, tags.literal, tags.standard(tags.variableName)],
    color: 'var(--constant)',
  },
  { tag: [tags.comment, tags.quote], color: 'var(--comment)', fontStyle: 'italic' },
  // Plain identifiers ("def" tokens too) map to --variable (yellow in the
  // canonical solarized scheme); params (tags.local) are intentionally left
  // unstyled — the default --text.
  {
    tag: [tags.variableName, tags.propertyName, tags.definition(tags.variableName)],
    color: 'var(--variable)',
  },
  { tag: [tags.operator], color: 'var(--operator)' },
  // html/xml tags vs attributes
  { tag: [tags.tagName], color: 'var(--tag)' },
  { tag: [tags.attributeName], color: 'var(--attribute)' },
  { tag: [tags.typeName, tags.className, tags.namespace], color: 'var(--type)' },
  // Python decorators (@dataclass), CSS at-rules (@media), yaml `---` —
  // "meta" in the legacy grammars.  Orange, distinct from the gray of a
  // real comment.  No italic: the tree grammars tag only the `@` as meta
  // while the decorator name is a plain variable, so italic would single
  // out the punctuation and look inconsistent.
  { tag: [tags.meta], color: 'var(--type)' },
  { tag: [tags.link, tags.url], color: 'var(--accent)' },
  { tag: [tags.deleted, tags.invalid], color: 'var(--error)' },
])

// Matches the old `.editor-textarea` look (styles.css): --editor-bg
// background, --text color, 13px/1.5 mono, 10px 12px padding, tab-size 2.
// The line number gutter sits on --editor-surface-2; numerals are
// --line-number, the active line's brighter on --active-line.  Selection
// and caret use the theme's dedicated vars.
export const editorTheme = EditorView.theme({
  '&': {
    height: '100%',
    backgroundColor: 'var(--editor-bg)',
    color: 'var(--text)',
    fontSize: '13px',
    lineHeight: '1.5',
  },
  '.cm-content': {
    padding: '10px 12px',
    fontFamily: 'var(--mono)',
    caretColor: 'var(--cursor)',
    tabSize: '2',
  },
  '.cm-scroller': {
    fontFamily: 'var(--mono)',
  },
  '.cm-gutters': {
    backgroundColor: 'var(--editor-surface-2)',
    color: 'var(--line-number)',
    borderRight: 'none',
  },
  '.cm-lineNumbers .cm-gutterElement': {
    padding: '0 8px 0 12px',
  },
  '.cm-lineNumbers .cm-activeLineGutter': {
    color: 'var(--line-number-active)',
  },
  '.cm-activeLine': {
    backgroundColor: 'var(--active-line)',
  },
  '&.cm-focused': { outline: 'none' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--cursor)' },
  // CM's base theme paints the selection layer with literal light-mode colors
  // (&light .cm-selectionBackground → #d9d9d9 / #d7d4f0) that outrank this
  // rule on specificity, so the dark theme showed a near-white selection.
  // !important wins over those literals and keeps the palette var in charge.
  '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {
    backgroundColor: 'var(--selection) !important',
  },
})

export const highlightExt = syntaxHighlighting(highlightStyle)
