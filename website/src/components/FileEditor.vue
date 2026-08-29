<script setup lang="ts">
import { computed, markRaw, onMounted, onUnmounted, ref, watch } from 'vue'
import { EditorState, type Extension } from '@codemirror/state'
import { EditorView, drawSelection, keymap, lineNumbers } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { forceParsing } from '@codemirror/language'
import type { Language } from '@codemirror/language'
import { api } from '../api'
import { languageForPath } from '../editor/languages'
import { editorTheme, highlightExt } from '../editor/theme'
import { gitBadgeClass } from '../gitStatus'
import type { GitStatusState, TabEntry } from '../types'
import { basename, tabKey } from '../utils'
import DiffView from './DiffView.vue'

/**
 * Editor pane with one tab per open entry — a regular file tab or a
 * read-only diff tab.  The tab list and active tab live in App.vue; this
 * component owns per-file CodeMirror state.  A single EditorView serves
 * all file tabs — switching tabs swaps its state via `view.setState`,
 * which carries each tab's doc, undo history, and cursor.  While a diff
 * tab is active the CM host stays mounted but hidden (`display: none`)
 * and DiffView takes over the pane — the EditorView's parent must never
 * unmount.  Files load via `GET /api/file`, save with Ctrl/Cmd+S via
 * `PUT /api/file` — the same path-safe endpoint the agent's WriteFile
 * tool backs onto.  Syntax highlighting comes from the CodeMirror legacy
 * grammars (`editor/languages.ts`).  A tab refetches from disk on
 * activation (unless it has unsaved edits), so the agent's writes show up.
 */

const props = defineProps<{ files: TabEntry[]; active: TabEntry | null; git: GitStatusState }>()
const emit = defineEmits<{
  'activate-tab': [tab: TabEntry]
  'close-tab': [tab: TabEntry]
  'open-file': [path: string]
  'file-saved': [path: string]
  'refresh-git': []
}>()

interface EditorTab {
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

// Path whose state the view currently displays — the flush-on-switch needs
// to know which tab to write the live view state back into (the watcher's
// `props.active` is already the NEW path by the time it fires).
let currentPath: string | null = null

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
function restoreScroll(path: string, target: number) {
  if (!view || currentPath !== path) return
  lastRestoreWrite = performance.now() // the restore owns the scroll from here
  const step = (left: number) => {
    const v = view
    if (!v || currentPath !== path || left <= 0) return
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

// The file tab whose CodeMirror machinery is live (null while a diff tab
// or nothing is active).  `tabs` is keyed by path, so a diff tab for a
// path that is also open as a file shares the key — every per-file lookup
// below must gate on `kind === 'file'` first.
const activeTab = computed(() =>
  props.active?.kind === 'file' ? (tabs.value[props.active.path] ?? null) : null,
)
const isDirty = (tab: EditorTab | null | undefined) => !!tab && tab.dirty

/** Active file path — the header, explorer highlight, and save target. */
const activeFilePath = computed(() =>
  props.active?.kind === 'file' ? props.active.path : null,
)

/** Active diff tab (or null) — when set, DiffView replaces the header+body. */
const activeDiff = computed<Extract<TabEntry, { kind: 'diff' }> | null>(() =>
  props.active?.kind === 'diff' ? props.active : null,
)

/** Strip highlight: a tab is active when its key equals the active key. */
const activeKey = computed(() => (props.active ? tabKey(props.active) : null))

const statusText = computed(() => {
  const t = activeTab.value
  if (!t) return ''
  if (t.loadError) return t.loadError
  if (t.saveError) return t.saveError
  if (t.loading) return 'loading…'
  if (t.saving) return 'saving…'
  if (t.saved) return 'saved'
  return `${t.totalLines} lines`
})

function ensureTab(path: string): EditorTab {
  const existing = tabs.value[path]
  if (existing) return existing
  tabs.value[path] = {
    state: null, dirty: false, totalLines: 0,
    loading: false, loadError: null, saving: false, saveError: null,
    saved: false, flashTimer: null, scrollTop: 0,
  }
  // Re-read: the store wraps the literal in a reactive proxy on get, and
  // callers must mutate the proxy — writes to the raw literal would not
  // trigger re-renders.
  return tabs.value[path]
}

// The update listener closes over its tab, so typing always updates the
// right entry — the view only ever displays the active tab's state, so
// the listener facet in force is the active tab's.
function buildExtensions(tab: EditorTab, language: Language | null): Extension[] {
  return [
    history(),
    drawSelection(),
    lineNumbers(),
    EditorView.lineWrapping,
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
    editorTheme,
    highlightExt,
    ...(language ? [language] : []), // no language → plain text
  ]
}

function buildState(tab: EditorTab, doc: string, language: Language | null): EditorState {
  return EditorState.create({ doc, extensions: buildExtensions(tab, language) })
}

// Empty doc shown when no tab is active — built once so the "no file
// selected" view keeps the theme.  The doc never changes, so its update
// listener (closed over noopTab) never fires.
const noopTab: EditorTab = {
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
  if (!view || !currentPath) return
  const prevTab = tabs.value[currentPath]
  if (prevTab?.state) {
    prevTab.state = markRaw(view.state)
    prevTab.scrollTop = view.scrollDOM.scrollTop
  }
}

function switchToTab(path: string) {
  if (!view) return
  if (currentPath && currentPath !== path) flushCurrentFile()
  const tab = tabs.value[path]
  if (tab?.state) {
    showTab(tab, path, true)
    if (!tab.dirty) void refreshTab(path) // disk may have moved under us
  } else {
    // Target tab still loading — never leave an interactive view showing
    // another file's doc.  The overlay covers clicks; blur covers stray
    // keyboard focus; typing into an untracked doc is impossible.
    const focusOnLoad = view.hasFocus // re-focus on arrival iff we had focus
    view.contentDOM.blur()
    currentPath = null
    void loadTab(path, focusOnLoad)
  }
}

/** Display `tab` in the single view.  Caller guarantees `tab.state` is set. */
function showTab(tab: EditorTab, path: string, focus: boolean) {
  if (!view || !tab.state) return
  view.setState(tab.state)
  currentPath = path
  if (focus) view.focus()
  // setState keeps the DOM's current scroll, and the new content height only
  // lands during the post-setState measure — writing scrollTop synchronously
  // would clamp it against the previous tab's stale scrollHeight.  restoreScroll
  // defers past that measure and then converges on the exact position.
  restoreScroll(path, tab.scrollTop)
}

async function loadTab(path: string, focusOnLoad = false) {
  const tab = ensureTab(path)
  if (tab.state || tab.loading) return // already loaded or in flight
  const seq = beginFetch(path)
  tab.loading = true
  tab.loadError = null
  try {
    const res = await api.readFile(path)
    if (isStale(path, seq) || !tabs.value[path]) return // superseded or closed
    const language = languageForPath(path)
    tab.totalLines = res.total_lines
    tab.state = markRaw(buildState(tab, res.content, language))
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
  const tab = ensureTab(path)
  if (tab.loading || tab.dirty) return
  const seq = beginFetch(path)
  const startText = tab.state ? tab.state.doc.toString() : null
  try {
    const res = await api.readFile(path)
    if (isStale(path, seq) || tabs.value[path] !== tab) return
    if (tab.dirty) return // user typed while we fetched — keep their edits
    const current =
      currentPath === path && view
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
    tab.state = markRaw(buildState(tab, res.content, language))
    tab.dirty = false
    if (currentPath === path && view) {
      // Live swap while the user looks at the tab — keep their scroll.
      const savedTop = view.scrollDOM.scrollTop
      view.setState(tab.state)
      restoreScroll(path, savedTop)
      if (language) forceParsing(view, tab.state.doc.length, 500)
    }
  } catch (err) {
    if (isStale(path, seq) || tabs.value[path] !== tab) return
    if (tab.dirty) return
    tab.loadError = (err as Error).message
  }
}

async function save() {
  // Only file tabs save — the keymap can't fire while a diff tab is
  // active (the view is blurred and hidden), but the guard must not
  // depend on that.
  if (props.active?.kind !== 'file') return
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
      currentPath === path && view
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
  // Diff tabs hold no CodeMirror state — close immediately.  File tabs
  // with unsaved edits ask first, as before.
  if (tabEntry.kind === 'file') {
    const tab = tabs.value[tabEntry.path]
    if (tab && isDirty(tab)) {
      if (!window.confirm(`Close ${tabEntry.path}? Unsaved changes will be lost.`)) return
    }
    removeTab(tabEntry.path)
  }
  emit('close-tab', tabEntry)
}

function removeTab(path: string) {
  const tab = tabs.value[path]
  if (tab?.flashTimer) clearTimeout(tab.flashTimer)
  fetchTokens.delete(path) // any in-flight fetch for it is now stale
  delete tabs.value[path]
}

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

watch(() => props.active, (tab) => {
  if (!tab) {
    if (view) {
      // Flush the closing tab's live state before dropping the doc — the
      // flush-on-switch invariant must not depend on call order (a future
      // "close all" flow would otherwise lose unflushed keystrokes).
      flushCurrentFile()
      view.setState(emptyState)
      view.contentDOM.blur()
    }
    currentPath = null
    return
  }
  if (tab.kind === 'diff') {
    // Diff tabs own no CodeMirror state.  Flush the outgoing file tab,
    // park the view on the empty doc, and blur it — the CM host stays
    // mounted (hidden behind DiffView) so the EditorView survives.
    if (view) {
      flushCurrentFile()
      view.setState(emptyState)
      view.contentDOM.blur()
    }
    currentPath = null
    return
  }
  switchToTab(tab.path)
})
</script>

<template>
  <div class="editor">
    <div v-if="files.length" class="editor-tabs">
      <div
        v-for="tab in files"
        :key="tabKey(tab)"
        class="editor-tab"
        :class="{ active: activeKey === tabKey(tab), dirty: tab.kind === 'file' && isDirty(tabs[tab.path]) }"
        :title="tab.path"
        @click="emit('activate-tab', tab)"
      >
        <span class="editor-tab-name">{{ basename(tab.path) }}</span>
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
          :aria-label="`close ${tab.path}`"
          @click.stop="requestClose(tab)"
        >
          ×
        </button>
      </div>
    </div>

    <!-- A diff tab takes over the pane.  Keyed by tab key: switching
         between two diff tabs remounts it, so the diff refetches fresh
         on every activation. -->
    <DiffView
      v-if="activeDiff"
      :key="tabKey(activeDiff)"
      :tab="activeDiff"
      :git="git"
      @open-file="emit('open-file', $event)"
      @refresh-git="emit('refresh-git')"
    />

    <div v-else class="editor-header">
      <span class="editor-path" :title="activeFilePath ?? undefined">
        {{ activeFilePath ?? 'no file selected' }}
      </span>
      <span v-if="activeTab && isDirty(activeTab)" class="editor-dirty" title="unsaved changes">●</span>
      <span class="editor-status" :class="{ 'status-error': !!(activeTab?.loadError || activeTab?.saveError) }">
        {{ statusText }}
      </span>
    </div>

    <!-- Host stays mounted so the single view never dies; messages paint
         over it.  The overlay blocks pointer events and the view is blurred
         while its doc is untracked, so no edits can land in limbo.  While a
         diff tab is active the whole body hides (`display: none`) rather
         than unmounting — the EditorView's parent must survive. -->
    <div class="editor-body" :class="{ hidden: !!activeDiff }">
      <div ref="editorHost" class="editor-cm" />
      <div v-if="!active" class="editor-message">
        Select a file in the explorer to edit it.
      </div>
      <div v-else-if="activeTab?.loading" class="editor-message">loading…</div>
      <div v-else-if="activeTab?.loadError" class="editor-message error">{{ activeTab.loadError }}</div>
    </div>
  </div>
</template>
