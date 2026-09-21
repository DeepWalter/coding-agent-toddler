<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ModelSlotInfo } from '../types'
import {
  EFFORT_STOPS,
  effortLabel,
  stopForTier,
  stopIndexOf,
  tierForStop,
  type EffortStop,
} from '../effort'
import { usePicker } from '../composables/usePicker'
import { formatTokens } from '../utils'

const props = defineProps<{
  /** The live model spec — the conversation's identity, notation included
   *  ("deepseek-flash[1m]"). */
  model: string
  /** The slot that spec was picked by (the row to highlight). */
  modelSlot: string
  /** The live effort tier, verbatim; null when the conversation names
   *  none, which reads as "default" (the endpoint's own). */
  effort: string | null
  /** Every slot the conversation can switch to, in menu order, each with
   *  the window its spec is accounted for. */
  slots: ModelSlotInfo[]
  busy: boolean
  connected: boolean
}>()

const emit = defineEmits<{
  'set-model': [slot: string]
  'set-effort': [tier: string]
}>()

/** The picker's one gate — idle and connected.  Deliberately NOT the mode
 *  pill's ``gatingEditable``: that freezes the permission gate while a plan
 *  is explored, and the model is not part of gating. */
const disabled = computed(() => props.busy || !props.connected)

// Shared with the mode picker, so the two popups cannot both be open.
const { open, toggle, close } = usePicker('model')

const pickerEl = ref<HTMLElement | null>(null)
const pillEl = ref<HTMLButtonElement | null>(null)
const railEl = ref<HTMLElement | null>(null)

// ---------------------------------------------------------------------------
// The model rows
// ---------------------------------------------------------------------------

/** The slot the conversation was picked by — the row to highlight — or
 *  null when that is not answerable.
 *
 *  The stored slot is provenance, so it is trusted only while it still
 *  names the live model: an install that retargets `TODDLER_PRO_MODEL`
 *  underneath an old conversation leaves that slot pointing at a model the
 *  conversation does not run, and highlighting it would claim otherwise.
 *  The same goes for a row that names no slot at all (written before the
 *  column existed).  Either way the answer falls back to the spec below. */
const pickedSlot = computed<ModelSlotInfo | null>(
  () =>
    props.slots.find(
      (s) => s.name === props.modelSlot && s.spec === props.model,
    ) ?? null,
)

/** A row is selected when it is the picked one.  Without a picked slot to
 *  go on, selection falls back to naming the live spec — which can be
 *  several rows at once, and on a default install (all three slots on one
 *  id) is exactly that: the honest answer when the slot is not known. */
function isSelected(slot: ModelSlotInfo): boolean {
  if (pickedSlot.value) return slot.name === pickedSlot.value.name
  return slot.spec === props.model
}

function choose(slot: ModelSlotInfo) {
  // The menu stays open — it is a picker you compare within, and the
  // session_info echo moves the highlight while it is still visible.
  // (The mode menu closes on choose; this one is the exception.)
  //
  // Re-picking the row already selected is not sent.  Note this compares
  // SLOTS, not specs: two slots routinely name one model, and picking the
  // other one is a real change — it is what the highlight follows.
  if (slot.name === props.modelSlot) return
  emit('set-model', slot.name)
}

// ---------------------------------------------------------------------------
// The effort control
// ---------------------------------------------------------------------------

/** The stop the pointer is on, or null when no drag is in flight.  The
 *  knob renders from here while dragging, so a session_info landing
 *  mid-drag (another tab's switch, or the echo of our own previous
 *  commit) cannot yank it out from under the pointer.  Releasing hands
 *  over to the tier the commit applies optimistically (`setEffort` in
 *  useConsole), so the readout keeps the released stop rather than falling
 *  back to the one the server is still holding. */
const dragging = ref<EffortStop | null>(null)

/** Where the knob sits: the live drag wins, the server otherwise. */
const stop = computed<EffortStop>(
  () => dragging.value ?? stopForTier(props.effort),
)
const stopIndex = computed(() => stopIndexOf(stop.value))
/** What the row reads.  Mid-drag it names the stop under the pointer — the
 *  value a release would commit — so a drag is legible while it happens.
 *  Otherwise it is the live tier verbatim, which keeps a collapsed one
 *  ("xhigh") and an unset one ("default") saying what they are. */
const effortDisplay = computed(() =>
  dragging.value === null ? effortLabel(props.effort) : dragging.value,
)
/** The knob's position as 0…1, handed to CSS as --t (the same inline
 *  custom-property idiom as the context gauge's --gauge-t). */
const knobT = computed(() => stopIndex.value / (EFFORT_STOPS.length - 1))

function commit(next: EffortStop) {
  if (disabled.value) return
  // Only on an actual change — the same rule the mode picker's choose()
  // follows, and the reason a click on the current stop is free.
  if (next === stopForTier(props.effort)) return
  emit('set-effort', tierForStop(next))
}

/** The label's click: step to the next stop, wrapping past the top back to
 *  `none`.  Wrapping is right for a cycle and wrong for the arrows on the
 *  rail — see onRailKeydown. */
function cycle() {
  const next = EFFORT_STOPS[(stopIndex.value + 1) % EFFORT_STOPS.length]
  commit(next)
}

/** The stop nearest *clientX* on the rail.  Rounding to the nearest stop
 *  means a drag released between two of them still lands on one, so the
 *  knob is never parked at a fraction and there is no ambiguous release. */
function stopForX(clientX: number): EffortStop {
  const rail = railEl.value
  if (!rail) return stopForTier(props.effort)
  const rect = rail.getBoundingClientRect()
  // A zero-width rail (mid-layout, or a hidden ancestor) would make the
  // fraction NaN and every index below it meaningless.
  if (rect.width <= 0) return stopForTier(props.effort)
  const last = EFFORT_STOPS.length - 1
  const t = (clientX - rect.left) / rect.width
  return EFFORT_STOPS[Math.max(0, Math.min(last, Math.round(t * last)))]
}

// The drag idiom is the pane dividers' (App.vue: onDividerDown/Move):
// capture the pointer on pointerdown and the moves keep arriving here —
// even after the pointer leaves the rail and the popup — so no document
// listeners are needed.  No preventDefault on the down: suppressing it
// would suppress the focus the rail needs for an immediate arrow key.
function onRailPointerDown(event: PointerEvent) {
  if (disabled.value || event.button !== 0 || !event.isPrimary) return
  const rail = railEl.value
  if (!rail) return
  rail.setPointerCapture(event.pointerId)
  dragging.value = stopForX(event.clientX)
}

function onRailPointerMove(event: PointerEvent) {
  if (dragging.value === null) return
  dragging.value = stopForX(event.clientX)
}

function onRailPointerUp(event: PointerEvent) {
  if (dragging.value === null) return
  const next = stopForX(event.clientX)
  endDrag(event.pointerId)
  // The one place a drag reaches the wire: committing on every stop
  // crossing would re-key the context, write the row and broadcast to
  // every tab mid-gesture, and would make an abandoned drag impossible to
  // undo.
  commit(next)
}

/** An interrupted drag — an OS gesture, a stolen capture, or the popup
 *  unmounting under the pointer — drops the in-flight stop without
 *  committing, so the knob falls back to what the server holds. */
function onRailPointerCancel() {
  dragging.value = null
}

function endDrag(pointerId: number) {
  const rail = railEl.value
  // releasePointerCapture throws on an id that is not captured, and the
  // browser releases implicitly on pointerup anyway.
  if (rail?.hasPointerCapture(pointerId)) rail.releasePointerCapture(pointerId)
  dragging.value = null
}

function onRailKeydown(event: KeyboardEvent) {
  if (disabled.value) return
  const last = EFFORT_STOPS.length - 1
  const i = stopIndex.value
  // The arrows clamp rather than wrap — a slider is a scale, and stepping
  // off the top onto `none` would silently turn thinking off.  (The label
  // click cycles, which is where wrapping belongs.)
  const target =
    event.key === 'ArrowRight' || event.key === 'ArrowUp'
      ? Math.min(last, i + 1)
      : event.key === 'ArrowLeft' || event.key === 'ArrowDown'
        ? Math.max(0, i - 1)
        : event.key === 'Home'
          ? 0
          : event.key === 'End'
            ? last
            : null
  if (target === null) return
  // The dock floats over a scroller — an unhandled arrow would scroll the
  // console behind it.
  event.preventDefault()
  event.stopPropagation()
  commit(EFFORT_STOPS[target])
}

// A turn starting (here or on another tab) or the socket dropping takes the
// gate away: close, rather than leave a popup whose every click the server
// would reject, and drop any drag the gate interrupted.
watch(disabled, (off) => {
  if (!off) return
  dragging.value = null
  close()
})

// ---------------------------------------------------------------------------
// Popup plumbing — the mode picker's, unchanged
// ---------------------------------------------------------------------------

function onPointerDown(event: PointerEvent) {
  // Close when the click lands outside the picker — a click on a menu row
  // lands inside and is handled by that row.
  if (open.value && pickerEl.value && !pickerEl.value.contains(event.target as Node)) {
    close()
  }
}

function onPickerKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && open.value) {
    close()
    pillEl.value?.focus()
  }
}

onMounted(() => document.addEventListener('pointerdown', onPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onPointerDown))
</script>

<template>
  <div
    ref="pickerEl"
    class="model-picker"
    @keydown="onPickerKeydown"
  >
    <button
      ref="pillEl"
      type="button"
      class="model-toggle"
      :disabled="disabled"
      aria-haspopup="menu"
      :aria-expanded="open"
      title="model and thinking effort"
      @click="toggle(!disabled)"
    >
      <!-- One text run, so the pill ellipsizes as a whole on a narrow
           console pane.  The tier is tinted but deliberately not a separate
           flex item: an unshrinkable one spills out of the pill's border
           once the row runs out of room. -->
      {{ model }} <span class="model-toggle-effort">{{ effortLabel(effort) }}</span>
    </button>
    <div
      v-if="open"
      class="model-menu"
      role="menu"
      aria-label="Model and thinking effort"
    >
      <button
        v-for="slot in slots"
        :key="slot.name"
        type="button"
        role="menuitemradio"
        class="model-menu-item"
        :class="{ selected: isSelected(slot) }"
        :aria-checked="isSelected(slot)"
        @click="choose(slot)"
      >
        <span class="model-menu-name">{{ slot.name }}</span>
        <span class="model-menu-spec">{{ slot.spec }}</span>
        <span class="model-menu-window">{{ formatTokens(slot.context_tokens) }}</span>
      </button>
      <!-- A group, not a menuitem: this row is a four-stop control, not one
           more radio.  Announcing it as a radio would describe a widget the
           user cannot see; role=slider on the rail below says what it is. -->
      <div
        class="effort-row"
        role="group"
        aria-label="Thinking effort"
      >
        <div class="effort-head">
          <button
            type="button"
            class="effort-label"
            :disabled="disabled"
            :aria-label="`effort, ${effortLabel(effort)} — click to cycle`"
            @click="cycle"
          >
            effort
          </button>
          <span class="effort-value">{{ effortDisplay }}</span>
        </div>
        <!-- The track is padding only; the rail inside it is what gets
             measured, so its 0%…100% span is exactly the four stops and
             the knob can overhang the ends without leaving the row. -->
        <div class="effort-track">
          <div
            ref="railEl"
            class="effort-rail"
            role="slider"
            :tabindex="disabled ? -1 : 0"
            :aria-label="`effort — ${effortLabel(effort)}`"
            aria-orientation="horizontal"
            :aria-valuemin="0"
            :aria-valuemax="EFFORT_STOPS.length - 1"
            :aria-valuenow="stopIndex"
            :aria-valuetext="effortDisplay"
            :aria-disabled="disabled"
            @pointerdown="onRailPointerDown"
            @pointermove="onRailPointerMove"
            @pointerup="onRailPointerUp"
            @pointercancel="onRailPointerCancel"
            @lostpointercapture="onRailPointerCancel"
            @keydown="onRailKeydown"
          >
            <span class="effort-line" aria-hidden="true" />
            <span
              class="effort-fill"
              :style="{ '--t': knobT }"
              aria-hidden="true"
            />
            <span
              v-for="(s, i) in EFFORT_STOPS"
              :key="s"
              class="effort-stop"
              :style="{ '--t': i / (EFFORT_STOPS.length - 1) }"
              aria-hidden="true"
            />
            <span
              class="effort-knob"
              :style="{ '--t': knobT }"
              aria-hidden="true"
            />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
