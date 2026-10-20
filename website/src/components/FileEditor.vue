<script setup lang="ts">
import { computed, markRaw, onMounted, onUnmounted, ref, watch } from 'vue'
import { EditorSelection, EditorState, type Extension } from '@codemirror/state'
import { EditorView, drawSelection, keymap, lineNumbers } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { forceParsing } from '@codemirror/language'
import type { Language } from '@codemirror/language'
import { api } from '../api'
import { languageForName, languageForPath } from '../editor/languages'
import { editorTheme, highlightExt } from '../editor/theme'
import { gitBadgeClass } from '../gitStatus'
import type { GitStatusState, RevealRequest, TabEntry } from '../types'
import { basename, isExternal, tabKey } from '../utils'
import ExternalTag from './ExternalTag.vue'
import FileEditorDiffView from './FileEditorDiffView.vue'
import FileEditorDocView from './FileEditorDocView.vue'

/**
 * Editor pane with one tab per open entry — a regular file tab, a
 * read-only text tab (content the app holds rather than the disk), a
 * read-only markdown document (the same, rendered), or a read-only diff
 * tab.  A file outside the served root (an absolute path — `isExternal`)
 * opens as a file tab too, but never becomes editable: the write endpoint
 * stays root-scoped.  The tab list and active tab live in App.vue; this
 * component owns per-entry CodeMirror state.  A single EditorView serves
 * the editable kinds — switching tabs swaps its state via
 * `view.setState`, which carries each tab's doc, undo history, and
 * cursor.  While a diff or document tab is active the CM host stays
 * mounted but hidden (`display: none`) and its own view takes over the
 * pane — the EditorView's parent must never unmount.  Files load via
 * `GET /api/file`, save with Ctrl/Cmd+S via `PUT /api/file` — the same
 * path-safe endpoint the agent's WriteFile tool backs onto.  Syntax
 * highlighting comes from the CodeMirror legacy grammars
 * (`editor/languages.ts`).  Tabs refetch from disk on activation, on the
 * end frame of a write tool that names their file, and once more when a
 * turn ends (the catch-all for shell/formatter changes) — unless they have
 * unsaved edits, which always win.
 */

const props = defineProps<{
  files: TabEntry[]
  active: TabEntry | null
  git: GitStatusState
  /** A tool card's request to select a string once the file is on screen. */
  reveal?: RevealRequest | null
  /** A completed write tool call: refresh that file's open tab. */
  writtenFile?: { path: string; nonce: number } | null
  /** A turn ended: re-read every open file tab (shell-side writes too). */
  refreshAll?: number
}>()
const emit = defineEmits<{
  'activate-tab': [tab: TabEntry]
  'close-tab': [tab: TabEntry]
  'open-file': [path: string]
  'file-saved': [path: string]
  'dirty-count': [count: number]
  'refresh-git': []
}>()

interface EditorTab {
  // Where the buffer came from: a file tab is fetched from disk (and saved
  // back to it — unless its path is absolute, an external file that is
  // never saved), a text tab holds content the app handed over and only
  // ever reads it.  The record outlives the open-tab list, so the prune
  // needs to know which orphans are worth keeping.
  kind: 'file' | 'text'
  // Displayed without the editing extensions — same rule whether the
  // content came from the app (a text tab) or from outside the root.
  readOnly: boolean
  // CodeMirror state for the tab.  `markRaw`'d — CM state objects must
  // never be wrapped in Vue proxies.  null until the first load succeeds.
  state: EditorState | null
  dirty: boolean // unsaved edits — set by the update listener, cleared on load/save
  totalLines: number
  loading: boolean
  loadError: string | null
  saving: boolean
  saveError: string | null
  saved: boolean // 2s "saved" flash
  flashTimer: ReturnType<typeof setTimeout> | null
  scrollTop: number // pixel offset restored on switch-back
}

// Deep-reactive record keyed by path.  Null prototype — paths come from the
// file tree (untrusted): a file named `__proto__` or `constructor` must not
// read inherited members or poison Object.prototype.
const tabs = ref<Record<string, EditorTab>>(Object.create(null))
const editorHost = ref<HTMLElement | null>(null)
let view: EditorView | null = null

// Record key whose state the view currently displays — the flush-on-switch
// needs to know which tab to write the live view state back into (the
// watcher's `props.active` is already the NEW tab by the time it fires).  A
// file tab's key is its path; a text tab's is its id.
let currentKey: string | null = null

// Restore-vs-user-input race guard: wheel/touch timestamps mirror CM's own
// approach in the measure's anchor logic.  A restore that's still converging
// must not fight a user who starts scrolling mid-restore.
let lastUserScroll = 0
let lastRestoreWrite = 0

/**
 * Restore a tab's scroll position after a state swap.  A fresh render
 * starts from ESTIMATED block heights; the measure after each write
 * corrects them and re-anchors, nudging the scroll by the estimate error
 * (a few px for plain lines, more when lines wrap).  So instead of writing
 * the target once, rewrite it each frame until the correction stops moving
 * the scroll — the anchored block is measured by then, the re-anchor is
 * exact, and the loop exits.  The guard stops the loop if the user scrolls.
 */
function restoreScroll(key: string, target: number) {
  if (!view || currentKey !== key) return
  lastRestoreWrite = performance.now() // the restore owns the scroll from here
  const step = (left: number) => {
    const v = view
    if (!v || currentKey !== key || left <= 0) return
    if (lastUserScroll > lastRestoreWrite) return // user took over — their scroll wins
    v.scrollDOM.scrollTop = target
    requestAnimationFrame(() => {
      if (Math.abs(v.scrollDOM.scrollTop - target) < 0.5) return // settled
      step(left - 1)
    })
  }
  // Defer the first write a frame so CodeMirror's post-setState measure
  // lays the new doc out first (see the showTab comment).
  requestAnimationFrame(() => step(5))
}

// Per-path fetch tokens: a newer request for the same path (close + reopen,
// or a refresh superseding an earlier fetch) must invalidate any in-flight
// response — the old one would otherwise land in the new tab.
let fetchSeq = 0
const fetchTokens = new Map<string, number>()

function beginFetch(path: string): number {
  const seq = ++fetchSeq
  fetchTokens.set(path, seq)
  return seq
}

function isStale(path: string, seq: number): boolean {
  return fetchTokens.get(path) !== seq
}

// A tool card's reveal is one-shot: it names a string to select in the file
// the tab is opening.  It waits for the tab's fetch or refresh to land — the
// document it searches must be the fresh one — and is then consumed.
let pendingReveal: RevealRequest | null = null

// In-flight disk refreshes per path.  A reveal waits for one instead of
// selecting in the state the refresh is about to replace.
const refreshCounts = new Map<string, number>()

function beginRefresh(path: string) {
  refreshCounts.set(path, (refreshCounts.get(path) ?? 0) + 1)
}

function endRefresh(path: string) {
  const count = (refreshCounts.get(path) ?? 0) - 1
  if (count > 0) refreshCounts.set(path, count)
  else refreshCounts.delete(path)
}

/** Select the pending reveal in the file on screen, if it is ready.  The
 *  document is searched for the exact text rather than mapped to lines: the
 *  request already is the old_string or new_string the edit was about. */
function applyPendingReveal() {
  const request = pendingReveal
  if (!request || !view) return
  if (props.active?.kind !== 'file' || props.active.path !== request.path) return
  if (currentKey !== request.path) return
  const tab = tabs.value[request.path]
  if (!tab?.state || tab.loading || refreshCounts.has(request.path)) return

  const text = view.state.doc.toString()
  let needle = request.needle
  let at = -1
  // A recorded start wins over a text search: the replacement may also
  // occur earlier in the file, and only the position names the occurrence
  // the edit made.  It is checked against the text, so a file changed since
  // the edit falls through to the search instead of selecting a wrong span.
  if (
    request.line !== null &&
    request.column !== null &&
    request.line >= 1 &&
    request.line <= view.state.doc.lines
  ) {
    const line = view.state.doc.line(request.line)
    if (request.column <= line.length) {
      const from = line.from + request.column
      if (text.startsWith(needle, from)) {
        at = from
      } else if (needle.includes('\n') && text.includes('\r\n')) {
        const crlf = needle.replace(/\n/g, '\r\n')
        if (text.startsWith(crlf, from)) {
          needle = crlf
          at = from
        }
      }
    }
  }
  // The edit tool reads and writes with universal newlines, so its strings
  // are LF; the file API serves the file's bytes, which may be CRLF.
  if (at < 0) {
    at = text.indexOf(needle)
    if (at < 0 && needle.includes('\n') && text.includes('\r\n')) {
      needle = needle.replace(/\n/g, '\r\n')
      at = text.indexOf(needle)
    }
  }
  if (at >= 0) {
    const range = EditorSelection.range(at, at + needle.length)
    view.dispatch({
      selection: range,
      effects: EditorView.scrollIntoView(range, { y: 'center' }),
      userEvent: 'select',
    })
  } else {
    // Nothing to select — the saved offset is better than wherever the
    // state swap left the scroll.
    restoreScroll(request.path, tab.scrollTop)
  }
  // A file whose text has moved on still opens focused — the click was
  // about reading it, selection or not.
  view.focus()
  pendingReveal = null
}

/** The record key a tab's buffer lives under, or null for a kind that owns
 *  no CodeMirror state (diffs render themselves). */
function bufferKey(tab: TabEntry | null): string | null {
  if (!tab) return null
  if (tab.kind === 'file') return tab.path
  if (tab.kind === 'text') return tab.id
  return null
}

// The buffered tab whose CodeMirror machinery is live (null while a diff tab
// or nothing is active).  A diff tab for a path that is also open as a file
// shares `tabs`' key space — every lookup below must gate on the kind first.
const activeBufferKey = computed(() => bufferKey(props.active))
const activeTab = computed(() =>
  activeBufferKey.value ? (tabs.value[activeBufferKey.value] ?? null) : null,
)
const isDirty = (tab: EditorTab | null | undefined) => !!tab && tab.dirty

/** Number of open file tabs with unsaved edits — reported upward for the
 * explorer icon's activity badge.  Counted against props.files (not the
 * raw record) so buffers orphaned by a session/cwd switch — their records
 * outlive the open-tab list, which close-tabs alone prunes — can't
 * inflate the badge. */
const dirtyCount = computed(() => {
  let n = 0
  for (const tab of props.files) {
    if (tab.kind === 'file' && tabs.value[tab.path]?.dirty) n++
  }
  return n
})
watch(dirtyCount, (count) => emit('dirty-count', count))

/** Active diff tab (or null) — when set, FileEditorDiffView replaces the
 *  header+body. */
const activeDiff = computed<Extract<TabEntry, { kind: 'diff' }> | null>(() =>
  props.active?.kind === 'diff' ? props.active : null,
)

/** Active markdown document tab (or null) — replaced by FileEditorDocView
 *  the same way a diff tab is; documents own no CodeMirror state. */
const activeDoc = computed<Extract<TabEntry, { kind: 'markdown' }> | null>(() =>
  props.active?.kind === 'markdown' ? props.active : null,
)

/** Strip highlight: a tab is active when its key equals the active key. */
const activeKey = computed(() => (props.active ? tabKey(props.active) : null))

/** What the strip calls a tab: a pathless kind (text, markdown document)
 *  reads as the title it was opened with, a path-bearing one (file, diff)
 *  as the basename of the file. */
function tabLabel(tab: TabEntry): string {
  return tab.kind === 'text' || tab.kind === 'markdown'
    ? tab.title
    : basename(tab.path)
}

/** The strip's hover text — the full identity, which for a file is its path. */
function tabTitle(tab: TabEntry): string {
  return tab.kind === 'text' || tab.kind === 'markdown' ? tab.title : tab.path
}

/** What the header names — a pathless kind names its title, a file its path.
 *  (Diff and markdown tabs replace the header, so they never reach here.) */
const activeTitle = computed(() => {
  const tab = props.active
  if (!tab) return null
  return tab.kind === 'text' || tab.kind === 'markdown' ? tab.title : tab.path
})

const statusText = computed(() => {
  const t = activeTab.value
  if (!t) return ''
  // A text tab and an external file tab read alike: nothing here can be
  // typed into, and saying so is what keeps the absence of a save honest.
  if (t.readOnly) return `read-only · ${t.totalLines} lines`
  if (t.loadError) return t.loadError
  if (t.saveError) return t.saveError
  if (t.loading) return 'loading…'
  if (t.saving) return 'saving…'
  if (t.saved) return 'saved'
  return `${t.totalLines} lines`
})

function ensureTab(key: string, kind: 'file' | 'text'): EditorTab {
  const existing = tabs.value[key]
  if (existing) return existing
  tabs.value[key] = {
    kind,
    readOnly: kind === 'text' || isExternal(key),
    state: null, dirty: false, totalLines: 0,
    loading: false, loadError: null, saving: false, saveError: null,
    saved: false, flashTimer: null, scrollTop: 0,
  }
  // Re-read: the store wraps the literal in a reactive proxy on get, and
  // callers must mutate the proxy — writes to the raw literal would not
  // trigger re-renders.
  return tabs.value[key]
}

// The update listener closes over its tab, so typing always updates the
// right entry — the view only ever displays the active tab's state, so
// the listener facet in force is the active tab's.
function buildExtensions(
  tab: EditorTab,
  language: Language | null,
  readOnly = false,
): Extension[] {
  // A read-only buffer keeps every reader's affordance — selection, copy,
  // scrolling, highlighting, the line numbers — and drops the ones that
  // write back: undo history, the save and indent bindings, the dirty
  // tracker.  `EditorState.readOnly` blocks the dispatches themselves, so
  // the doc cannot move even if some binding did fire.
  const editing: Extension[] = readOnly
    ? [EditorState.readOnly.of(true)]
    : [
        history(),
        keymap.of([
          // Ctrl/Cmd+S saves — explicit Ctrl-s keeps "Ctrl works on Mac too"
          // parity with the old onKeydown, which accepted either modifier.
          { key: 'Mod-s', run: () => { void save(); return true } },
          { key: 'Ctrl-s', run: () => { void save(); return true } },
          // Tab indents two spaces and stays in the editor.
          { key: 'Tab', run: (v) => {
            v.dispatch(v.state.replaceSelection('  '))
            return true
          } },
          ...defaultKeymap,
          ...historyKeymap,
        ]),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) tab.dirty = true
        }),
      ]

  return [
    ...editing,
    drawSelection(),
    lineNumbers(),
    EditorView.lineWrapping,
    editorTheme,
    highlightExt,
    ...(language ? [language] : []), // no language → plain text
  ]
}

function buildState(
  tab: EditorTab,
  doc: string,
  language: Language | null,
  readOnly = false,
): EditorState {
  return EditorState.create({
    doc,
    extensions: buildExtensions(tab, language, readOnly),
  })
}

// Empty doc shown when no tab is active — built once so the "no file
// selected" view keeps the theme.  The doc never changes, so its update
// listener (closed over noopTab) never fires.
const noopTab: EditorTab = {
  kind: 'file',
  readOnly: false,
  state: null, dirty: false, totalLines: 0,
  loading: false, loadError: null, saving: false, saveError: null,
  saved: false, flashTimer: null, scrollTop: 0,
}
const emptyState = markRaw(EditorState.create({ doc: '', extensions: buildExtensions(noopTab, null) }))

// Flush-before-switch: after every dispatch `view.state` is a NEW object,
// so a stored tab.state reference goes stale the moment the user types.
// Capture doc, undo history, selection, and scroll offset before the state
// is replaced — that keeps tab.state ≡ the view state last shown for it.
// (dirty needs no capture: the update listener has kept it current.)
function flushCurrentFile() {
  if (!view || !currentKey) return
  const prevTab = tabs.value[currentKey]
  if (prevTab?.state) {
    prevTab.state = markRaw(view.state)
    prevTab.scrollTop = view.scrollDOM.scrollTop
  }
}

function switchToTab(path: string) {
  if (!view) return
  if (currentKey && currentKey !== path) flushCurrentFile()
  const tab = tabs.value[path]
  if (tab?.state) {
    showTab(tab, path, true)
    if (!tab.dirty) void refreshTab(path) // disk may have moved under us
    else applyPendingReveal() // no refresh will run to apply it
  } else {
    // Target tab still loading — never leave an interactive view showing
    // another file's doc.  The overlay covers clicks; blur covers stray
    // keyboard focus; typing into an untracked doc is impossible.
    const focusOnLoad = view.hasFocus // re-focus on arrival iff we had focus
    view.contentDOM.blur()
    currentKey = null
    void loadTab(path, focusOnLoad)
  }
}

/** Display the read-only tab `entry` names.  There is nothing to fetch: the
 *  content arrived with the tab, and the buffer is rebuilt from it. */
function showTextTab(entry: Extract<TabEntry, { kind: 'text' }>) {
  if (!view) return
  flushCurrentFile()
  const tab = ensureTab(entry.id, 'text')
  const language = entry.language ? languageForName(entry.language) : null
  tab.state = markRaw(buildState(tab, entry.content, language, tab.readOnly))
  tab.totalLines = entry.content ? entry.content.split('\n').length : 0
  tab.loading = false
  tab.loadError = null
  tab.dirty = false
  showTab(tab, entry.id, true)
  // Parse synchronously so the highlight is complete from the first paint,
  // as a file load does.
  if (language) forceParsing(view, tab.state.doc.length, 500)
}

/** Display `tab` in the single view.  Caller guarantees `tab.state` is set. */
function showTab(tab: EditorTab, key: string, focus: boolean) {
  if (!view || !tab.state) return
  view.setState(tab.state)
  currentKey = key
  if (focus) view.focus()
  // A pending reveal owns the scroll — the click asked for a new position,
  // not the remembered one.  Otherwise setState keeps the DOM's current
  // scroll, and the new content height only lands during the post-setState
  // measure — writing scrollTop synchronously would clamp it against the
  // previous tab's stale scrollHeight.  restoreScroll defers past that
  // measure and then converges on the exact position.
  if (pendingReveal?.path !== key) restoreScroll(key, tab.scrollTop)
}

async function loadTab(path: string, focusOnLoad = false) {
  const tab = ensureTab(path, 'file')
  if (tab.state || tab.loading) return // already loaded or in flight
  const seq = beginFetch(path)
  tab.loading = true
  tab.loadError = null
  try {
    const res = await api.readFile(path)
    if (isStale(path, seq) || !tabs.value[path]) return // superseded or closed
    const language = languageForPath(path)
    tab.totalLines = res.total_lines
    tab.state = markRaw(buildState(tab, res.content, language, tab.readOnly))
    tab.dirty = false
    tab.loading = false
    if (props.active?.kind === 'file' && props.active.path === path && view) {
      showTab(tab, path, focusOnLoad)
      if (language) {
        // Parse the whole document synchronously so highlighting is complete
        // from the first paint.  The background parse worker only runs in
        // short idle-callback slices with ≥100ms gaps, and browsers withhold
        // idle work while the user is scrolling or typing — so the bottom of
        // a large file would otherwise stay plain text until the browser goes
        // idle.  Cost is ~13ms for a 64KB file; the 500ms cap only binds on
        // pathological files, and the worker continues in the background.
        forceParsing(view, tab.state.doc.length, 500)
      }
    }
    applyPendingReveal()
  } catch (err) {
    if (isStale(path, seq) || !tabs.value[path]) return
    tab.loadError = (err as Error).message
    tab.loading = false
  }
}

// Silent disk re-read for an already-loaded tab — the agent may have
// written the file since we fetched it.  Aborts if the user touched the doc
// meanwhile (dirty, or the content differs from the pre-fetch snapshot):
// their edits — possibly already saved — must never be replaced by a
// response that predates them.
async function refreshTab(path: string) {
  const tab = ensureTab(path, 'file')
  if (tab.loading || tab.dirty) return
  beginRefresh(path)
  const seq = beginFetch(path)
  const startText = tab.state ? tab.state.doc.toString() : null
  try {
    const res = await api.readFile(path)
    if (isStale(path, seq) || tabs.value[path] !== tab) return
    if (tab.dirty) return // user typed while we fetched — keep their edits
    const current =
      currentKey === path && view
        ? view.state.doc.toString()
        : (tab.state?.doc.toString() ?? null)
    if (startText !== null && current !== startText) return
    const language = languageForPath(path)
    tab.totalLines = res.total_lines
    tab.loadError = null
    if (current === res.content) {
      // Disk matches what we show — the state is already correct.  Rebuilding
      // it would be a no-op swap that re-enters the estimate-correction dance
      // for nothing (a fresh render starts from estimated heights and the
      // anchor logic can nudge the scroll).
      return
    }
    tab.state = markRaw(buildState(tab, res.content, language, tab.readOnly))
    tab.dirty = false
    if (currentKey === path && view) {
      // Live swap while the user looks at the tab — keep their scroll,
      // unless a reveal owns it (its selection scrolls itself into view).
      const savedTop = view.scrollDOM.scrollTop
      view.setState(tab.state)
      if (pendingReveal?.path !== path) restoreScroll(path, savedTop)
      if (language) forceParsing(view, tab.state.doc.length, 500)
    }
  } catch (err) {
    if (isStale(path, seq) || tabs.value[path] !== tab) return
    if (tab.dirty) return
    tab.loadError = (err as Error).message
  } finally {
    endRefresh(path)
    applyPendingReveal()
  }
}

async function save() {
  // Only file tabs save — the keymap can't fire while a diff tab is
  // active (the view is blurred and hidden), but the guard must not
  // depend on that — and an external file never does: the write endpoint
  // stays root-scoped, and the read-only state has no save binding anyway.
  if (props.active?.kind !== 'file' || isExternal(props.active.path)) return
  const path = props.active.path
  const tab = tabs.value[path]
  if (!tab?.state || tab.loading || tab.saving || !view) return
  tab.saveError = null
  tab.saving = true
  // The live view IS this tab's state — the keymap only fires while it's
  // displayed, and switchToTab keeps view.state ≡ the active tab's state.
  const text = view.state.doc.toString()
  try {
    await api.writeFile(path, text)
    emit('file-saved', path) // the disk changed — git status needs a refresh
    if (tabs.value[path] !== tab) return // tab closed or reopened while saving
    // Typing during the await already set dirty=true — re-derive from the
    // live doc (or the flushed state, if the user switched away mid-save)
    // instead of trusting the flag.
    const now =
      currentKey === path && view
        ? view.state.doc.toString()
        : (tab.state?.doc.toString() ?? text)
    tab.dirty = now !== text
    tab.saved = true
    if (tab.flashTimer) clearTimeout(tab.flashTimer)
    tab.flashTimer = setTimeout(() => {
      tab.saved = false
    }, 2000)
  } catch (err) {
    if (tabs.value[path] === tab) tab.saveError = (err as Error).message
  } finally {
    tab.saving = false
  }
}

function requestClose(tabEntry: TabEntry) {
  // Diff tabs hold no CodeMirror state — close immediately.  File tabs with
  // unsaved edits ask first, as before; a text tab is read-only, so its
  // buffer just goes.
  const key = bufferKey(tabEntry)
  if (key) {
    const tab = tabs.value[key]
    if (tab && tab.kind === 'file' && isDirty(tab)) {
      if (!window.confirm(`Close ${key}? Unsaved changes will be lost.`)) return
    }
    removeTab(key)
  }
  emit('close-tab', tabEntry)
}

function removeTab(key: string) {
  const tab = tabs.value[key]
  if (tab?.flashTimer) clearTimeout(tab.flashTimer)
  fetchTokens.delete(key) // any in-flight fetch for it is now stale
  delete tabs.value[key]
}

// A text tab's buffer holds the content itself, so an orphan left by a
// session or repo switch is dead weight that the file buffers'
// keep-on-purpose rule (see `dirtyCount`) does not cover.
watch(
  () => props.files,
  (files) => {
    const live = new Set(
      files.map((tab) => bufferKey(tab)).filter((key): key is string => !!key),
    )
    for (const key of Object.keys(tabs.value)) {
      if (tabs.value[key].kind === 'text' && !live.has(key)) removeTab(key)
    }
  },
)

// Refresh/close guard: reloading silently discards unsaved edits (tabs
// persist, content refetches from disk), so ask first when any tab is
// dirty — same spirit as requestClose's confirm.  The dialog text is the
// browser's; preventDefault alone triggers it (returnValue is deprecated).
function onBeforeUnload(event: BeforeUnloadEvent) {
  if (!Object.values(tabs.value).some((t) => t.dirty)) return
  event.preventDefault()
}

onMounted(() => {
  if (!editorHost.value) return
  view = new EditorView({ parent: editorHost.value })
  // Wheel/touch timestamps feed the restoreScroll guard — a converging
  // restore must not fight a user who starts scrolling mid-restore.
  view.dom.addEventListener('wheel', () => { lastUserScroll = performance.now() }, { passive: true })
  view.dom.addEventListener('touchstart', () => { lastUserScroll = performance.now() }, { passive: true })
  window.addEventListener('beforeunload', onBeforeUnload)
  // The initial activation (restored from localStorage) needs no watcher
  // fire — the immediate watcher would run before the view exists.
  // Diff tabs never persist across reloads, so the restored active tab is
  // always a file tab; the else branch also covers the empty case.
  if (props.active?.kind === 'file') switchToTab(props.active.path)
  else view.setState(emptyState)
})

onUnmounted(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
  view?.destroy()
  view = null
  for (const tab of Object.values(tabs.value)) {
    if (tab.flashTimer) clearTimeout(tab.flashTimer)
  }
})

// A write tool's end frame names the file it changed: refresh that tab now.
// A tab that has never loaded is skipped — its first load fetches the fresh
// bytes anyway — and a closed tab has nothing to refresh.
watch(() => props.writtenFile, (change) => {
  if (!change) return
  if (!props.files.some((tab) => tab.kind === 'file' && tab.path === change.path)) {
    return
  }
  if (!tabs.value[change.path]?.state) return
  void refreshTab(change.path)
})

// A turn ended — the catch-all for writers that never name a file (a shell
// command, a formatter, a git move).  Every open file tab is re-read;
// refreshTab skips dirty/loading tabs and leaves an unchanged buffer alone.
watch(() => props.refreshAll, (token, previous) => {
  if (token === previous) return
  for (const tab of props.files) {
    if (tab.kind === 'file' && tabs.value[tab.path]?.state) {
      void refreshTab(tab.path)
    }
  }
})

// A card's reveal arrives with (or just before) the active-tab change that
// opens its file.  This watcher is created first so it can mark the request
// before switchToTab runs: showTab then knows not to restore the old scroll.
watch(
  () => props.reveal,
  (request) => {
    if (!request) return
    pendingReveal = { ...request }
    // The editor may already be showing this file (a repeat click): no
    // active-tab change will fire, so fetch the fresh doc here.  Otherwise
    // switchToTab/loadTab drives the fetch and applies the reveal when it
    // settles.
    if (
      currentKey === request.path &&
      props.active?.kind === 'file' &&
      props.active.path === request.path
    ) {
      const tab = tabs.value[request.path]
      if (tab?.state && !tab.dirty && !refreshCounts.has(request.path)) {
        void refreshTab(request.path)
      } else {
        applyPendingReveal()
      }
    }
  },
  { immediate: true },
)

watch(() => props.active, (tab) => {
  // A reveal the old tab never consumed does not follow the reader to
  // another file.  (The reveal and its opening tab change in one flush; the
  // reveal watcher above runs first, so this only drops a stale request.)
  if (
    pendingReveal &&
    (tab?.kind !== 'file' || tab.path !== pendingReveal.path)
  ) {
    pendingReveal = null
  }
  if (!tab) {
    if (view) {
      // Flush the closing tab's live state before dropping the doc — the
      // flush-on-switch invariant must not depend on call order (a future
      // "close all" flow would otherwise lose unflushed keystrokes).
      flushCurrentFile()
      view.setState(emptyState)
      view.contentDOM.blur()
    }
    currentKey = null
    return
  }
  if (tab.kind === 'diff' || tab.kind === 'markdown') {
    // These tabs own no CodeMirror state.  Flush the outgoing file tab,
    // park the view on the empty doc, and blur it — the CM host stays
    // mounted (hidden behind the diff/document view) so the EditorView
    // survives.
    if (view) {
      flushCurrentFile()
      view.setState(emptyState)
      view.contentDOM.blur()
    }
    currentKey = null
    return
  }
  if (tab.kind === 'text') {
    showTextTab(tab)
    return
  }
  switchToTab(tab.path)
})

// A text tab's content can move after it opens — a command still streaming
// when the row was clicked, a result that lands while the tab is watched.
// The doc swaps in place; the reader keeps their scroll.
watch(
  () => (props.active?.kind === 'text' ? props.active.content : null),
  (content) => {
    const tab = activeTab.value
    if (!view || content === null || !tab?.state) return
    if (content === view.state.doc.toString()) return
    const savedTop = view.scrollDOM.scrollTop
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: content },
    })
    tab.totalLines = content ? content.split('\n').length : 0
    view.scrollDOM.scrollTop = savedTop
  },
)
</script>

<template>
  <div class="editor">
    <div v-if="files.length" class="editor-tabs">
      <div
        v-for="tab in files"
        :key="tabKey(tab)"
        class="editor-tab"
        :class="{ active: activeKey === tabKey(tab), dirty: tab.kind === 'file' && isDirty(tabs[tab.path]) }"
        :title="tabTitle(tab)"
        @click="emit('activate-tab', tab)"
      >
        <span class="editor-tab-name">{{ tabLabel(tab) }}</span>
        <!-- A file outside the root reads as external wherever it is named:
             the basename alone can be ambiguous, and nothing behind the tab
             can be saved. -->
        <ExternalTag v-if="tab.kind === 'file' && isExternal(tab.path)" />
        <span
          v-if="tab.kind === 'file' && git.files[tab.path]"
          class="git-badge"
          :class="gitBadgeClass(git.files[tab.path])"
          :title="`git status: ${git.files[tab.path]}`"
        >{{ git.files[tab.path] }}</span>
        <span
          v-else-if="tab.kind === 'diff'"
          class="diff-tag"
          :class="tab.staged ? 'staged' : 'unstaged'"
        >{{ tab.staged ? 'staged' : 'unstaged' }}</span>
        <span
          v-if="tab.kind === 'file' && isDirty(tabs[tab.path])"
          class="editor-tab-dirty"
          title="unsaved changes"
        >●</span>
        <button
          type="button"
          class="editor-tab-close"
          :aria-label="`close ${tabTitle(tab)}`"
          @click.stop="requestClose(tab)"
        >
          ×
        </button>
      </div>
    </div>

    <!-- A diff tab takes over the pane.  Keyed by tab key: switching
         between two diff tabs remounts it, so the diff refetches fresh
         on every activation. -->
    <FileEditorDiffView
      v-if="activeDiff"
      :key="tabKey(activeDiff)"
      :tab="activeDiff"
      :git="git"
      @open-file="emit('open-file', $event)"
      @refresh-git="emit('refresh-git')"
    />

    <!-- A markdown document takes over the pane the same way.  Keyed by
         tab key too, so switching documents resets the scroll to the top
         of the one arriving. -->
    <FileEditorDocView
      v-else-if="activeDoc"
      :key="tabKey(activeDoc)"
      :tab="activeDoc"
      @open-file="emit('open-file', $event)"
    />

    <div v-else class="editor-header">
      <span class="editor-path" :title="activeTitle ?? undefined">
        {{ activeTitle ?? 'no file selected' }}
      </span>
      <!-- The header names the whole path already; the tag says what the
           path's leading slash means. -->
      <ExternalTag v-if="active?.kind === 'file' && isExternal(active.path)" />
      <span
        v-if="activeTab && isDirty(activeTab)"
        class="editor-dirty"
        title="unsaved changes"
      >●</span>
      <span class="editor-status" :class="{ 'status-error': !!(activeTab?.loadError || activeTab?.saveError) }">
        {{ statusText }}
      </span>
    </div>

    <!-- Host stays mounted so the single view never dies; messages paint
         over it.  The overlay blocks pointer events and the view is blurred
         while its doc is untracked, so no edits can land in limbo.  While a
         diff or document tab is active the whole body hides
         (`display: none`) rather than unmounting — the EditorView's parent
         must survive. -->
    <div class="editor-body" :class="{ hidden: !!activeDiff || !!activeDoc }">
      <div ref="editorHost" class="editor-cm" />
      <div v-if="!active" class="editor-message">
        Select a file in the explorer to edit it.
      </div>
      <div v-else-if="activeTab?.loading" class="editor-message">loading…</div>
      <div v-else-if="activeTab?.loadError" class="editor-message error">{{ activeTab.loadError }}</div>
    </div>
  </div>
</template>
