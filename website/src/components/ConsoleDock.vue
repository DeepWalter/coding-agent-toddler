<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { Block, Mode, Paused } from '../types'
import InputBar from './InputBar.vue'
import PausePrompt from './PausePrompt.vue'
import PlanAsk from './PlanAsk.vue'

// One floating card occupies the console's bottom slot at any time: the tool
// gate (state.paused), a plan awaiting THIS tab's decision, or the input bar.
// Paused first — only a live server wait is guaranteed actionable; an
// undecided plan on a stale tab may be dead (approved elsewhere).
defineProps<{
  paused: Paused | null
  awaitingPlan: Extract<Block, { kind: 'plan' }> | null
  busy: boolean
  connected: boolean
  mode: Mode
  gatingEditable: boolean
  inPlan: boolean
  model: string
  contextPct: number
}>()

const emit = defineEmits<{
  send: [text: string]
  cancel: []
  'set-mode': [mode: Mode]
  'approve-tool': []
  'deny-tool': []
  'approve-plan': [planId: string, mode: 'manual' | 'auto']
  'reject-plan': [planId: string, feedback: string]
}>()

// The draft survives the bar being swapped out for a confirmation: the dock
// stays mounted, so this ref outlives any InputBar instance reading it.
const draft = ref('')

// --bar-h handshake, moved up from InputBar: the dock is positioned against
// .pane-console (its parentElement — the template places it there), and the
// scroller's bottom padding reads --bar-h so pinned output ends just above
// whichever card floats, at whatever height it grew to.
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
    <PausePrompt
      v-if="paused"
      :paused="paused"
      :busy="busy"
      @approve="emit('approve-tool')"
      @deny="emit('deny-tool')"
      @cancel="emit('cancel')"
    />
    <PlanAsk
      v-else-if="awaitingPlan"
      :key="awaitingPlan.id"
      :block="awaitingPlan"
      :busy="busy"
      @approve="(planId, mode) => emit('approve-plan', planId, mode)"
      @reject="(planId, feedback) => emit('reject-plan', planId, feedback)"
      @cancel="emit('cancel')"
    />
    <InputBar
      v-else
      v-model:draft="draft"
      :busy="busy"
      :connected="connected"
      :mode="mode"
      :gating-editable="gatingEditable"
      :in-plan="inPlan"
      :model="model"
      :context-pct="contextPct"
      @send="(text) => emit('send', text)"
      @cancel="emit('cancel')"
      @set-mode="(m) => emit('set-mode', m)"
    />
  </div>
</template>
