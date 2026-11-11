# Plan: Git diff gutter and overview ruler in the web editor

## Context

The web editor (`website/src/components/FileEditor.vue`) shows file content but
no working-tree state. VS Code-style SCM decorations make it obvious what the
last commit does not contain: green bars for added lines, blue bars for modified
lines, red triangles for deletion points, plus matching colored ticks along the
right-hand scrollbar. This adds both, driven by a read-only git endpoint and
CodeMirror extensions.

Decorations are **live against HEAD while typing**: each file tab fetches its
HEAD version and a client-side diff is rerun on a short debounce after every
edit. The one new dependency is CodeMirror's own `@codemirror/merge`, whose
line-oriented `Chunk` replaces a hand-rolled diff.

## Semantics

- Baseline is **HEAD**, so staged and unstaged changes are both shown. An
  untracked or newly added file has no HEAD side → every line is green.
  Committing makes the baseline equal the buffer → decorations disappear.
- Classification follows VS Code:
  - insertion only → `add` range (green);
  - replacement (both sides non-empty) → `mod` range (blue) covering the
    new-side lines;
  - deletion only → `del` marker (red triangle) at the line where the empty
    new-side range begins — the line that moved up into the gap. A chunk at
    EOF resolves to the last line; the state builder still clamps to
    `[1, doc.lines]`.
- Binary, non-UTF-8 HEAD content, unmerged conflicts, non-repositories, and
  missing git produce no decorations while the file still opens normally.
- Text tabs, markdown tabs, and external absolute paths get no SCM machinery.

## Backend: `GET /api/git/baseline?path=<rel>`

`toddler/web/git.py` grows `git_baseline`; `toddler/web/api.py` wires the route
with the same `_diff_error` mapping as the other git endpoints.

Response:

```json
{
  "path": "src/foo.py",
  "available": true,
  "exists": true,
  "binary": false,
  "unmerged": false,
  "content": "the file at HEAD…"
}
```

- `resolve_relative` gates containment; directory → 400; escaping/null path →
  400 (the same guards as `_diff_raw`).
- `_path_state` first: an `unmerged` path returns `unmerged: true` with empty
  content.  Every other kind continues — the real question is whether HEAD has
  a blob at the path, not what the index says (this also handles a staged
  deletion recreated in the worktree).
- Not a repository or git unavailable → `available: false` (degrade like
  `git_status`, rather than a 500). This stays distinct from `exists: false`:
  the latter means "no HEAD side", which the editor paints as every line
  added, and would be wrong for a repository it cannot read.
- Unborn branch: `git rev-parse --verify --quiet HEAD` non-zero → `exists:
  false`.
- Otherwise `git cat-file blob HEAD:<rel>` (raw blob, no diff/filter). A
  non-zero exit means the path is not in HEAD (untracked, staged-new, or
  renamed) → `exists: false`.
- Output bytes containing NUL or failing UTF-8 decoding → `binary: true`,
  `content: ""` (the editor itself only opens text).
- Raw `\r\n` bytes stay in `content`; the frontend diff normalizes line
  endings.

No `_APPLY_LOCK` for a read.

## Frontend line diff

`Chunk.build(a: Text, b: Text, { scanLimit, timeout })` from
`@codemirror/merge` returns line-boundary chunks: `fromA/toA/fromB/toB` are
character offsets, `endA`/`endB` the clamped safe ends.

`website/src/editor/lineDiff.ts` (pure; only a type-only `Text` import):

- `splitLines(text)` — `text.split('\n')`, strip one trailing `\r` per line.
  This matches CodeMirror's line count (a trailing newline yields a final empty
  line) and makes LF↔CRLF-only differences invisible.
- `chunksToLineChanges(chunks, current)` — start line
  `doc.lineAt(clamp(from)).number`; a non-empty side's last line
  `doc.lineAt(clamp(to - 1)).number` (a position at a line's newline belongs to
  that line); `a` empty → `add`, `b` empty → `del`, both non-empty → `mod`.
  Exports `ScmLineChange` (`kind: 'add' | 'mod' | 'del'; start; end`, 1-based).

`website/src/editor/scm.ts`:

- `computeScmChanges(baseline, current)` returns `[]` immediately when the
  strings are equal, then builds `Text.of(splitLines(...))` for both sides and
  calls `Chunk.build(a, b, { scanLimit: 100_000, timeout: 50 })`.
  `Chunk.build` does **not** apply MergeView's `scanLimit: 500` default, so
  passing one avoids its large-input fast fallback; the 50 ms timeout bounds a
  pathological rewrite. An imprecise result sets `chunk.precise = false`, but
  the chunks remain usable.

## Frontend decorations: `website/src/editor/scm.ts`

One module-level extension bundle, reused by every SCM-enabled state:

- `scmField: StateField<{ markers: RangeSet<GutterMarker>; entries }>`.
  - `entries` are `{ kind, from, to }` document offsets for the ruler.
  - On `tr.docChanged` the marker range set is mapped with `changes.map` and
    the entries with `changes.mapPos` (start assoc -1, end assoc 1), so marks
    shift with the buffer until the debounced recompute replaces them.
  - `setScmChanges = StateEffect.define<ScmLineChange[]>()`; the effect rebuilds
    both values from `tr.state.doc`: one point `GutterMarker` per changed line
    at `line.from`, using three singleton marker subclasses whose
    `elementClass` is `cm-scm-add` / `cm-scm-mod` / `cm-scm-del`; one entry per
    range (`from = doc.line(first).from`, `to = doc.line(last).to`), deletion
    entries being points.
- `scmGutter = gutter({ class: 'cm-scmGutter', markers: … })`, spread **before**
  `lineNumbers()` in `buildExtensions`, so the SCM column is leftmost.
- `scmRuler` is a `ViewPlugin`:
  - appends `<div class="cm-scmRuler" aria-hidden="true">` to `view.dom`
    (`.cm-editor` is `position: relative`) and removes it in `destroy()`;
  - repaints when the field reference changes, `docChanged`, `heightChanged`,
    or `geometryChanged` (not on plain scrolling);
  - uses `view.requestMeasure({ key: this, read, write })`; `read` obtains
    `scrollDOM.clientHeight`, `scrollDOM.offsetWidth - clientWidth` (classic
    scrollbar width; 0 for macOS overlay scrollbars), and per-entry
    `lineBlockAt(pos)`; maps `top = (documentPadding.top + block.top) /
    contentHeight * clientHeight`, height `max(2, bottom - top)` scaled the same
    way. `write` sets the ruler height/right offset and replaces tick children
    (`cm-scmTick add|mod|del`). Deletion ticks are 2px at the line center.
  - retries briefly when the editor is hidden and reports zero height.

### Theme (`website/src/editor/theme.ts`)

Using the palette variables already defined in all four themes:

- `.cm-scmGutter` — fixed ~6px column, zero padding on its elements.
- `...cm-scm-add::before` / `...cm-scm-mod::before` — 3px full-height bars in
  `var(--success)` / `var(--accent)`.
- `...cm-scm-del::after` — a right-pointing 5px triangle in `var(--error)`,
  vertically centered. A separate pseudo-element lets a clamped EOF deletion
  coexist with a bar on the same line.
- `.cm-scmRuler` — absolute, `pointer-events: none`, z-index above content
  (below panels), width ~10px; ticks 6px wide, 2px minimum height.

## FileEditor wiring

`EditorTab` gains `baseline: string | null` (HEAD text, `''` = no HEAD version,
`null` = unavailable) and `baselineFetched: boolean`.

- **Extensions**: `buildExtensions(tab, language, readOnly, scm = false,
  onDocChange?)` and `buildState(..., scm, onDocChange?)`. The SCM bundle is
  added only for internal file tabs (`!tab.readOnly`); text tabs and
  `emptyState` pass `false`. The editable update listener keeps
  `tab.dirty = true` and calls `onDocChange` on `docChanged`.
- **Baseline fetch** (`fetchBaseline(path, tab)`): skips external/absolute
  paths; per-path sequence token (mirroring `fetchTokens`); calls
  `api.gitBaseline(path)`; on success sets `baselineFetched`, sets `baseline =
  payload.exists ? content : ''` when `available && !binary && !unmerged`,
  else `null`; then schedules a recompute. On error it marks fetched/
  unavailable and clears the marks.
- **Recompute** (`scheduleScm(path, delay = 250)` → `recomputeScm`): clears/
  reschedules a per-path timer; reads `tab.baseline` and the live
  `view.state.doc` when the path is `currentKey`, else `tab.state.doc`; runs
  `computeScmChanges`; applies with
  `view.dispatch({ effects: setScmChanges.of(changes) })` for the active tab or
  `tab.state.update({ effects })` for an inactive one. Effects-only
  transactions leave undo/history/dirty untouched.
- **Triggers**:
  - `loadTab` starts the baseline fetch alongside the file read;
  - `switchToTab` refetches the baseline on activation (HEAD may have moved) and
    schedules a recompute;
  - `refreshTab` schedules a recompute after rebuilding state;
  - `save` schedules a recompute against the just-saved buffer;
  - a watcher on `props.git.files` object identity refetches the active tab's
    baseline on every git-status refresh (commit, checkout, stage/discard, agent
    turn end, save), catching HEAD movement even when the badge letter or branch
    did not change;
  - `removeTab` clears its timer and tokens; `onUnmounted` clears all timers.
- A recompute that lands after a tab switch still applies to the right tab
  (`currentKey` decides dispatch vs `state.update`), and the recompute reads the
  document synchronously, so there is no line-number drift.

## Tests and fixtures

- `tests/test_web_git.py` — `TestBaselineEndpoint`: clean, modified,
  staged+unstaged, untracked, staged-new, unborn HEAD, empty file at HEAD,
  binary, non-UTF-8, unmerged, non-repo, missing git, missing path,
  directory/escape paths, spaces/unicode, CRLF, and rename behavior.
- `website/scripts/line-diff-test.mjs` — `node:test` over the chunk conversion:
  equal, insert/delete at start/middle/end, replacements with unequal counts,
  separate changes, empty/whole-file, CRLF-only, final newline changes, and
  clamped ends via hand-made chunks.
- `website/scripts/mock-server.mjs` — a dedicated `/api/git/baseline` branch
  (before the generic `/api/*` fallback) with a 20-line baseline and a worktree
  fixture whose modification, deletion, and addition are far enough apart to
  stay distinct chunks. Unknown paths answer `available: false`, so earlier
  phases gain no marks.
- `website/scripts/ui-test.mjs` phase 26 — gutter markers align with their line
  numbers, the overview ruler's three tick kinds are in document order, and
  typing on a fresh line plus appending a line update the marks live after the
  debounce without a save.

## Verification

```bash
# backend
.venv/bin/python -m pytest tests/test_web_git.py -q
.venv/bin/ruff check toddler/web/git.py toddler/web/api.py tests/test_web_git.py

# pure line-diff conversion (Node 24 strips the TS types)
node --test website/scripts/line-diff-test.mjs

# frontend typecheck + build
cd website && npm run build

# UI (fresh mock per run)
node website/scripts/mock-server.mjs                       # terminal 1
cd website && npx vite --config mock-vite.config.ts        # terminal 2
BASE_URL=http://localhost:5199/ PW_CHANNEL=chrome \
  node website/scripts/ui-test.mjs                         # terminal 3
```

Manual: open a modified file — green bars for added lines, blue for modified
lines, a red triangle where a deletion occurred, matching ticks along the right
edge; type and watch marks update after the debounce; commit from Source Control
and they clear; an untracked file is entirely green; an external file and a
non-repo directory show none; both themes render.

## Known limitations

- Pathologically large rewrites can exceed CodeMirror's `scanLimit`/`timeout`
  and produce an imprecise (coarser) chunk set rather than exact classification.
- Renamed files whose old path is outside the pathspec read as fully added until
  the rename is committed (the same limitation as `GET /api/git/diff`).
- A missing/added final newline can surface as a modification of the last line;
  deleting a final line from a file with no trailing newline can read as a
  modification of the line above rather than a deletion marker.
- CodeMirror merges changes separated by less than about one line into one
  chunk; a merged add+mod block reads as blue (modified), and a merged change
  adjacent to a deletion loses the separate red triangle.
- Changes inside closed folds do not render in the gutter (CodeMirror only
  builds markers for visible lines); the ruler still maps them to the fold.
- HEAD content is fetched per file tab and kept in memory; there is no cross-tab
  cache.
- Binary HEAD content, unmerged files, non-repositories, and external files get
  no decorations by design.
