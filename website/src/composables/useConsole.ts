import { reactive, toRaw } from 'vue'
import type {
  Block,
  Command,
  ConsoleState,
  Frame,
  Mode,
  PlanDecision,
} from '../types'

/**
 * Console state — a single linear event stream applied by a pure
 * reducer-style function (`reduce`), so the transition logic stays
 * deterministic and testable outside the component tree.
 *
 * The stream mixes committed transcript blocks (from hello replay) with
 * live blocks: assistant text accumulates into one open block per turn,
 * tool calls become collapsible cards, and a finished turn closes the
 * assistant block so the next turn starts fresh.  `hello` replaces
 * everything (reconnect = replay from storage).
 */

export type ConsoleAction =
  | Frame
  | { type: 'local_user'; text: string }
  | { type: 'local_mode'; mode: Mode }
  | { type: 'local_plan_decision'; planId: number; decision: PlanDecision | null }

export function initialState(): ConsoleState {
  return {
    nextId: 0,
    blocks: [],
    busy: false,
    session: null,
    conversation: null,
    paused: null,
  }
}

/** Pure transition: returns a new state, never mutates the input. */
export function reduce(state: ConsoleState, action: ConsoleAction): ConsoleState {
  const next = structuredClone(state)
  apply(next, action)
  return next
}

/** A Block minus its id, distributed per kind so literals type-check. */
type BlockSeed = {
  [K in Block['kind']]: { kind: K } & Omit<Extract<Block, { kind: K }>, 'id'>
}[Block['kind']]

function push(next: ConsoleState, block: BlockSeed): void {
  next.blocks.push({ ...block, id: next.nextId++ } as Block)
}

function findTool(
  s: ConsoleState,
  toolId: string,
  opts: { open?: boolean } = {},
): Extract<Block, { kind: 'tool' }> | null {
  for (let i = s.blocks.length - 1; i >= 0; i--) {
    const b = s.blocks[i]
    if (
      b.kind === 'tool'
      && b.tool_id === toolId
      && (opts.open === undefined || b.open === opts.open)
    ) {
      return b
    }
  }
  return null
}

function lastOpenToolId(s: ConsoleState): string | null {
  for (let i = s.blocks.length - 1; i >= 0; i--) {
    const b = s.blocks[i]
    if (b.kind === 'tool' && b.open) return b.tool_id
  }
  return null
}

function closeAssistant(s: ConsoleState): void {
  // Open assistant blocks may sit anywhere in the stream (text before a
  // tool card, text after it) — the turn ending closes all of them so a
  // later text_delta can never merge into a finished turn.
  for (const b of s.blocks) {
    if (b.kind === 'assistant' && !b.closed) b.closed = true
  }
}

function closeOpenTools(s: ConsoleState): void {
  // A turn ending mid-call (cancel, fatal error, or the loop stopping
  // after a tool request without an end frame) never emits a
  // tool_call_end — close any open cards so they stop spinning
  // "waiting for result…" and render as cancelled instead.
  for (const b of s.blocks) {
    if (b.kind === 'tool' && b.open) b.open = false
  }
}

/** Human labels for replayed synthetic markers (see ReplayMessage.fold).
 *  Must stay byte-identical with the live pushes (turn_cancelled below) so
 *  a reload renders the same fold line. */
const FOLD_LABELS: Record<'cancelled' | 'compacted', string> = {
  cancelled: 'The previous turn was cancelled by the user',
  compacted: 'Compacted',
}

/** Fold text for a replayed marker.  Unknown fold values fall back to the
 *  content (brackets trimmed) — a future server key must never degrade into
 *  a boxed user message. */
function foldText(fold: string, content: string): string {
  if (fold === 'cancelled' || fold === 'compacted') return FOLD_LABELS[fold]
  return content.trim().replace(/^\[/, '').replace(/\]$/, '')
}

function apply(s: ConsoleState, action: ConsoleAction): void {
  switch (action.type) {
    case 'hello': {
      s.session = action.session
      s.conversation = action.conversation
      s.busy = action.busy
      // Resume a mid-approval reconnect — but only while actually
      // paused; the snapshot can outlive the turn by a race.
      s.paused = action.busy && action.paused
        ? { prompt: action.paused.prompt, choices: action.paused.choices, tool_id: null }
        : null
      s.blocks = []
      for (const msg of action.messages) {
        if (msg.role === 'tool') {
          // Replayed tool call — same shape as tool_call_end, closed so
          // the card renders its result state instead of "running".
          push(s, {
            kind: 'tool',
            tool_id: msg.tool_id ?? '',
            tool_name: msg.tool_name ?? '',
            input: msg.input ?? {},
            result: msg.result ?? null,
            open: false,
          })
          continue
        }
        if (!msg.content) continue
        if (msg.role === 'user' && msg.fold) {
          // Synthetic marker — fold line, not a user bubble.
          push(s, { kind: 'fold', text: foldText(msg.fold, msg.content) })
        } else if (msg.role === 'user') push(s, { kind: 'user', text: msg.content })
        else push(s, { kind: 'assistant', text: msg.content, closed: true })
      }
      // A plan proposed mid-turn is snapshotted server-side (proposal +
      // latest step statuses); re-render the card so a reconnecting or
      // new tab can still see and approve it.  Same busy gate as paused
      // — the snapshot can outlive the turn by a race.
      if (action.busy && action.plan) {
        push(s, {
          kind: 'plan',
          plan: action.plan.plan,
          steps: action.plan.steps,
          decision: null,
        })
      }
      break
    }
    case 'turn_started':
      s.busy = true
      break
    case 'state':
      s.busy = action.busy
      break
    case 'text_delta': {
      const last = s.blocks[s.blocks.length - 1]
      if (last && last.kind === 'assistant' && !last.closed) {
        last.text += action.text
      } else {
        push(s, { kind: 'assistant', text: action.text, closed: false })
      }
      break
    }
    case 'tool_call_start': {
      // Streaming mode emits two starts per call — the live stream
      // handler yields one as chunks arrive, the execution phase yields
      // another before running the tool, with the same tool_id.  Upsert
      // by tool_id so one call renders one card; the second start's
      // full parameters merge over the stream's partial input.
      const existing = findTool(s, action.tool_id, { open: true })
      if (existing) {
        if (action.partial_input) Object.assign(existing.input, action.partial_input)
      } else {
        push(s, {
          kind: 'tool',
          tool_id: action.tool_id,
          tool_name: action.tool_name,
          input: action.partial_input ?? {},
          result: null,
          open: true,
        })
      }
      break
    }
    case 'tool_call_delta': {
      // Fragments merge best-effort; tool_call_end replaces with the
      // authoritative input.
      const block = findTool(s, action.tool_id, { open: true })
      if (block) Object.assign(block.input, action.input_delta)
      break
    }
    case 'tool_call_end': {
      const block = findTool(s, action.tool_id, { open: true }) ?? findTool(s, action.tool_id)
      if (block) {
        block.input = action.input
        block.result = action.result
        block.open = false
      }
      // The gate is resolved — any paused prompt for this tool is stale.
      if (s.paused && s.paused.tool_id === action.tool_id) s.paused = null
      break
    }
    case 'plan_proposed':
      push(s, { kind: 'plan', plan: action.plan, steps: [], decision: null })
      break
    case 'plan_step_update': {
      const last = [...s.blocks].reverse().find((b) => b.kind === 'plan')
      if (last && last.kind === 'plan') last.steps = action.steps
      break
    }
    case 'local_plan_decision': {
      // Optimistic, same-tab mark of a plan decision — acks carry no plan id,
      // so the shared state can't learn the outcome from the server.
      for (let i = s.blocks.length - 1; i >= 0; i--) {
        const b = s.blocks[i]
        if (b.kind === 'plan' && b.id === action.planId) {
          b.decision = action.decision
          break
        }
      }
      break
    }
    case 'agent_paused':
      s.paused = {
        prompt: action.prompt,
        choices: action.choices,
        tool_id: lastOpenToolId(s),
      }
      break
    case 'agent_finished':
      // A completed turn needs no marker — reloaded transcripts never had
      // one, so pushing a notice here would break live/reload parity.
      closeAssistant(s)
      closeOpenTools(s)
      s.paused = null
      break
    case 'recoverable_error':
      push(s, { kind: 'notice', message: action.message })
      break
    case 'notice':
      // Slash-command output (/help, /mode, …) broadcast by the server —
      // markdown by contract (see CommandResult).
      push(s, { kind: 'notice', message: action.message })
      break
    case 'session_info':
      // Slash commands that mutate session metadata (/mode, /plan)
      // broadcast this instead of hello: update the header labels but
      // keep the console scroll-back (hello would reset all blocks).
      s.session = action.session
      s.conversation = action.conversation
      break
    case 'fatal_error':
      closeAssistant(s)
      closeOpenTools(s)
      s.paused = null
      push(s, { kind: 'error', message: action.message })
      break
    case 'turn_cancelled':
      // Same fold line the server's persisted repair marker replays as, so
      // live and reloaded transcripts agree.
      closeAssistant(s)
      closeOpenTools(s)
      s.paused = null
      push(s, { kind: 'fold', text: FOLD_LABELS.cancelled })
      break
    case 'ack': {
      // A refused plan decision means the server had already decided (another
      // tab, or the machine moved on) — undo the optimistic mark while the
      // block is still fully pending, so it reads undecided instead of lying
      // "approved…" and re-offers.  Steps running means our decision was real.
      if (!action.accepted && ['approve_plan', 'reject_plan'].includes(action.cmd)) {
        for (let i = s.blocks.length - 1; i >= 0; i--) {
          const b = s.blocks[i]
          if (
            b.kind === 'plan'
            && b.decision !== null
            && b.steps.every(([, , st]) => st === 'pending')
          ) {
            b.decision = null
            break
          }
        }
      }
      if (
        action.accepted
        && ['approve_tool', 'deny_tool', 'approve_plan', 'reject_plan'].includes(action.cmd)
      ) {
        s.paused = null
      }
      break
    }
    case 'error':
      push(s, {
        kind: 'error',
        message: action.code ? `${action.code}: ${action.message}` : action.message,
      })
      break
    case 'pong':
      break
    case 'local_user':
      push(s, { kind: 'user', text: action.text })
      break
    case 'local_mode': {
      // Optimistic gating flip — the server's session_info broadcast
      // confirms it.  Plan is not a gating mode: it flags the next turn
      // for plan mode, so gating reads manual.  mode_label stays
      // server-owned — it reports PLAN for the whole plan lifecycle, so
      // overwriting it here would be reverted by the next broadcast.
      if (s.session) {
        s.session.permission_mode = action.mode === 'plan' ? 'manual' : action.mode
      }
      break
    }
  }
}

/**
 * Composable exposing reactive console state plus the user actions.
 * `send` is the WebSocket command transport (useWebSocket.send).
 */
export function useConsole(send: (cmd: Command) => void) {
  const state = reactive<ConsoleState>(initialState())

  function applyFrame(action: ConsoleAction) {
    Object.assign(state, reduce(toRaw(state), action))
  }

  function sendTurn(input: string) {
    const text = input.trim()
    if (!text || state.busy) return
    applyFrame({ type: 'local_user', text })
    send({ cmd: 'turn', input: text })
  }

  /** Manual early compaction — the context pill's click.  Unlike sendTurn
   *  there is no optimistic user bubble: the server diverts /compact to the
   *  slash dispatcher, which answers with a fresh hello replay (folded
   *  transcript + new usage %) followed by a notice — that replay is the
   *  transcript the compacted conversation should show. */
  function sendCompact() {
    if (state.busy) return
    send({ cmd: 'turn', input: '/compact' })
  }

  function cancelTurn() {
    send({ cmd: 'cancel' })
  }

  function approveTool() {
    const toolId = state.paused?.tool_id
    if (toolId) send({ cmd: 'approve_tool', tool_id: toolId })
  }

  function denyTool() {
    const toolId = state.paused?.tool_id
    if (toolId) send({ cmd: 'deny_tool', tool_id: toolId })
  }

  // The shared decision path for the dock ask and the inline card: mark the
  // block's decision optimistically (a refused ack undoes it) and send the
  // command.  The decision === null guard keeps double-clicks idempotent.
  function approvePlan(planId: string, mode: 'manual' | 'auto') {
    const raw = toRaw(state)
    for (let i = raw.blocks.length - 1; i >= 0; i--) {
      const b = raw.blocks[i]
      if (b.kind === 'plan' && b.plan.id === planId) {
        if (b.decision === null) {
          applyFrame({ type: 'local_plan_decision', planId: b.id, decision: mode })
        }
        break // at most one block per plan id
      }
    }
    send({ cmd: 'approve_plan', plan_id: planId, mode })
  }

  function rejectPlan(planId: string, feedback: string) {
    const raw = toRaw(state)
    for (let i = raw.blocks.length - 1; i >= 0; i--) {
      const b = raw.blocks[i]
      if (b.kind === 'plan' && b.plan.id === planId) {
        if (b.decision === null) {
          applyFrame({ type: 'local_plan_decision', planId: b.id, decision: 'rejected' })
        }
        break
      }
    }
    send({ cmd: 'reject_plan', plan_id: planId, feedback })
  }

  function setMode(mode: Mode) {
    // Optimistic — the server's session_info broadcast confirms it.
    applyFrame({ type: 'local_mode', mode })
    send({ cmd: 'set_mode', mode })
  }

  function newConversation() {
    send({ cmd: 'new_conversation' })
  }

  function switchSession(sessionId: string) {
    if (!state.session || sessionId !== state.session.id) {
      send({ cmd: 'switch_session', session_id: sessionId })
    }
  }

  return {
    state,
    applyFrame,
    sendTurn,
    sendCompact,
    cancelTurn,
    approveTool,
    denyTool,
    approvePlan,
    rejectPlan,
    setMode,
    newConversation,
    switchSession,
  }
}
