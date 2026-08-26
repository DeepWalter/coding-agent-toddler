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
import { basename } from '../utils'

/**
 * Editor pane with one tab per open file.  The tab list and active path
 * live in App.vue; this component owns per-tab CodeMirror state.  A single
 * EditorView serves all tabs — switching tabs swaps its state via
 * `view.setState`, which carries each tab's doc, undo history, and cursor.
 * Files load via `GET /api/file`, save with Ctrl/Cmd+S via `PUT /api/file` —
 * the same path-safe endpoint the agent's WriteFile
 * tool backs onto.  Syntax highlighting comes from the CodeMirror
 * legacy grammars (`editor/languages.ts`).  A tab refetches from disk on
 * activation (unless it has unsaved edits), so the agent's writes show up.
 */

const props = defineProps<{ files: string[]; active: string | null }>()
const emit = defineEmits<{
  'activate-file': [path: string]
  'close-file': [path: string]
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

const activeTab = computed(() => (props.active ? tabs.value[props.active] ?? null : null))
const isDirty = (tab: EditorTab | null | undefined) => !!tab && tab.dirty

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
function switchToTab(path: string) {
  if (!view) return
  if (currentPath && currentPath !== path) {
    const prevTab = tabs.value[currentPath]
    if (prevTab?.state) {
      prevTab.state = markRaw(view.state)
      prevTab.scrollTop = view.scrollDOM.scrollTop
    }
  }
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
    if (props.active === path && view) {
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
  const path = props.active
  const tab = path ? tabs.value[path] : null
  if (!path || !tab?.state || tab.loading || tab.saving || !view) return
  tab.saveError = null
  tab.saving = true
  // The live view IS this tab's state — the keymap only fires while it's
  // displayed, and switchToTab keeps view.state ≡ the active tab's state.
  const text = view.state.doc.toString()
  try {
    await api.writeFile(path, text)
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

function requestClose(path: string) {
  const tab = tabs.value[path]
  if (tab && isDirty(tab)) {
    if (!window.confirm(`Close ${path}? Unsaved changes will be lost.`)) return
  }
  removeTab(path)
  emit('close-file', path)
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
  if (props.active) switchToTab(props.active)
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

watch(() => props.active, (path) => {
  if (!path) {
    if (view) {
      // Flush the closing tab's live state before dropping the doc — the
      // flush-on-switch invariant must not depend on call order (a future
      // "close all" flow would otherwise lose unflushed keystrokes).
      if (currentPath) {
        const prev = tabs.value[currentPath]
        if (prev?.state) {
          prev.state = markRaw(view.state)
          prev.scrollTop = view.scrollDOM.scrollTop
        }
      }
      view.setState(emptyState)
      view.contentDOM.blur()
    }
    currentPath = null
    return
  }
  switchToTab(path)
})
</script>

<template>
  <div class="editor">
    <div v-if="files.length" class="editor-tabs">
      <div
        v-for="path in files"
        :key="path"
        class="editor-tab"
        :class="{ active: path === active, dirty: isDirty(tabs[path]) }"
        :title="path"
        @click="emit('activate-file', path)"
      >
        <span class="editor-tab-name">{{ basename(path) }}</span>
        <span v-if="isDirty(tabs[path])" class="editor-tab-dirty" title="unsaved changes">●</span>
        <button
          type="button"
          class="editor-tab-close"
          :aria-label="`close ${path}`"
          @click.stop="requestClose(path)"
        >
          ×
        </button>
      </div>
    </div>

    <div class="editor-header">
      <span class="editor-path" :title="active ?? undefined">
        {{ active ?? 'no file selected' }}
      </span>
      <span v-if="activeTab && isDirty(activeTab)" class="editor-dirty" title="unsaved changes">●</span>
      <span class="editor-status" :class="{ 'status-error': !!(activeTab?.loadError || activeTab?.saveError) }">
        {{ statusText }}
      </span>
    </div>

    <!-- Host stays mounted so the single view never dies; messages paint
         over it.  The overlay blocks pointer events and the view is blurred
         while its doc is untracked, so no edits can land in limbo. -->
    <div class="editor-body">
      <div ref="editorHost" class="editor-cm" />
      <div v-if="!active" class="editor-message">
        Select a file in the explorer to edit it.
      </div>
      <div v-else-if="activeTab?.loading" class="editor-message">loading…</div>
      <div v-else-if="activeTab?.loadError" class="editor-message error">{{ activeTab.loadError }}</div>
    </div>
  </div>
</template>
