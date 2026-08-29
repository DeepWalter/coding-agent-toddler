# Source Control Panel + Side-by-Side Git Diff for the Web UI

## Context

The web UI (Vue 3 frontend in `website/`, FastAPI backend in `toddler/web/`) currently only *badges* changed files in the explorer via `GET /api/git/status`. There is no way to see *what* changed. This adds a VSCode-like source-control experience:

- **VSCode-style activity bar** (icon rail on the far-left edge) switching the left pane between Explorer and Source Control; clicking the active icon toggles the pane.
- **Source Control panel** with two collapsible sections — **Staged Changes** and **Changes** (unstaged) — listing changed files with letter badges + counts. `MM` files appear in both sections; `??` untracked only in Changes.
- **Side-by-side diff view** (VSCode default) in the editor pane: two columns, line-number gutters, green/red lines, aligned del/add runs, synced vertical scroll, independent horizontal scroll, hunk headers. Handles untracked (empty old side), deleted (empty new side), binary (message).
- Staged diffs = index vs HEAD; unstaged = worktree vs index.

No unified-diff parsing or diff endpoint exists anywhere today — this is greenfield on both sides.

## Verified facts (from code + live git experiments)

- `git diff --no-index /dev/null <f>` exits **rc 1 on differences** (success case) and its header is `diff --git a/<path> b/<path>` — never parse paths from header lines (C-quoted, `+++` carries trailing TAB). Parser must only use `--- /dev/null` / `+++ /dev/null` presence.
- Rename detection is defeated by `--` path limiting: an `R` file's diff renders as full-file addition. Acceptable; note as future work.
- `parse_status()` in `toddler/web/git.py:62` is pinned by 12 tests — refactor must preserve its signature/behavior exactly. `test_status_payload_keys` (tests/test_web_git.py:222) asserts the exact key set — update it.
- `_git()` (git.py:121) returns bytes + rc, 30s timeout, swallows errors into empty snapshot in `git_status()` (git.py:140).
- Frontend tab state lives in `App.vue` (`openFiles: string[]`, `activePath`), persisted repo-scoped in localStorage `tod.tabs`. FileEditor.vue owns one CodeMirror view; tab switches flush `view.state`/`scrollTop` (extract this flush for diff-tab switches). Strip `:key` is path-based (line 410) — must become entry-based so a file and its diff can coexist.
- Theme colors only come from CSS vars (`[data-theme="tokyo-night"|"github-light"]` on `<html>`); `color-mix()` derives diff backgrounds from vars (Chrome 111+/FF 113+/Safari 16.2+ — fine).

## Backend

### 1. `toddler/web/git.py` — extend status, add diff

- Refactor `parse_status` into `_parse_records(output) -> (branch, [(path, x, y), ...])` (same NUL framing, `## ` header, rename old-path skip, trailing-slash strip) with `parse_status` as a thin wrapper collapsing via `_letter` — signature unchanged.
- New `build_sections(records) -> (staged: {path: letter}, unstaged: {path: letter})`:
  - unmerged XY pairs (`_UNMERGED`) → `C` in both sections;
  - x-axis in `MADRCT` → staged (C collapses to R, like `_letter`);
  - y-axis in `MADRTU` → unstaged; y `?` (untracked) → unstaged `U`.
- `git_status()` returns `{"branch", "files", "dirs", "sections": {"staged": …, "unstaged": …}}` — `files`/`dirs` unchanged (explorer badges + StatusBar keep working); empty-snapshot error paths include `sections`.
- New `class DiffError(Exception)` with `status_code`/`message` (maps to `{"error": msg}`), and:

```python
async def git_diff(root: Path, rel: str, staged: bool) -> dict:
    # {path, staged, binary, truncated, old_path, new_path, hunks: [{old_start,
    #   old_count, new_start, new_count, lines: [{kind, old_ln, new_ln, text, no_newline}]}]}
```

Logic: `files.resolve_relative(root, rel)` → 400 (reuse `toddler/web/files.py:54` guard); 400 if directory. Tracked/untracked via `git ls-files --error-unmatch -- <rel>` (rc 0/1 — authoritative). Tracked → `git diff --no-color --unified=3 [--staged] -- <rel>` (empty stdout → `{"hunks": []}`, benign stale status). Untracked → require file on disk (else 404), `git diff --no-color --unified=3 --no-index -- /dev/null <rel>`, accept rc 0 and 1 (rc 1 + non-empty stderr → 404; other errors → 500). `old_path`/`new_path` = rel unless parser saw `---/+++ /dev/null` → null. `--unified=3` as module constant `_CONTEXT_LINES`.

### 2. `toddler/web/diffparse.py` — new module (pure, no git)

`__all__ = ["parse_diff", "Hunk", "DiffLine"]`. `DiffLine {kind: "ctx"|"del"|"add", old_ln: int|None, new_ln: int|None, text: str, no_newline: bool}`, `Hunk {old_start, old_count, new_start, new_count, lines}`. `parse_diff(output: bytes, *, max_lines=10_000) -> {"hunks", "binary", "truncated", "old_dev_null", "new_dev_null"}` — single pass over decoded lines:

- `\ No newline at end of file` → set flag on previous line; skip.
- strip one trailing `\r` per line (CRLF repos).
- `Binary files … differ` → `binary=True`, stop.
- `^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@$` → new hunk (handles `-0,0`/`+0,0` naturally).
- outside hunks: only `--- /dev/null` / `+++ /dev/null` matter; skip everything else (`diff --git`, `index`, modes, `similarity index`, `rename from/to`).
- inside hunk: ` ` ctx (both counters), `-` del, `+` add; else skip.
- cap at `max_lines` → `truncated=True`.

### 3. `toddler/web/api.py` — endpoint

```python
@router.get("/git/diff")
async def git_diff(path: str = Query(...), staged: bool = Query(default=False), state: WebAppState = _GetState):
    try: return await git.git_diff(state.repo_root, path, staged=staged)
    except git.DiffError as exc: return JSONResponse({"error": exc.message}, status_code=exc.status_code)
```

Errors 400 (escape/not-a-file), 404 (missing untracked file), 500 (git failure), per `{"error": msg}` convention.

### 4. Tests

- `tests/test_web_git.py`: update `test_status_payload_keys` keys; add `TestBuildSections` (pure): `MM→both M`, `??→unstaged U only`, `AM→A/M`, rename old-path skipped, `UU/AA/DU→C both`, copy → staged R.
- `tests/test_web_diff.py` (new; helpers copied from test_web_git.py — real git repo + TestClient, skipif no git):
  - `TestParseDiff` (canned bytes): standard modify hunk with exact line numbers; new file `-0,0 +1,3` (old_ln None, old_dev_null); deleted `-1,10 +0,0` (new_ln None); binary; no-newline marker; rename/mode headers skipped; two hunks; CRLF strip; `max_lines` truncation; header-only (mode change) → empty hunks.
  - `TestDiffEndpoint` (real repo): unstaged modify; staged vs unstaged same file differ; MM file returns different content per `staged`; untracked (rc 1 accepted, all adds, old_path null); staged delete (`git rm` → all dels, new_path null); binary; `?path=../x` → 400; `?path=dir` → 400; path with spaces/unicode; non-repo → 500.

## Frontend

### 5. `website/src/types.ts`

```ts
export interface GitSectionMap { staged: Record<string, GitStatusLetter>; unstaged: Record<string, GitStatusLetter> }
// GitStatusPayload + GitStatusState gain `sections: GitSectionMap`
export type DiffLineKind = 'ctx' | 'del' | 'add'
export interface DiffLine { kind; old_ln: number|null; new_ln: number|null; text: string; no_newline?: boolean }
export interface DiffHunk { old_start; old_count; new_start; new_count; lines: DiffLine[] }
export interface GitDiffPayload { path; staged; binary; truncated; old_path: string|null; new_path: string|null; hunks: DiffHunk[] }
export type TabEntry = { kind: 'file'; path: string } | { kind: 'diff'; path: string; staged: boolean }
```

### 6. `website/src/api.ts` + `useGitStatus.ts`

- `gitDiff(path, staged)` → `GET /api/git/diff?path=…&staged=0|1` (`cache: 'no-store'`).
- useGitStatus: add `sections` to initial state and copy in `refresh()`. WS hooks/debounce/sequence guard untouched.

### 7. `website/src/App.vue` — activity bar + tab model

- Replace `openFiles`/`activePath` with `openTabs: ref<TabEntry[]>`, `activeTab: ref<TabEntry|null>`, `activeFilePath = computed(...)` (file tabs only). Tab key fn: `f:<path>` / `d:<s|u>:<path>`.
- `openFile(path)` (unchanged semantics, now tab entries; keeps `git.refresh` side effect), new `openDiff(path, staged)`, `closeTab(t)` (same right-then-left fallback). `persistTabs()` writes **file tabs only**; diff-active `active` persists as null (existing restore logic handles). cwd watcher clears both.
- Activity bar state: `leftPane = ref<'explorer'|'sc'|null>(readStored('tod.leftpane', …, 'explorer'))`; `selectPane(k)` toggles. `scCount` computed from sections for the icon badge.
- Template: `.split` gains `<nav class="activity-bar">` (two `<button class="activity-btn">` with SVG icons, active = `box-shadow: inset 2px 0 0 var(--accent)`, badge pill) as first child; the explorer `aside` becomes `v-if="leftPane"` `class="pane pane-left"` hosting `FileExplorer` **or** new `SourceControl`; divider also `v-if`'d with the pane. Rename `.pane-explorer` → `.pane-left` in styles.css (`.pane-left, .pane-editor { flex: none }`). Drag math untouched.
- FileEditor props become `{ files: TabEntry[]; active: TabEntry|null; git }`, emits `activate-tab`/`close-tab`/`file-saved`.

### 8. `website/src/components/SourceControl.vue` — new

Props `{ git: GitStatusState }`; emits `open-diff(path, staged)`, `refresh-git`. Header (title, branch name or "no repository", ↻ refresh disabled while loading). Two collapsible sections (local `collapsed` ref record, chevron, counts): **Staged Changes** / **Changes** from `git.sections.staged` / `.unstaged` — sorted keys, dim dir-prefix + basename, right-aligned `.git-badge` (reuse `gitBadgeClass` from `src/gitStatus.ts`), click → `open-diff`. Muted empty states per section; single "not a git repository" line when `branch` null. `MM` files naturally appear in both.

### 9. `website/src/diff.ts` — new pure alignment

`buildRows(hunks): DiffRow[]` — `DiffRow {header: string|null, old: DiffLine|null, new: DiffLine|null}`. Per hunk: header row first; ctx → pair; del/add runs buffer and flush as positional pairs (`n = max(dels, adds)`, `old = dels[i] ?? null`). (LCS pairing = documented future work.)

### 10. `website/src/components/DiffView.vue` — new

Props `{ tab: TabEntry(diff), git }`; emit `open-file`. Fetch on mount + watch tab key via `api.gitDiff` with a per-mount stale guard (same idea as FileEditor's fetch tokens); DiffView remounts per activation so diffs refetch fresh after agent writes. Header: path, letter badge (from `git.sections[staged?'staged':'unstaged'][path] ?? git.files[path]`), staged/unstaged tag, **Open File** button, ↻ refresh, status text (loading / error / binary / "N deleted, M added" / truncated note).

Body: binary → centered message; no hunks → "No changes"; else two scrollable columns (`<div class="diff-col">` each with sticky column header old_path/new_path, `@scroll` syncs the other column's `scrollTop` when `|diff| > 1`) rendering the same `buildRows` rows: gutter (`old_ln`/`new_ln` right-aligned, sticky left) + text (`white-space: pre`). Classes `ctx`/`del`/`add`/`hunk`; empty cells are `min-height`-pinned empty spans (exact row heights = exact sync); `no_newline` rows get a dim "no newline at end of file" note.

### 11. `website/src/components/FileEditor.vue` — coexist with diff tabs

- Strip: `v-for="tab in files" :key="tabKey(tab)"`, name `basename(tab.path)`, badges only for file tabs, dim staged/unstaged tag on diff tabs, dirty dot only for files, close: dirty-confirm for files, immediate for diffs.
- Active watcher branches: diff tab → extract `flushCurrentFile()` from `switchToTab` (save `view.state` + `scrollTop` into the outgoing file tab), `currentPath = null`, `view.setState(emptyState)`, blur — CM host stays mounted but hidden while a diff is active. Template: `.editor-tabs` always; below, `<DiffView>` when active kind is diff, else existing `.editor-header` + `.editor-body`.

### 12. `website/src/styles.css`

New classes, all from existing theme vars (no new theme variables):
- `.activity-bar` (48px flex-none column, `--editor-surface` bg, `--editor-border` right border), `.activity-btn` (44×40, icon `--text-subtle`, hover `--editor-surface-2`/`--text`), `.activity-btn.active` (inset 2px accent left bar), `.activity-badge` (tiny `--accent-fill` pill, `--on-accent` text).
- `.sc*`: header/branch/refresh (clone `.explorer-refresh`), sections (head hover `--editor-surface-2`, chevron, count), rows (clone `.tree-row`, `margin-left: auto` badge), empty states.
- `.diff*`: `.diff` flex column; `.diff-body` flex row `min-height: 0`; `.diff-col` (`flex:1; min-width:0; overflow:auto`, `+ .diff-col` left border); `.diff-col-header` sticky, `--editor-surface-2` bg, `--line-number` text; `.diff-row` (`display:flex; font: 13px/1.5 var(--mono); white-space: pre; min-height:1.5em`); `.diff-gutter` (flex-none, right-aligned, `--line-number` on `--editor-surface-2`, `min-width:4ch`, sticky left); `.diff-text` (`flex:1; min-width:0`); `.diff-row.del .diff-text { background: color-mix(in srgb, var(--error) 12%, transparent) }`; `.diff-row.add .diff-text { background: color-mix(in srgb, var(--success) 12%, transparent) }`; `.diff-row.hunk` (muted); `.diff-noeol` note.

No syntax highlighting of diff lines in v1 (future: `markdown.ts` `highlightCode` + `editor/theme.ts` HighlightStyle path).

## Docs

Update `docs/plans/web-frontend.md`: document `sections` field on `/api/git/status`, the new `/api/git/diff` endpoint (params, payload shape, `--no-index` rc-1 note, staged semantics), and a UI subsection (activity bar, SC panel, side-by-side diff, ephemeral diff tabs).

## Implementation order

1. Backend: `diffparse.py` + `TestParseDiff` → `git.py` (`_parse_records`, `build_sections`, `git_status`, `DiffError`, `git_diff`) + `TestBuildSections` + `TestDiffEndpoint` + test_web_git.py updates → `api.py` endpoint. Run `.venv/bin/pytest tests/test_web_git.py tests/test_web_diff.py` + `ruff check .`.
2. Frontend data layer: types.ts, api.ts, useGitStatus.ts (build still green).
3. Shell: App.vue activity bar + TabEntry refactor + styles (verify tabs/persist/drag still work).
4. Panels: SourceControl.vue → diff.ts + DiffView.vue → FileEditor.vue branch → remaining styles. `npm run build` after each.
5. Docs.

## Verification

1. `ruff check .` and `.venv/bin/pytest` (all suites).
2. `cd website && npm run build` (vue-tsc strict + vite).
3. Manual `tod serve --dev` + `npm run dev` against a scratch repo with: MM file, untracked, staged add, staged delete, binary, CRLF file, rename. Check: activity-bar toggle/close/switch + count badge; SC sections/counts/collapse; click rows open diff tabs coexisting with file tabs; MM opens different diffs from the two sections; untracked/deleted/binary rendering; vertical sync both directions + independent horizontal scroll; Ctrl+S in editor refreshes SC; agent edit → re-activated diff tab shows fresh content; reload restores file tabs but not diff tabs; both themes render diff colors.

## Risks / gotchas

- `--no-index` rc 1 = success; check stderr before treating as failure.
- Never parse paths from diff header lines (`--- /dev/null` presence only).
- `parse_status` signature pinned by tests; `test_status_payload_keys` key set must change with the payload.
- Diff-tab switch must flush outgoing file tab before `setState(emptyState)` or keystrokes are lost.
- Only file tabs persist to `tod.tabs`; cwd watcher clears diff tabs too.
- Row height determinism (fixed line-height, `pre`, `min-height` empty cells) is what makes scroll sync exact.
- Strip trailing `\r` at parse time.
