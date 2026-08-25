<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { EditorState } from '@codemirror/state'
import { EditorView, drawSelection, keymap } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { forceParsing } from '@codemirror/language'
import type { Language } from '@codemirror/language'
import { api } from '../api'
import { languageForPath } from '../editor/languages'
import { editorTheme, highlightExt } from '../editor/theme'

/**
 * Bottom-of-left-pane CodeMirror editor.  Loads the file selected in the
 * explorer via `GET /api/file`, tracks unsaved changes against the last
 * saved snapshot, and persists with Ctrl/Cmd+S (or the Save button) via
 * `PUT /api/file` — the same path-safe endpoint the agent's WriteFile
 * tool backs onto.  Syntax highlighting comes from the highlight.js
 * legacy grammars (`editor/languages.ts`).
 */

const props = defineProps<{ path: string | null }>()

const savedContent = ref('')
const totalLines = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const saving = ref(false)
const saveError = ref<string | null>(null)
const saved = ref(false)

// Mirror of the live editor doc, kept in sync by the update listener —
// the dirty check compares against it (no v-model on a CodeMirror view).
const editorText = ref('')
const editorHost = ref<HTMLElement | null>(null)
let view: EditorView | null = null

const dirty = computed(() => editorText.value !== savedContent.value)

const statusText = computed(() => {
  if (loadError.value) return loadError.value
  if (saveError.value) return saveError.value
  if (loading.value) return 'loading…'
  if (saving.value) return 'saving…'
  if (saved.value) return 'saved'
  return props.path ? `${totalLines.value} lines` : ''
})

// Guards stale loads when switching files faster than the fetch round-trip.
let loadSeq = 0
let savedFlash: ReturnType<typeof setTimeout> | null = null

function destroyEditor() {
  view?.destroy()
  view = null
}

function buildState(doc: string, language: Language | null): EditorState {
  return EditorState.create({
    doc,
    extensions: [
      history(),
      drawSelection(),
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
        if (update.docChanged) editorText.value = update.state.doc.toString()
      }),
      editorTheme,
      highlightExt,
      ...(language ? [language] : []), // no language → plain text
    ],
  })
}

async function mountEditor(seq: number, doc: string, language: Language | null) {
  await nextTick() // host div exists only after loading=false renders
  if (seq !== loadSeq) return // superseded while waiting for the DOM
  if (!editorHost.value) return
  view = new EditorView({
    state: buildState(doc, language),
    parent: editorHost.value,
  })
  editorText.value = doc
  view.focus() // cursor at doc start, like the old textarea
  if (language) {
    // Parse the whole document synchronously so highlighting is complete
    // from the first paint.  The background parse worker only runs in
    // short idle-callback slices with ≥100ms gaps, and browsers withhold
    // idle work while the user is scrolling or typing — so the bottom of
    // a large file would otherwise stay plain text until the browser goes
    // idle.  Cost is ~13ms for a 64KB file; the 500ms cap only binds on
    // pathological files, and the worker continues in the background.
    forceParsing(view, view.state.doc.length, 500)
  }
}

watch(() => props.path, async (path) => {
  const seq = ++loadSeq
  loadError.value = null
  saveError.value = null
  saved.value = false
  savedContent.value = ''
  editorText.value = ''
  totalLines.value = 0
  destroyEditor() // host div is about to unmount with the v-else chain
  if (!path) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    const res = await api.readFile(path)
    if (seq !== loadSeq) return
    savedContent.value = res.content
    totalLines.value = res.total_lines
    loading.value = false
    await mountEditor(seq, res.content, languageForPath(path))
  } catch (err) {
    if (seq !== loadSeq) return
    loadError.value = (err as Error).message
    loading.value = false
  }
})

async function save() {
  if (!props.path || loading.value || saving.value || !view) return
  saveError.value = null
  saving.value = true
  const text = view.state.doc.toString()
  editorText.value = text // snapshot now — typing during the await re-dirties
  try {
    await api.writeFile(props.path, text)
    savedContent.value = text
    saved.value = true
    if (savedFlash) clearTimeout(savedFlash)
    savedFlash = setTimeout(() => {
      saved.value = false
    }, 2000)
  } catch (err) {
    saveError.value = (err as Error).message
  } finally {
    saving.value = false
  }
}

onUnmounted(() => {
  destroyEditor()
  if (savedFlash) clearTimeout(savedFlash)
})
</script>

<template>
  <div class="editor">
    <div class="editor-header">
      <span class="editor-path" :title="path ?? undefined">{{ path ?? 'no file selected' }}</span>
      <span v-if="dirty" class="editor-dirty" title="unsaved changes">●</span>
      <span class="editor-status" :class="{ 'status-error': !!loadError || !!saveError }">
        {{ statusText }}
      </span>
      <button
        type="button"
        class="btn editor-save"
        :disabled="!path || !dirty || loading || saving"
        @click="save"
      >
        Save
      </button>
    </div>

    <div v-if="!path" class="editor-message">
      Select a file in the explorer to edit it.
    </div>
    <div v-else-if="loading" class="editor-message">loading…</div>
    <div v-else-if="loadError" class="editor-message error">{{ loadError }}</div>
    <div v-else ref="editorHost" class="editor-cm" />
  </div>
</template>
