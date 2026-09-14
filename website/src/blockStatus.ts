/**
 * The console's one status vocabulary — how a block is doing, and how it
 * ended.  Shared by the gutter mark every output block carries and by the
 * tool card's own labels, so the two can never drift.
 *
 * A status is read from the block alone, never from console-level busy
 * state: one that consulted `busy` would make the same block mean different
 * things in two tabs, and every kind already carries its own lifecycle flag.
 * The flag differs by kind — assistant blocks carry `closed`, thinking and
 * tool blocks carry `open`, plans carry a `decision` and their step rows.
 */

import type { Block } from './types'

/** running = still happening (animated spinner); ok / error / cancelled are
 *  the three ways a block settles.  One vocabulary, every block kind. */
export type BlockStatus = 'running' | 'ok' | 'error' | 'cancelled'

/** User input is the one kind with no mark, so it is excluded from the
 *  parameter type rather than answered with a value nobody renders —
 *  ConsolePane's rowStatus narrows it away. */
export function blockStatus(
  block: Exclude<Block, { kind: 'user' }>,
): BlockStatus {
  switch (block.kind) {
    // A replayed block is always closed, so a reload reads settled.
    case 'assistant':
      return block.closed ? 'ok' : 'running'
    // A thought closes by five routes — the answer starting, a tool call,
    // the turn ending, a fatal error, a cancel — and the two unhappy ones
    // leave their own error or fold line right below it: the thought itself
    // finished saying what it had to say.
    case 'thinking':
      return block.open ? 'running' : 'ok'
    case 'tool': {
      if (block.open) return 'running'
      const result = block.result
      // A call the turn ended mid-flight never got an end frame; the
      // reducer renders that as result: null.
      if (!result) return 'cancelled'
      return result.success ? 'ok' : 'error'
    }
    case 'plan': {
      // Rejected is the one terminal decision the user can make — and the
      // card's own note already reads "plan rejected".
      if (block.decision === 'rejected') return 'cancelled'
      const steps = block.steps.map(([, , status]) => status)
      // No producer for 'failed' today — PlanStepStatus is pending /
      // in_progress / completed, and an approved-then-abandoned plan
      // settles by rejection instead.  Kept so a dead step can never leave
      // a spinner turning if a status is ever added server-side.
      if (steps.includes('failed')) return 'error'
      if (steps.length > 0 && steps.every((s) => s === 'completed')) {
        return 'ok'
      }
      // Undecided while the user reads it, approved with no step update
      // yet, or mid-step: in flight either way.
      return 'running'
    }
    case 'error':
      return 'error'
    // Slash-command output: the command completed, and the notice is its
    // own success report.
    case 'notice':
      return 'ok'
    // A synthetic marker (a cancelled turn, a compaction).  It stands for
    // "this turn produced nothing", so it reads settled and empty rather
    // than succeeded; its italic label carries the actual words.
    case 'fold':
      return 'cancelled'
  }
}
