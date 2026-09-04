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
  /** True when the pill may flip gating: idle, or a turn in an execution
   *  phase.  Frozen while a plan is explored/proposed/awaiting approval —
   *  gating is pinned to manual until the plan is approved. */
  gating_editable: boolean
  context_usage_pct: number
  model: string
  cwd: string
}

/** Workflow/gating mode — the input-bar pill cycles manual → auto → plan.
 *  Plan is not a gating mode (gating drops to manual); it flags the next
 *  turn to run in plan mode, mirroring the CLI's `/mode plan`. */
export type Mode = 'manual' | 'auto' | 'plan'

/** How THIS tab decided a proposed plan.  Same-tab truth: the ack frames for
 *  approve_plan/reject_plan carry no plan id, so decisions can't wait for the
 *  server — they are marked optimistically and undone if the ack refuses. */
export type PlanDecision = 'manual' | 'auto' | 'rejected'

export interface ConversationInfo {
  id: string | null
  sequence_num: number | null
  title: string | null
}

export interface ReplayMessage {
  role: string
  content: string
  /**
   * Synthetic marker the server tagged at replay — set only on user entries
   * whose text was never typed by a human (turn-cancelled repair, compacted
   * summary).  The console renders a fold line instead of a user bubble.
   */
  fold?: 'cancelled' | 'compacted'
  /**
   * Tool-call replay — present when role === 'tool'.  Mirrors the
   * tool_call_end payload so the reducer builds the same ToolCard block;
   * result is null for a use that never executed (cancelled turn).
   */
  tool_id?: string
  tool_name?: string
  input?: Record<string, unknown>
  result?: ToolResult | null
}

/** The agent_paused frame as replayed by hello.paused. */
export interface PausedFrame {
  type: 'agent_paused'
  prompt: string
  choices: string[] | null
}

/** A plan awaiting (or already running under) approval, as replayed by
 * hello.plan — the server snapshots plan_proposed + the latest
 * plan_step_update so a reconnecting tab re-renders its PlanCard. */
export interface PlanSnapshot {
  plan: Plan
  steps: PlanStepRow[]
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
      plan: PlanSnapshot | null
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
  | { type: 'notice'; message: string }
  | { type: 'session_info'; session: SessionInfo; conversation: ConversationInfo }
  | { type: 'fatal_error'; message: string }
  | { type: 'ack'; cmd: string; accepted: boolean }
  | { type: 'turn_cancelled' }
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
  | { cmd: 'set_mode'; mode: Mode }
  | { cmd: 'new_conversation'; title?: string }
  | { cmd: 'switch_session'; session_id: string }
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
  | {
      id: number
      kind: 'plan'
      plan: Plan
      steps: PlanStepRow[]
      /** null until this tab decides (hello rebuilds and live proposals both
       *  start undecided).  The server never replays a decision. */
      decision: PlanDecision | null
    }
  | { id: number; kind: 'error'; message: string }
  | { id: number; kind: 'notice'; message: string }
  /** A synthetic model-scaffolding marker, rendered as a muted fold line
   *  (e.g. turn cancelled) — text is the human label. */
  | { id: number; kind: 'fold'; text: string }

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

/** One per-file status letter from GET /api/git/status (Python normalizes
 * XY porcelain pairs; copies collapse to R).  Letters match VS Code's
 * file decorations: U untracked, C conflict. */
export type GitStatusLetter = 'M' | 'A' | 'D' | 'R' | 'U' | 'T' | 'C'

/** Per-axis path → letter maps for the source-control panel. */
export interface GitSectionMap {
  staged: Record<string, GitStatusLetter>
  unstaged: Record<string, GitStatusLetter>
}

/** Wire payload of GET /api/git/status — only changed paths present.
 * dirs: badge for every parent directory of a changed file (the most
 * severe descendant wins, computed on the server), so explorer folders
 * signal changes even below the tree's depth limit.  sections: the same
 * paths split by axis — staged (index vs HEAD) and unstaged (worktree
 * vs index) — for the source-control panel; `??` untracked paths appear
 * only in unstaged, an `MM` file in both. */
export interface GitStatusPayload {
  branch: string | null
  files: Record<string, GitStatusLetter>
  dirs: Record<string, GitStatusLetter>
  sections: GitSectionMap
}

/** Reactive state of useGitStatus (payload + fetch lifecycle). */
export interface GitStatusState {
  branch: string | null
  files: Record<string, GitStatusLetter>
  dirs: Record<string, GitStatusLetter>
  sections: GitSectionMap
  loading: boolean
  error: string | null
}

// ---------------------------------------------------------------------------
// Git diff (GET /api/git/diff)
// ---------------------------------------------------------------------------

export type DiffLineKind = 'ctx' | 'del' | 'add'

/** One unified-diff content line; old_ln/new_ln is null on the side the
 * line does not exist (adds have no old number, deletions no new one). */
export interface DiffLine {
  kind: DiffLineKind
  old_ln: number | null
  new_ln: number | null
  text: string
  no_newline?: boolean
}

/** A `@@ -o,c +n,c @@` section: contiguous lines from both sides. */
export interface DiffHunk {
  old_start: number
  old_count: number
  new_start: number
  new_count: number
  lines: DiffLine[]
}

/** Wire payload of GET /api/git/diff.  old_path/new_path are null when
 * that side is /dev/null (added/deleted files); hunks is empty for
 * binary files or when nothing changed. */
export interface GitDiffPayload {
  path: string
  staged: boolean
  binary: boolean
  truncated: boolean
  old_path: string | null
  new_path: string | null
  hunks: DiffHunk[]
}

/** One entry in the editor tab strip: a regular file tab or a read-only
 * diff tab (side-by-side view of one file).  Type, not interface, so the
 * `kind` discriminant narrows in template/type checks. */
export type TabEntry =
  | { kind: 'file'; path: string }
  | { kind: 'diff'; path: string; staged: boolean }

// ---------------------------------------------------------------------------
// Git hunk apply (POST /api/git/hunk)
// ---------------------------------------------------------------------------

/** What a hunk action does: stage (worktree → index), unstage (index →
 * HEAD), revert (discard). */
export type HunkApplyAction = 'stage' | 'unstage' | 'revert'

/** Body of POST /api/git/hunk — the hunk is the one on screen; git
 * apply's context matching rejects a stale one (409). */
export interface HunkApplyRequest {
  path: string
  staged: boolean
  action: HunkApplyAction
  old_path: string | null
  new_path: string | null
  hunk: DiffHunk
}

// ---------------------------------------------------------------------------
// Whole-file git actions (POST /api/git/file, POST /api/git/commit)
// ---------------------------------------------------------------------------

/** What a source-control row button does: stage (worktree → index,
 * untracked files included), unstage (index → worktree), discard
 * (restore from index, or delete an untracked file). */
export type GitFileAction = 'stage' | 'unstage' | 'discard'

/** Body of POST /api/git/file. */
export interface GitFileRequest {
  path: string
  action: GitFileAction
}

/** Body of POST /api/git/commit — multi-line messages are fine. */
export interface GitCommitRequest {
  message: string
}
