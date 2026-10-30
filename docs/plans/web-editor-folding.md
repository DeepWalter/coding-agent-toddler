# Plan: Code Folding in the Web Editor

## Context

The web UI's editor pane is a single CodeMirror 6 view
(`website/src/components/FileEditor.vue`) with syntax highlighting but no way to
collapse code. Long Python/TS files and nested config files can only be
scrolled. This adds CodeMirror folding to every buffer: a chevron gutter,
click-to-unfold placeholders, and the library's fold chords.

Folding has to cover three kinds of buffer:

- **Tree grammars** (Python, JS/TS/JSX, JSON, CSS/SCSS, HTML/XML, Go, Rust,
  Java, C/C++, SQL) whose `@codemirror/lang-*` packages already ship
  `foldNodeProp` ranges.
- **Legacy stream grammars and plain text** (YAML, shell, TOML, Dockerfile,
  unknown extensions) which carry no fold information at all.
- **Read-only buffers** (text tabs, external files), where folding is a reading
  aid just as much as in an editable file.

On top of the grammar folds, Python and JS/TS files fold their leading run of
top-level imports as a single block, and any multi-line bracket pair folds
while keeping both brackets visible.

No new dependency: `@codemirror/language` is already installed and exports
`foldGutter`, `foldKeymap`, and `foldService`.

## Design

### One shared extension set

`website/src/editor/folding.ts` exports `foldingExtensions(language)`:

- `foldGutter()` — the marker column and click handler. It also returns
  `codeFolding()`, which installs the `foldState` field, so calling
  `codeFolding()` again would duplicate that field. The gutter sits after
  `lineNumbers()` in `FileEditor.vue`, i.e. to the right of the line numbers.
- `keymap.of(foldKeymap)` — `foldGutter` does not install the chords. This also
  gives read-only buffers keyboard folding: their branch has no keymap of its
  own, and a fold is an effect, not a doc change, so `EditorState.readOnly` does
  not block it.
- `foldService.of(indentFold)` — only for `null` and `StreamLanguage` buffers.
  `foldable()` consults registered services *before* syntax folders, so
  installing the fallback for a tree grammar would shadow that grammar's real
  folds.
- `foldService.of(importFold(…))` — Python and JS/TS only; the leading import
  run (see below).
- `foldService.of(bracketFold)` — every grammar-backed buffer; multi-line
  bracket pairs (see below).

Because these extensions live in the shared tail of `buildExtensions`, editable
tabs, read-only tabs, text tabs, and the empty state all get folding without
touching the editable/read-only branch.

### Indentation fallback

`indentFold(state, lineStart)`:

- Blank or whitespace-only header lines, and the document's last line, are not
  foldable.
- Base indentation is the header's leading whitespace measured in columns via
  `countColumn` at `state.tabSize` (4 — the view writes that inline; the theme's
  `tabSize: '2'` rule is dead). Mixed tabs/spaces therefore compare by columns.
- Scan forward: blank lines belong to the block; the first non-blank line at or
  below the base indentation closes it; otherwise the last deeper line is the
  block's end.
- Return `{ from: header.to, to: last.to }`. Leaving both newlines visible is
  what keeps the next sibling on its own line.
- Scans stop after 2000 lines. Hitting that cap yields no fold at all — a
  partial range would hide the wrong lines, and the gutter calls `foldable`
  for every viewport line on each rebuild.

### Import folds

`languages.ts` also names each grammar's top-level import node (`ImportStatement`
for Python, `ImportDeclaration` for JS/TS) with a cheap line prefilter.
`importFold` in `folding.ts` walks the file's top-level nodes from the start,
collecting imports while stepping over comments and a leading module docstring
(Python) or directive (JS); the first other statement ends the run. The fold
range runs from the end of the first import's line to the end of the last
import's line, so the first import stays visible and only that line carries a
marker. Every other line returns null, so grammar folds are never shadowed.
Function-local imports are not top-level nodes and stay out of the block, as do
imports that appear after other statements.

### Bracket folds

`bracketFold` folds a bracket pair whose closer is on a later line, from just
after the opener to just before the closer — a call renders as `call(…)`, both
brackets visible. `matchBrackets` from `@codemirror/language` supplies the
match, syntax-aware where the grammar declares bracket tokens and scanning
otherwise, capped at 50,000 characters. Only the line holding the opener offers
the fold; every other line returns null. A guard keeps openers to code: in a
tree grammar the bracket must be its own one-character token (ruling out
strings, comments, regexes, and HTML text/attribute nodes); in a stream grammar
the token name must not look string-like. Without it the plain scan would pair
a bracket inside one string with a bracket inside another. Grammar-backed
buffers only; plain text keeps its indentation folds and gets no bracket folds.
The service is registered before the indentation fallback, so a shell/YAML
brace line folds to its bracket rather than to its indent.

### Theme

`website/src/editor/theme.ts` styles the new chrome from the existing palette
(all four theme blocks define these vars; the package's base-theme literals are
overridden by the normal editor theme):

- `.cm-foldGutter` — `--line-number` at rest (gutter chrome, not syntax color).
- `.cm-foldGutter .cm-gutterElement:hover` — `--accent-hover` on
  `--editor-surface-3`.
- `.cm-foldPlaceholder` — a chip on `--editor-surface-2` with an
  `--editor-border` border and `--text-subtle` text, brightening on hover.

The gutter's width spacer reserves roughly one glyph column on every buffer,
including files with nothing foldable. Markers are viewport-scoped.

## Known limitations

- Folds persist across tab switches because they live in the tab's stored
  `EditorState` (`flushCurrentFile`). They reset whenever `buildState` rebuilds
  that state: a disk refresh after the file changed, a page reload, and every
  text-tab activation (`showTextTab` always rebuilds — undo history resets there
  today too).
- `applyPendingReveal` selects text inside a fold. CodeMirror clears folds whose
  interior contains the selection head, so a normal reveal unfolds itself; a
  selection that merely spans a closed fold leaves it closed.
- Markdown headings do not fold: `.md` maps to plain text, which has no
  indentation blocks and no markdown grammar.
- Import folding covers Python and JS/TS only, and only the leading run at the
  top of a file; a single-line solo import has nothing to hide.
- Bracket folds need a grammar-backed buffer (plain text and markdown get none)
  and a closer within 50,000 characters; brackets inside strings or comments
  never fold.
- Windows ignores Ctrl-Alt character chords (AltGr), so fold-all/unfold-all are
  keyboard-only and Mac/Linux-only; gutter clicks and Ctrl-Shift-[ still work.

## Changes

1. `website/src/editor/folding.ts` — new module: `indentFold`, `importFold`,
   `bracketFold` (with its string/comment guard), the square SVG `markerDOM`,
   and `foldingExtensions`.
2. `website/src/editor/languages.ts` — `importFoldInfo`: the grammar's import
   node name and line prefilter, for Python and JS/TS.
3. `website/src/editor/theme.ts` — fold gutter and placeholder rules.
4. `website/src/components/FileEditor.vue` — import and spread
   `foldingExtensions(language)` after `lineNumbers()`; doc comment sentence.
5. `website/scripts/mock-server.mjs` — `mock-fold.py`, `mock-fold.yaml`,
   `mock-fold-imports.{py,ts}`, and `mock-fold-brackets.{py,sh}` fixtures.
6. `website/scripts/ui-test.mjs` — phase 21: Python tree folds, YAML
   indentation folds (including the equal-indent boundary), placeholder unfold,
   and fold survival across a tab round trip; phase 22: leading import runs in
   Python and TS, including the docstring/comment handling and a function-local
   import staying out; phase 23: a Python call and a shell brace body fold
   between their brackets, while a same-line pair and brackets inside strings
   stay unfoldable.

## Verification

```bash
cd website && npm run build     # vue-tsc --noEmit + vite build

# terminal 1 (repo root): protocol mock, serves website/dist, stateful
node website/scripts/mock-server.mjs
# terminal 2: app dev server, proxies /api + /ws to :8100
cd website && npx vite --config mock-vite.config.ts
# terminal 3 (repo root): restart the mock between runs
BASE_URL=http://localhost:5199/ PW_CHANNEL=chrome node website/scripts/ui-test.mjs
```

Manual: open a Python file and a YAML file — chevrons on foldable lines, click
to fold/unfold, the placeholder follows the active theme, `Cmd-Alt-[`/`]` work,
saving still writes the whole document, and folds survive tab switches.
