<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Mode } from '../types'

const props = defineProps<{
  busy: boolean
  connected: boolean
  mode: Mode
  /** False while a plan is explored/proposed/awaiting approval — gating
   *  is pinned to manual until the plan is approved, so the pill is
   *  frozen (see server session_info.gating_editable). */
  gatingEditable: boolean
  /** True while a plan is pending or a plan turn is in flight — the pill
   *  reads "plan" instead of the gating mode (plan is not a gating mode). */
  inPlan: boolean
  model: string
  contextPct: number
}>()

const emit = defineEmits<{
  send: [text: string]
  cancel: []
  'set-mode': [mode: Mode]
}>()

/** The three /mode values plus the copy shown under each name in the popup.
 *  Icons are stroke-2 paths (24 viewBox) matching the theme-toggle and pane
 *  icons: ask = raised hand, auto = zap, plan = page with todo items. */
const MODE_OPTIONS: { value: Mode; icon: string[]; description: string }[] = [
  {
    value: 'manual',
    icon: [
      'M18 11V6a2 2 0 0 0-4 0v5',
      'M14 10V4a2 2 0 0 0-4 0v2',
      'M10 10.5V6a2 2 0 0 0-4 0v8',
      'M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15',
    ],
    description: 'Ask before edit',
  },
  {
    value: 'auto',
    icon: ['M13 2 3 14h9l-1 8 10-12h-9l1-8z'],
    description: 'Edit automatically',
  },
  {
    value: 'plan',
    icon: [
      'M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z',
      'M14 2v5h5',
      'M8.5 11.5l1.5 1.5 3-3',
      'M8 17h8',
    ],
    description: 'Explore and present a plan before executing',
  },
]

/** What the pill shows: "plan" while mode_label reports PLAN (a plan is
 *  armed or in flight), else the live permission gate. */
const displayMode = computed<Mode>(() => (props.inPlan ? 'plan' : props.mode))

/** The dropdown's selected entry (what re-picking must not re-emit).
 *  Plan is only a selectable state while it is *armed* — pending on an
 *  idle agent, where picking manual/auto must leave plan mode (arming
 *  already dropped gating to manual, so comparing against the gate
 *  would swallow those picks).  Once the plan is in flight the pill
 *  freezes before approval and the gate goes live again while the plan
 *  executes, so the selection tracks the gate — which is what the
 *  manual/auto entries edit in that state. */
const selectedMode = computed<Mode>(() =>
  props.inPlan && !props.busy ? 'plan' : props.mode,
)

/** The option matching what the pill shows — its icon renders in the pill. */
const currentOption = computed(() =>
  MODE_OPTIONS.find((opt) => opt.value === displayMode.value),
)

const open = ref(false)
const pickerEl = ref<HTMLElement | null>(null)
const pillEl = ref<HTMLButtonElement | null>(null)

function toggle() {
  if (!props.connected || !props.gatingEditable) return
  open.value = !open.value
}

function choose(mode: Mode) {
  open.value = false
  // A native select only fires change on an actual change — same here.
  // The comparison is against the entry the dropdown shows selected, not
  // the gate: while an armed plan reads "plan", picking manual must fire
  // even though gating is already manual — it is what leaves plan mode.
  if (mode !== selectedMode.value) emit('set-mode', mode)
}

function onPickerKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && open.value) {
    open.value = false
    pillEl.value?.focus()
  }
}

function onPointerDown(event: PointerEvent) {
  // Close when the click lands outside the picker — a click on a menu
  // entry lands inside and is handled by choose().
  if (open.value && pickerEl.value && !pickerEl.value.contains(event.target as Node)) {
    open.value = false
  }
}

onMounted(() => document.addEventListener('pointerdown', onPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onPointerDown))

// The draft is owned by the console dock (v-model:draft), which stays mounted
// while a confirmation card replaces this bar — typing is never lost.
const draft = defineModel<string>('draft', { default: '' })

function submit() {
  const trimmed = draft.value.trim()
  if (!trimmed || props.busy || !props.connected) return
  emit('send', trimmed)
  draft.value = ''
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    submit()
  }
}

// The textarea grows with its content up to 14 lines (CSS max-height caps it;
// beyond that it scrolls internally) while the bar floats over the console —
// typing never shrinks the console's visible output.  "auto" first, then
// measure, so the box also shrinks back when text is cleared after submit.
// (The dock's own height — not this bar's — is what --bar-h reports now; the
// card chrome and its measurement live in ConsoleDock.)
const textareaEl = ref<HTMLTextAreaElement | null>(null)

function autosizeTextarea() {
  const ta = textareaEl.value
  if (!ta) return
  ta.style.height = 'auto'
  ta.style.height = `${ta.scrollHeight}px`
}

watch(draft, async () => {
  await nextTick()
  autosizeTextarea()
})

onMounted(() => {
  autosizeTextarea()
  // Returning from a confirmation with a non-empty draft (a real cache
  // restore) — take focus back so typing can continue.  A plain remount
  // (page load, busy Cancel) leaves focus where it is.
  if (draft.value.trim()) {
    nextTick(() => textareaEl.value?.focus())
  }
})
</script>

<template>
  <div class="input-bar">
    <textarea
      ref="textareaEl"
      v-model="draft"
      class="input-bar-textarea"
      rows="1"
      placeholder="Describe a task…"
      :disabled="!connected"
      @keydown="onKeydown"
    />
    <div class="input-bar-footer">
      <div class="input-bar-meta">
        <div ref="pickerEl" class="mode-picker" @keydown="onPickerKeydown">
          <button
            ref="pillEl"
            type="button"
            class="mode-toggle"
            :class="displayMode"
            :disabled="!connected || !gatingEditable"
            aria-haspopup="menu"
            :aria-expanded="open"
            title="mode — manual / auto / plan"
            @click="toggle"
          >
            <svg
              class="mode-toggle-icon"
              viewBox="0 0 24 24"
              width="12"
              height="12"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path v-for="d in currentOption?.icon" :key="d" :d="d" />
            </svg>
            {{ displayMode }}
          </button>
          <div v-if="open" class="mode-menu" role="menu" aria-label="Workflow mode">
            <button
              v-for="opt in MODE_OPTIONS"
              :key="opt.value"
              type="button"
              role="menuitemradio"
              class="mode-menu-item"
              :class="[opt.value, { selected: opt.value === selectedMode }]"
              :aria-checked="opt.value === selectedMode"
              :disabled="opt.value === 'plan' && busy"
              :title="opt.value === 'plan' && busy ? 'Set while the agent is idle' : undefined"
              @click="choose(opt.value)"
            >
              <span class="mode-menu-label">
                <svg
                  class="mode-menu-icon"
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
                  <path v-for="d in opt.icon" :key="d" :d="d" />
                </svg>
                {{ opt.value }}
              </span>
              <span class="mode-menu-desc">{{ opt.description }}</span>
            </button>
          </div>
        </div>
        <template v-if="model">
          <span class="input-bar-meta-sep">·</span>
          <span class="input-bar-model">{{ model }}</span>
          <span class="input-bar-meta-sep">·</span>
          <span class="input-bar-context">context {{ contextPct }}%</span>
        </template>
      </div>
      <button
        v-if="busy"
        type="button"
        class="btn danger small"
        @click="emit('cancel')"
      >
        Cancel
      </button>
      <button
        v-else
        type="button"
        class="btn primary small"
        :disabled="!connected || !draft.trim()"
        @click="submit"
      >
        Send
      </button>
    </div>
  </div>
</template>
