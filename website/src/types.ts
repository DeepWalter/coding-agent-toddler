/**
 * Wire types for the `tod serve` WebSocket protocol and REST API.
 *
 * Field names and shapes mirror the Python serializers in
 * toddler/web/events.py, ws.py and api.py — keep them in lockstep.
 */

// ---------------------------------------------------------------------------
// hello payloads
// ---------------------------------------------------------------------------

export interface SessionInfo {
  id: string | null
  title: string | null
  mode_label: string
  permission_mode: 'manual' | 'auto'
  context_usage_pct: number
  model: string
  cwd: string
}

export interface ConversationInfo {
  id: string | null
  sequence_num: number | null
  title: string | null
}

export interface ReplayMessage {
  role: string
  content: string
}

/** The agent_paused frame as replayed by hello.paused. */
export interface PausedFrame {
  type: 'agent_paused'
  prompt: string
  choices: string[] | null
}

// ---------------------------------------------------------------------------
// Event payloads
// ---------------------------------------------------------------------------

export interface ToolResult {
  success: boolean
  output: string | null
  error: string | null
  checkpoint_id: string | null
  metadata: Record<string, unknown> | null
}

export interface PlanStep {
  id: string
  description: string
  tool_calls_expected: number
  files_affected: number
}

export interface Plan {
  id: string
  title: string
  summary: string | null
  steps: PlanStep[]
  rationale: string | null
  risks: string[] | null
  estimated_files_touched: number
}

/** plan_step_update snapshot rows: [id, description, status]. */
export type PlanStepRow = [string, string, string]

export interface TokenUsage {
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_creation_tokens: number | null
}

// ---------------------------------------------------------------------------
// Server → client frames (type strings are the snake_case event class names)
// ---------------------------------------------------------------------------

export type Frame =
  | {
      type: 'hello'
      session: SessionInfo
      conversation: ConversationInfo
      busy: boolean
      paused: PausedFrame | null
      messages: ReplayMessage[]
    }
  | { type: 'turn_started' }
  | { type: 'state'; busy: boolean }
  | { type: 'text_delta'; text: string }
  | {
      type: 'tool_call_start'
      tool_id: string
      tool_name: string
      partial_input: Record<string, unknown> | null
    }
  | { type: 'tool_call_delta'; tool_id: string; input_delta: Record<string, unknown> }
  | {
      type: 'tool_call_end'
      tool_id: string
      tool_name: string
      input: Record<string, unknown>
      result: ToolResult | null
    }
  | { type: 'plan_proposed'; plan: Plan }
  | { type: 'plan_step_update'; steps: PlanStepRow[] }
  | { type: 'agent_paused'; prompt: string; choices: string[] | null }
  | { type: 'agent_finished'; reason: string; usage: TokenUsage | null }
  | { type: 'recoverable_error'; message: string }
  | { type: 'fatal_error'; message: string }
  | { type: 'ack'; cmd: string; accepted: boolean }
  | { type: 'turn_cancelled' }
  | { type: 'conversation_switched'; conversation: ConversationInfo }
  | { type: 'pong' }
  | { type: 'error'; code: string; message: string }

// ---------------------------------------------------------------------------
// Client → server commands
// ---------------------------------------------------------------------------

export type Command =
  | { cmd: 'turn'; input: string; force_plan?: boolean }
  | { cmd: 'cancel' }
  | { cmd: 'approve_tool'; tool_id: string }
  | { cmd: 'deny_tool'; tool_id: string }
  | { cmd: 'approve_plan'; plan_id: string; mode: 'manual' | 'auto' }
  | { cmd: 'reject_plan'; plan_id: string; feedback?: string }
  | { cmd: 'set_mode'; mode: 'manual' | 'auto' }
  | { cmd: 'ping' }

// ---------------------------------------------------------------------------
// Console state (useConsole)
// ---------------------------------------------------------------------------

/** One display unit in the linear console stream. */
export type Block =
  | { id: number; kind: 'user'; text: string }
  | { id: number; kind: 'assistant'; text: string; closed: boolean }
  | {
      id: number
      kind: 'tool'
      tool_id: string
      tool_name: string
      input: Record<string, unknown>
      result: ToolResult | null
      open: boolean
    }
  | { id: number; kind: 'plan'; plan: Plan; steps: PlanStepRow[] }
  | { id: number; kind: 'error'; message: string }
  | { id: number; kind: 'notice'; message: string }

/** A pending approval. tool_id is inferred from the open tool block. */
export interface Paused {
  prompt: string
  choices: string[] | null
  tool_id: string | null
}

export interface ConsoleState {
  /** Monotonic block id counter (keeps the reducer deterministic). */
  nextId: number
  blocks: Block[]
  busy: boolean
  session: SessionInfo | null
  conversation: ConversationInfo | null
  paused: Paused | null
}

// ---------------------------------------------------------------------------
// REST API payloads
// ---------------------------------------------------------------------------

export interface SessionSummary {
  id: string
  title: string | null
  created_at: string
  updated_at: string
  message_count: number
}

/** One flat row of `/api/tree` (relative POSIX path). */
export interface TreeEntry {
  path: string
  type: 'dir' | 'file'
}

/** Nested tree node built from TreeEntry[] (children only for dirs). */
export interface TreeNode {
  name: string
  path: string
  type: 'dir' | 'file'
  children: TreeNode[] | null
}
