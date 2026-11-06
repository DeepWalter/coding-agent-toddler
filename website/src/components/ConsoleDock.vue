<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type {
  Block,
  Mode,
  ModelSlotInfo,
  Paused,
  PromptAnswer,
  Question,
} from '../types'
import ConsoleDockInputBar from './ConsoleDockInputBar.vue'
import ConsoleDockPausePrompt from './ConsoleDockPausePrompt.vue'
import ConsoleDockPlanAsk from './ConsoleDockPlanAsk.vue'
import ConsoleDockQuestion from './ConsoleDockQuestion.vue'

// One floating card occupies the console's bottom slot at any time: the tool
// gate (state.paused), a plan awaiting THIS tab's decision, or the input bar.
// Paused first — only a live server wait is guaranteed actionable; an
// undecided plan on a stale tab may be dead (approved elsewhere).
const props = defineProps<{
  paused: Paused | null
  awaitingPlan: Extract<Block, { kind: 'plan' }> | null
  busy: boolean
  connected: boolean
  mode: Mode
  gatingEditable: boolean
  inPlan: boolean
  model: string
  modelSlot: string
  effort: string | null
  slots: ModelSlotInfo[]
  contextPct: number
}>()

const emit = defineEmits<{
  send: [text: string]
  cancel: []
  'set-mode': [mode: Mode]
  'set-model': [slot: string]
  'set-effort': [tier: string]
  compact: []
  'answer-tool': [toolId: string, answers: PromptAnswer[] | null]
  'approve-plan': [planId: string, mode: 'manual' | 'auto']
  'reject-plan': [planId: string, feedback: string]
}>()

// The questions of an ask-user pause, [] for any other shape.  A computed
// so the template gets a narrowed `Question[]` from the discriminated
// `choices` payload without repeating the type test twice.
const questions = computed<Question[]>(() =>
  props.paused?.prompt.choices.type === 'question'
    ? props.paused.prompt.choices.data
    : [],
)

// The draft survives the bar being swapped out for a confirmation: the dock
// stays mounted, so this ref outlives any ConsoleDockInputBar instance
// reading it.
const draft = ref('')

// --bar-h handshake, moved up from ConsoleDockInputBar: the dock is positioned
// against .pane-console (its parentElement — the template places it there),
// and the scroller's bottom padding reads --bar-h so pinned output ends just
// above whichever card floats, at whatever height it grew to.
const dockEl = ref<HTMLElement | null>(null)
let dockObserver: ResizeObserver | null = null

onMounted(() => {
  const el = dockEl.value
  const host = el?.parentElement
  if (!el || !host) return
  const applyDockHeight = () => host.style.setProperty('--bar-h', `${el.offsetHeight}px`)
  applyDockHeight()
  dockObserver = new ResizeObserver(applyDockHeight)
  dockObserver.observe(el)
})
onBeforeUnmount(() => dockObserver?.disconnect())
</script>

<template>
  <div ref="dockEl" class="console-dock">
    <ConsoleDockQuestion
      v-if="paused?.prompt.choices.type === 'question'"
      :key="paused.tool_id ?? 'questions'"
      :questions="questions"
      :tool-id="paused?.tool_id ?? ''"
      :busy="busy"
      @submit="(toolId, answers) => emit('answer-tool', toolId, answers)"
      @cancel="emit('cancel')"
    />
    <ConsoleDockPausePrompt
      v-else-if="paused"
      :paused="paused"
      :busy="busy"
      @answer="(answers) => emit('answer-tool', paused?.tool_id ?? '', answers)"
      @cancel="emit('cancel')"
    />
    <ConsoleDockPlanAsk
      v-else-if="awaitingPlan"
      :key="awaitingPlan.id"
      :block="awaitingPlan"
      :busy="busy"
      @approve="(planId, mode) => emit('approve-plan', planId, mode)"
      @reject="(planId, feedback) => emit('reject-plan', planId, feedback)"
      @cancel="emit('cancel')"
    />
    <ConsoleDockInputBar
      v-else
      v-model:draft="draft"
      :busy="busy"
      :connected="connected"
      :mode="mode"
      :gating-editable="gatingEditable"
      :in-plan="inPlan"
      :model="model"
      :model-slot="modelSlot"
      :effort="effort"
      :slots="slots"
      :context-pct="contextPct"
      @send="(text) => emit('send', text)"
      @cancel="emit('cancel')"
      @set-mode="(m) => emit('set-mode', m)"
      @set-model="(slot) => emit('set-model', slot)"
      @set-effort="(tier) => emit('set-effort', tier)"
      @compact="emit('compact')"
    />
  </div>
</template>
