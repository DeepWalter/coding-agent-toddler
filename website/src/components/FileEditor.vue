<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'

/**
 * Bottom-of-left-pane textarea editor.  Loads the file selected in the
 * explorer via `GET /api/file`, tracks unsaved changes against the last
 * saved snapshot, and persists with Ctrl/Cmd+S (or the Save button) via
 * `PUT /api/file` — the same path-safe endpoint the agent's WriteFile
 * tool backs onto.
 */

const props = defineProps<{ path: string | null }>()

const content = ref('')
const savedContent = ref('')
const totalLines = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const saving = ref(false)
const saveError = ref<string | null>(null)
const saved = ref(false)
const textarea = ref<HTMLTextAreaElement | null>(null)

const dirty = computed(() => content.value !== savedContent.value)

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

watch(() => props.path, async (path) => {
  const seq = ++loadSeq
  loadError.value = null
  saveError.value = null
  saved.value = false
  content.value = ''
  savedContent.value = ''
  totalLines.value = 0
  if (!path) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    const res = await api.readFile(path)
    if (seq !== loadSeq) return // superseded by a newer open
    content.value = res.content
    savedContent.value = res.content
    totalLines.value = res.total_lines
  } catch (err) {
    if (seq !== loadSeq) return
    loadError.value = (err as Error).message
  } finally {
    if (seq === loadSeq) {
      loading.value = false
      nextTick(() => textarea.value?.focus())
    }
  }
})

async function save() {
  if (!props.path || loading.value || saving.value) return
  saveError.value = null
  saving.value = true
  try {
    await api.writeFile(props.path, content.value)
    savedContent.value = content.value
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

function onKeydown(event: KeyboardEvent) {
  // Ctrl/Cmd+S saves; Tab indents two spaces (and stays in the editor).
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    save()
    return
  }
  if (event.key !== 'Tab' || event.metaKey || event.ctrlKey || event.altKey) return
  event.preventDefault()
  const el = event.currentTarget as HTMLTextAreaElement
  const start = el.selectionStart
  const end = el.selectionEnd
  content.value = content.value.slice(0, start) + '  ' + content.value.slice(end)
  nextTick(() => el.setSelectionRange(start + 2, start + 2))
}

onUnmounted(() => {
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
    <textarea
      v-else
      ref="textarea"
      v-model="content"
      class="editor-textarea"
      spellcheck="false"
      @keydown="onKeydown"
    />
  </div>
</template>
