<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import type { Block } from '../types'
import { renderMarkdown } from '../markdown'
import { blockStatus } from '../blockStatus'
import ConsoleTopFloat from './ConsoleTopFloat.vue'
import MessageBubble from './MessageBubble.vue'
import PlanCard from './PlanCard.vue'
import StatusMark from './StatusMark.vue'
import ThinkingBlock from './ThinkingBlock.vue'
import ToolCard from './ToolCard.vue'

const props = defineProps<{
  blocks: Block[]
  /** The plan block whose decision the console dock is asking about — its
   *  inline card renders read-only (actions live in the dock). */
  awaitingPlanId: number | null
  /** True while the console dock floats an ask (tool gate or plan decision)
   *  over the input bar. */
  askVisible: boolean
}>()
const emit = defineEmits<{
  'approve-plan': [planId: string, mode: 'manual' | 'auto']
  'reject-plan': [planId: string, feedback: string]
  'open-file': [path: string]
}>()

const scroller = ref<HTMLElement | null>(null)
const atBottom = ref(true)

// Within 40px of the bottom counts as pinned to the bottom.
const AT_BOTTOM_PX = 40

// True from a local send until the user grabs the scroller: while set, the
// blocks watch keeps pinning no matter where the scrollbar sat — a sent
// message must not be answered below the fold.  Cleared only by real user
// gestures (wheel / pointer), never by scroll events: our own programmatic
// pins fire scroll events too, and when a burst of streamed content grows
// the scroll height between such an event and the pin it reports, position
// heuristics alone would drop out mid-stream.
let follow = false

function onScroll() {
  const el = scroller.value
  if (!el) return
  atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_PX
  updateFloat()
}

function scrollToBottom() {
  const el = scroller.value
  if (el && (atBottom.value || follow)) el.scrollTop = el.scrollHeight
}

/* ------------------------------------------------------------------ */
/* The floating input box                                              */
/* ------------------------------------------------------------------ */

/** The `user` rows, index-locked to `floatTexts`/`floatIds` — row i is the i-th
 *  `user` block, the one-row-per-block order the template renders.  The
 *  elements are cached and the rects are not, so a row that grows in place
 *  cannot go stale; only `blocks` changing identity invalidates the list. */
let floatRows: HTMLElement[] = []
let floatIds: number[] = []
let floatTexts: string[] = []
let floatRowsDirty = true

/** Which input boxes the reader has opened, by block id.  It lives here, on the
 *  blocks, because a box is read in two places — its own row and the console's
 *  top float — and they are the same message: opening one has to open both, and
 *  the float taking over a box the reader opened in the transcript has to find
 *  it already open. */
const expandedIds = reactive(new Set<number>())

function toggleExpanded(id: number) {
  if (!expandedIds.delete(id)) expandedIds.add(id)
}

/** Where the last tick found the boundary.  The rows above the fold are a
 *  prefix of an ordered, disjoint list, so the answer is unique and the walk
 *  converges from either side: starting where the previous tick stopped
 *  costs a rect or two as the fold moves, and a jump costs one pass, once. */
let floatHint = 0

/** Which input the echo names, and whether its box is up.  Two questions, and
 *  the second cannot be answered until the first has rendered: the room the
 *  echo needs is its own height. */
const floatText = ref<string | null>(null)
const floatId = ref<number | null>(null)
const floatShown = ref(false)
const floatBox = ref<InstanceType<typeof ConsoleTopFloat> | null>(null)

function toggleFloat() {
  if (floatId.value !== null) toggleExpanded(floatId.value)
}

function refreshFloatRows(el: HTMLElement) {
  if (!floatRowsDirty) return
  floatRows = [...el.querySelectorAll<HTMLElement>(':scope > .stream-row[data-kind="user"]')]
  floatIds = []
  floatTexts = []
  for (const block of props.blocks) {
    if (block.kind !== 'user') continue
    floatIds.push(block.id)
    floatTexts.push(block.text)
  }
  floatRowsDirty = false
}

/** The index of the last `user` box the fold has scrolled past, or -1.  All of
 *  it is viewport space against the pane's own top edge: the pane has no
 *  border, so that edge is the top of the visible output, and the pane's own
 *  14px padding never enters the arithmetic. */
function floatAt(el: HTMLElement): number {
  refreshFloatRows(el)
  if (!floatRows.length) return -1
  const fold = el.getBoundingClientRect().top
  // The *top* edge is what the fold has to reach: the echo takes a box over the
  // moment it starts to leave, not once it is gone.  Nothing has to be hidden
  // for that to read — the echo is the same box, same text, same width, same
  // clamp, so it covers the part still showing and the handover is invisible.
  //
  // Forward to the first row whose top is past the fold, then back to the last
  // one above it: `floatHint` starts the pair where the last tick left off.
  let i = Math.min(floatHint, floatRows.length - 1)
  for (; i < floatRows.length; i++) {
    if (floatRows[i].getBoundingClientRect().top > fold) break
  }
  for (i = Math.min(i, floatRows.length - 1); i >= 0; i--) {
    if (floatRows[i].getBoundingClientRect().top <= fold) break
  }
  floatHint = Math.max(i, 0)
  return i
}

/** Refresh the echo.  Reached from the scroll and resize paths only, never
 *  from the blocks watch: that fires on every streamed frame, and content
 *  streaming in lands *below* the fold, where it cannot move the rows this
 *  reads — the fold itself only moves when the scroller does.
 *
 *  The echo is shown at the live tail too, which is the point of reading the
 *  fold rather than the scroll position: a reply that runs past a screenful
 *  takes the prompt that asked for it off the top, and that prompt is the one
 *  thing the reader cannot recover from what is on screen. */
async function updateFloat() {
  const el = scroller.value
  if (!el) return
  const i = floatAt(el)
  const id = i < 0 ? null : (floatIds[i] ?? null)
  const text = i < 0 ? null : (floatTexts[i] ?? null)
  // The id travels with the *index*, not with the text: two inputs can read the
  // same, and the echo still has to take the second one's state rather than
  // carrying the first one's across.
  if (id !== floatId.value || text !== floatText.value) {
    floatId.value = id
    floatText.value = text
    // The box the next input has to reach is the echo's own, and it is not in
    // the document until the patch above lands — which is also the only place a
    // change of state (the echo moving to a box that reads differently) is
    // reflected before that boundary is measured.
    await nextTick()
  }
  // It floats for exactly as long as it has room: the next input ends the echo
  // by reaching its bottom edge, and there is no other limit.
  const box = floatBox.value?.box
  const next = i < 0 ? undefined : floatRows[i + 1]
  floatShown.value =
    !!box && (!next || next.getBoundingClientRect().top > box.getBoundingClientRect().bottom)
}

// Each reducer pass returns a new blocks array, so identity change fires
// on every frame — streaming text stays pinned while the user is at the
// bottom, and doesn't yank the scrollbar while they scroll up.
watch(() => props.blocks, async () => {
  await nextTick()
  // The float's row list survives the patch (identity, not geometry) — it is
  // the *rows* that may have been replaced, so invalidate and let the next
  // scroll or resize rebuild it.
  floatRowsDirty = true
  scrollToBottom()
})

// When an ask (tool gate / plan decision) pops into the dock, jump the
// console to the bottom: the content it asks about (the gated tool call,
// the proposed plan) ends up above the card — even if the user had scrolled
// up.  The dock publishes its height as --bar-h on the pane only after
// layout (ResizeObserver), and each change grows this scroller's bottom
// padding — so keep re-pinning until the bottom stops moving (two steady
// frames), not a fixed count: a card taller than the bar it replaced can
// resize the scroller a frame or two after the swap, and the pin must land
// on the grown padding or the console ends a few pixels short of the
// bottom.  Once pinned, the blocks watch keeps newer output glued to the
// bottom.  Cancelled when the ask resolves (a newer watch run owns the pin).
let askPin = 0
watch(
  () => props.askVisible,
  (visible) => {
    askPin += 1
    if (!visible) return
    const pin = askPin
    let lastBottom = -1
    let steady = 0
    let budget = 30
    const force = () => {
      if (pin !== askPin || budget-- <= 0) return
      const el = scroller.value
      if (el) {
        el.scrollTop = el.scrollHeight // deliberate: ask wins over atBottom
        const bottom = el.scrollHeight - el.clientHeight
        if (bottom === lastBottom) {
          if (++steady >= 2) return
        } else {
          steady = 0
          lastBottom = bottom
        }
      }
      requestAnimationFrame(force)
    }
    force()
  },
)

// Sending a message appends the user block on the spot (local_user reducer),
// even while the scrollbar sits up in history — jump to the bottom then and
// follow: the message and the answer streaming in below it must show, so set
// follow for the blocks watch above to keep pinning until the user scrolls
// away.  User blocks only grow on sends, never on streamed deltas.  The
// pre-flush watch runs before the patch adds the block, so re-pin over the
// next few frames to cover the patch, then follow takes over.
watch(
  () => {
    let sent = 0
    for (const b of props.blocks) if (b.kind === 'user') sent++
    return sent
  },
  (sent, prev) => {
    if (sent <= prev) return
    follow = true
    let frames = 3
    const force = () => {
      if (frames-- <= 0) return
      const el = scroller.value
      if (el) el.scrollTop = el.scrollHeight // deliberate: send wins over atBottom
      requestAnimationFrame(force)
    }
    force()
  },
)

// Growing the dock card writes a taller --bar-h, which grows this scroller's
// bottom padding and shrinks its content box — a ResizeObserver report with
// an unchanged border box.  That must NOT scroll the console: the card is an
// overlay, so while the user composes it may cover the tail instead of
// shoving the output up (typing never moves what's on screen, pinned or not).
// Streaming content grows the scroll height without resizing this box, so
// the blocks watch above stays the pin mechanism and keeps the newest line
// just above the card.  Re-pin only when the scroller's own height changed
// (border box) — pane resize — keeping the bottom edge glued for the user
// sitting at it.  (The old in-flow pause strip used to resize the wrap on
// gate toggles; confirmations now live in the floating dock, so the border
// box only changes on pane resize — gate resolution never jumps the content.)
let boxObserver: ResizeObserver | null = null
let lastBoxHeight = 0
const releaseFollow = () => {
  follow = false
}

onMounted(() => {
  const el = scroller.value
  if (!el) return
  // Seed with the current height so the first border-box report (barring
  // sub-pixel drift) reads as no change instead of an unrequested scroll.
  lastBoxHeight = el.getBoundingClientRect().height
  boxObserver = new ResizeObserver((entries) => {
    // Before the height guard: a taller --bar-h grows this scroller's bottom
    // padding — a report with an unchanged border box — and that can unpin
    // the bottom with no scroll event behind it.
    updateFloat()
    const blockSize = entries[0]?.borderBoxSize[0]?.blockSize
    if (typeof blockSize !== 'number' || Math.abs(blockSize - lastBoxHeight) < 0.5) {
      return
    }
    lastBoxHeight = blockSize
    if (atBottom.value) scrollToBottom()
  })
  boxObserver.observe(el)
  el.addEventListener('wheel', releaseFollow, { passive: true })
  el.addEventListener('pointerdown', releaseFollow)
  updateFloat()
})
onBeforeUnmount(() => {
  boxObserver?.disconnect()
  scroller.value?.removeEventListener('wheel', releaseFollow)
  scroller.value?.removeEventListener('pointerdown', releaseFollow)
})

/** The gutter mark's reading for a row — null for user input, the one kind
 *  that gets neither a mark nor the indent.  The two key on 'user' and must
 *  stay in step: `.stream-row:not([data-kind='user'])` in styles.css is the
 *  other half of this rule. */
function rowStatus(block: Block) {
  return block.kind === 'user' ? null : blockStatus(block)
}
</script>

<template>
  <div
    ref="scroller"
    class="console-pane"
    @scroll="onScroll"
  >
    <div
      v-for="block in blocks"
      :key="block.id"
      class="stream-row"
      :data-kind="block.kind"
    >
      <!-- One mark per block, outdented into the row's left padding: the
           indent every output block gets is what makes room for it, and
           user input — the one kind with no mark — is the one row that
           stays flush left.  data-kind carries the Block member rather
           than a class of the same name, which would one day collide with
           a component's own rule (.thinking already exists). -->
      <StatusMark :status="rowStatus(block)" />
      <MessageBubble
        v-if="block.kind === 'user'"
        :role="'user'"
        :text="block.text"
        :expanded="expandedIds.has(block.id)"
        @toggle="toggleExpanded(block.id)"
      />
      <MessageBubble
        v-else-if="block.kind === 'assistant'"
        :role="'assistant'"
        :text="block.content"
        @open-file="(path) => emit('open-file', path)"
      />
      <ThinkingBlock v-else-if="block.kind === 'thinking'" :block="block" />
      <ToolCard v-else-if="block.kind === 'tool'" :block="block" />
      <PlanCard
        v-else-if="block.kind === 'plan'"
        :block="block"
        :awaiting="awaitingPlanId === block.id"
        @approve="(planId, mode) => emit('approve-plan', planId, mode)"
        @reject="(planId, feedback) => emit('reject-plan', planId, feedback)"
      />
      <div v-else-if="block.kind === 'error'" class="stream-line error">
        ⛔ {{ block.message }}
      </div>
      <!-- Fold lines are synthetic markers — plain text, never markdown:
           a leading ~~~ would open an unterminated code fence. -->
      <div v-else-if="block.kind === 'fold'" class="stream-line fold">
        ~~~{{ block.text }}~~~
      </div>
      <!-- Notices are slash-command output — markdown by contract
           (the server sends it straight through). -->
      <div
        v-else
        class="stream-line notice markdown"
        v-html="renderMarkdown(block.message)"
      ></div>
    </div>
    <div v-if="!blocks.length" class="console-empty">
      No messages yet — describe a task below.
    </div>
  </div>
  <!-- The top float is this component's second root, a positioned sibling of
       the scroller inside the wrap — which is also its offset parent, and the
       reason it lands below the header without knowing the header's height.
       Outside the scroller: it echoes content that has scrolled away, so it
       must not scroll with it. -->
  <ConsoleTopFloat
    v-if="floatText !== null"
    ref="floatBox"
    :text="floatText"
    :shown="floatShown"
    :expanded="floatId !== null && expandedIds.has(floatId)"
    @toggle="toggleFloat"
  />
</template>
