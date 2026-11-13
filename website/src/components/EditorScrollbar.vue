<script setup lang="ts">
import { computed, ref } from 'vue'

/**
 * The editor's overlay scrollbar, used while the terminal panel is open.
 *
 * The native scrollbar is painted with the `.cm-scroller`, which sits under
 * the absolutely positioned terminal panel; this bar is a sibling in the
 * editor body with a higher z-index, so it stays visible (and draggable)
 * over the panel.  It is a pure mirror: FileEditor feeds it the scroller's
 * geometry and applies the emitted scroll positions back to the view.
 */

const props = defineProps<{
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}>()
const emit = defineEmits<{ scroll: [top: number] }>()

// Keep the thumb grabbable even on very long documents.
const MIN_THUMB = 24

const maxScroll = computed(() =>
  Math.max(0, props.scrollHeight - props.clientHeight),
)
const needed = computed(() => maxScroll.value > 0 && props.clientHeight > 0)
const thumbHeight = computed(() => {
  if (!needed.value || props.scrollHeight <= 0) return 0
  const proportional =
    (props.clientHeight * props.clientHeight) / props.scrollHeight
  return Math.max(MIN_THUMB, Math.min(props.clientHeight, proportional))
})
const room = computed(() => Math.max(0, props.clientHeight - thumbHeight.value))
const thumbTop = computed(() => {
  if (room.value <= 0 || maxScroll.value <= 0) return 0
  return (props.scrollTop / maxScroll.value) * room.value
})

const track = ref<HTMLElement | null>(null)
const dragging = ref(false)
let dragStartY = 0
let dragStartScroll = 0

function clamp(top: number): number {
  return Math.min(maxScroll.value, Math.max(0, top))
}

function onPointerDown(event: PointerEvent) {
  if (event.button !== 0 || !needed.value) return
  const rect = track.value?.getBoundingClientRect()
  if (!rect) return
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  const y = event.clientY - rect.top
  if (y >= thumbTop.value && y <= thumbTop.value + thumbHeight.value) {
    dragging.value = true
    dragStartY = event.clientY
    dragStartScroll = props.scrollTop
  } else {
    // Page toward the click, like the native bar's track.
    const direction = y < thumbTop.value ? -1 : 1
    emit('scroll', clamp(props.scrollTop + direction * props.clientHeight))
  }
}

function onPointerMove(event: PointerEvent) {
  if (!dragging.value || room.value <= 0) return
  const delta = ((event.clientY - dragStartY) / room.value) * maxScroll.value
  emit('scroll', clamp(dragStartScroll + delta))
}

function onPointerUp(event: PointerEvent) {
  if (!dragging.value) return
  dragging.value = false
  ;(event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId)
}

function onWheel(event: WheelEvent) {
  // deltaMode 1 is lines, 2 is pages (Firefox); normalise both to pixels.
  const step =
    event.deltaMode === 1
      ? event.deltaY * 20
      : event.deltaMode === 2
        ? event.deltaY * props.clientHeight
        : event.deltaY
  emit('scroll', clamp(props.scrollTop + step))
}
</script>

<template>
  <div
    v-if="needed"
    ref="track"
    class="editor-scrollbar"
    :class="{ dragging }"
    role="scrollbar"
    aria-orientation="vertical"
    :aria-valuemin="0"
    :aria-valuemax="maxScroll"
    :aria-valuenow="scrollTop"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointercancel="onPointerUp"
    @wheel.prevent="onWheel"
  >
    <div
      class="editor-scrollbar-thumb"
      :style="{ top: `${thumbTop}px`, height: `${thumbHeight}px` }"
    />
  </div>
</template>
