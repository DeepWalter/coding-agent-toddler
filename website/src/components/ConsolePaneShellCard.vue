<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { blockStatus } from '../blockStatus'
import { useOverflow } from '../composables/useOverflow'
import {
  SHELL_CARD_NAME,
  shellCommandText,
  shellDescription,
  shellFailed,
  shellOutputText,
  shellTextTab,
  type ShellSide,
} from '../shellCall'
import type { Block, TabEntry } from '../types'

const props = defineProps<{ block: Extract<Block, { kind: 'tool' }> }>()
const emit = defineEmits<{
  'open-text': [tab: Extract<TabEntry, { kind: 'text' }>]
}>()

const state = computed(() => blockStatus(props.block))
const description = computed(() => shellDescription(props.block))
const command = computed(() => shellCommandText(props.block))
const output = computed(() => shellOutputText(props.block))
// "waiting for result…" / "cancelled before execution" are notes about the
// call rather than its output, and read as such.
const outputIsNote = computed(() => !props.block.result)

// Both rows are clipped to three lines in CSS and measured (useOverflow):
// the clip is what decides whether a row has more to show, and therefore
// whether it is a control at all.
const commandEl = ref<HTMLElement | null>(null)
const outputEl = ref<HTMLElement | null>(null)
const commandFlow = useOverflow(commandEl)
const outputFlow = useOverflow(outputEl)

const canOpenCommand = computed(() => !!command.value && commandFlow.clamped.value)
const canOpenOutput = computed(() => !!output.value && outputFlow.clamped.value)

// Text can change in place — a command still streaming, a result landing
// while the row is on screen — and a swap that leaves the box the same
// height raises no resize, so ask again after each change.
watch([command, output], () =>
  nextTick(() => {
    commandFlow.measure()
    outputFlow.measure()
  }),
)

function open(side: ShellSide, canOpen: boolean) {
  if (!canOpen) return
  emit('open-text', shellTextTab(props.block, side))
}

// --- copy -----------------------------------------------------------------

/** Write *text* to the clipboard.  The clipboard API needs a secure context,
 *  and this app is reachable over the LAN — fall back to the textarea trick
 *  rather than failing silently on plain HTTP. */
async function writeClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const scratch = document.createElement('textarea')
  scratch.value = text // never innerHTML — the text is the model's
  scratch.setAttribute('readonly', '')
  scratch.style.position = 'fixed'
  scratch.style.top = '-1000px'
  document.body.appendChild(scratch)
  scratch.select()
  const ok = document.execCommand('copy')
  scratch.remove()
  if (!ok) throw new Error('the browser refused the copy')
}

const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | null = null
onBeforeUnmount(() => {
  if (copyTimer) clearTimeout(copyTimer)
})

async function copyCommand() {
  if (!command.value) return
  try {
    await writeClipboard(command.value)
    copied.value = true
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => {
      copied.value = false
    }, 1200)
  } catch (err) {
    // Nothing to show the reader beyond the button not changing; the reason
    // belongs in the console.
    console.warn('copying the command failed:', err)
  }
}
</script>

<template>
  <div class="shell-call">
    <!-- The call's own line about what it does, read as ordinary output —
         the box is only what ran and what came back.  Absent on calls made
         before the parameter existed, which the line then simply omits. -->
    <div class="tool-call-heading">
      <span class="tool-call-name">{{ SHELL_CARD_NAME }}</span>
      <span v-if="description" class="tool-call-desc">{{ description }}</span>
    </div>
    <div class="shell-card" :class="state">
      <div
        class="shell-card-row"
        data-row="in"
        :data-clamped="canOpenCommand ? '' : undefined"
        :role="canOpenCommand ? 'button' : undefined"
        :tabindex="canOpenCommand ? 0 : undefined"
        @click="open('in', canOpenCommand)"
        @keydown.enter.prevent="open('in', canOpenCommand)"
        @keydown.space.prevent="open('in', canOpenCommand)"
      >
        <div class="tool-card-section-title">IN</div>
        <pre ref="commandEl" class="shell-card-text">{{ command }}</pre>
        <button
          v-if="command"
          type="button"
          class="shell-card-copy"
          :class="{ copied }"
          :aria-label="copied ? 'Command copied' : 'Copy command'"
          title="Copy command"
          @click.stop="copyCommand"
        >
          <svg
            viewBox="0 0 24 24"
            width="13"
            height="13"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <template v-if="copied">
              <polyline points="20 6 9 17 4 12" />
            </template>
            <template v-else>
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </template>
          </svg>
        </button>
      </div>
      <div
        class="shell-card-row"
        data-row="out"
        :data-clamped="canOpenOutput ? '' : undefined"
        :role="canOpenOutput ? 'button' : undefined"
        :tabindex="canOpenOutput ? 0 : undefined"
        @click="open('out', canOpenOutput)"
        @keydown.enter.prevent="open('out', canOpenOutput)"
        @keydown.space.prevent="open('out', canOpenOutput)"
      >
        <div class="tool-card-section-title">OUT</div>
        <pre
          ref="outputEl"
          class="shell-card-text"
          :class="{ note: outputIsNote, error: shellFailed(block) }"
        >{{ output }}</pre>
      </div>
    </div>
  </div>
</template>
