<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { TreeEntry, TreeNode } from '../types'

/**
 * Left-pane file browser.  The backend serves a flat gitignore-aware
 * listing (`/api/tree`); this component nests it, keeps per-directory
 * expansion state (top-level dirs start open), and re-fetches on the
 * refresh button so files the agent writes show up.
 */

const props = defineProps<{ root: string | null }>()
const emit = defineEmits<{ 'open-file': [path: string] }>()

const tree = ref<TreeNode[]>([])
const expanded = ref<Record<string, boolean>>({})
const selected = ref<string | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

const rootName = computed(() => {
  const parts = (props.root ?? '').split('/').filter(Boolean)
  return parts.length ? parts[parts.length - 1] : 'repo'
})

/** Entries arrive dirs-before-files per level (parents first) — nest and sort. */
function buildTree(entries: TreeEntry[]): TreeNode[] {
  const nodes = new Map<string, TreeNode>()
  const roots: TreeNode[] = []
  for (const entry of entries) {
    const parts = entry.path.split('/')
    const node: TreeNode = {
      name: parts[parts.length - 1],
      path: entry.path,
      type: entry.type,
      children: entry.type === 'dir' ? [] : null,
    }
    nodes.set(entry.path, node)
    const parent = parts.length > 1 ? nodes.get(parts.slice(0, -1).join('/')) : undefined
    if (parent?.children) parent.children.push(node)
    else roots.push(node)
  }
  const sort = (list: TreeNode[]) => {
    list.sort((a, b) =>
      a.type !== b.type
        ? (a.type === 'dir' ? -1 : 1)
        : a.name.localeCompare(b.name),
    )
    for (const n of list) if (n.children) sort(n.children)
  }
  sort(roots)
  return roots
}

function applyTree(entries: TreeEntry[]) {
  tree.value = buildTree(entries)
  // First-level dirs start expanded; a refresh keeps the user's state
  // and only opens brand-new top-level dirs.
  const next = { ...expanded.value }
  for (const node of tree.value) {
    if (node.type === 'dir' && !(node.path in next)) next[node.path] = true
  }
  expanded.value = next
}

async function load() {
  loading.value = true
  error.value = null
  try {
    applyTree((await api.tree()).entries)
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)

interface Row {
  node: TreeNode
  depth: number
}

/** The expanded tree flattened for rendering, with indent depths. */
const visible = computed<Row[]>(() => {
  const rows: Row[] = []
  const walk = (nodes: TreeNode[], depth: number) => {
    for (const node of nodes) {
      rows.push({ node, depth })
      if (node.type === 'dir' && expanded.value[node.path] && node.children) {
        walk(node.children, depth + 1)
      }
    }
  }
  walk(tree.value, 0)
  return rows
})

function onRowClick(node: TreeNode) {
  if (node.type === 'dir') {
    expanded.value = { ...expanded.value, [node.path]: !expanded.value[node.path] }
  } else {
    selected.value = node.path
    emit('open-file', node.path)
  }
}
</script>

<template>
  <div class="explorer">
    <div class="explorer-header">
      <span class="explorer-title">Explorer</span>
      <span class="explorer-root">{{ rootName }}</span>
      <button
        type="button"
        class="explorer-refresh"
        title="Refresh — files the agent wrote show up here"
        :disabled="loading"
        @click="load"
      >
        ↻
      </button>
    </div>

    <div class="explorer-list">
      <template v-if="error">
        <div class="explorer-empty error">{{ error }}</div>
      </template>
      <template v-else-if="visible.length">
        <button
          v-for="{ node, depth } in visible"
          :key="node.path"
          type="button"
          class="tree-row"
          :class="{ selected: node.path === selected }"
          :style="{ paddingLeft: `${10 + depth * 14}px` }"
          @click="onRowClick(node)"
        >
          <span class="tree-chevron">
            {{ node.type === 'dir' ? (expanded[node.path] ? '▾' : '▸') : '' }}
          </span>
          <span class="tree-name" :class="node.type === 'dir' ? 'tree-dir' : 'tree-file'">
            {{ node.name }}
          </span>
        </button>
      </template>
      <div v-else class="explorer-empty">
        {{ loading ? 'loading…' : 'empty repository' }}
      </div>
    </div>
  </div>
</template>
