/**
 * The console's one status vocabulary — how a block is doing, and how it
 * ended.  Shared by the gutter mark every output block carries and by the
 * tool card's own labels, so the two can never drift.
 *
 * A status is read from the block alone, never from console-level busy
 * state: one that consulted `busy` would make the same block mean different
 * things in two tabs, and every kind already carries its own lifecycle flag.
 * The flag differs by kind — assistant blocks carry `closed`, thinking and
 * tool blocks carry `open`, plans carry a `decision`, their step rows, and
 * the `cancelled` marker a cut-short turn leaves.
 */

import type { Block } from './types'

/** running = still happening (animated spinner); ok / error / cancelled are
 *  the three ways a block settles.  One vocabulary, every block kind. */
export type BlockStatus = 'running' | 'ok' | 'error' | 'cancelled'

/** The kinds that draw no gutter mark are excluded from the parameter type
 *  rather than answered with a value nobody renders — ConsolePane's
 *  rowStatus narrows them away: user input (its box is the marker) and a
 *  todo list (its own boxes carry every step's status). */
export function blockStatus(
  block: Exclude<Block, { kind: 'user' | 'todo' }>,
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
      // A call the turn ended mid-flight — cancel or fatal error — never
      // got an end frame, and the reducer renders that as result: null.  It
      // failed with the turn that carried it: nothing was produced, and the
      // fold line above says why.
      if (!result) return 'error'
      return result.success ? 'ok' : 'error'
    }
    case 'plan': {
      const steps = block.steps.map(([, , status]) => status)
      // No producer for 'failed' today — PlanStepStatus is pending /
      // in_progress / completed.  Kept so a dead step can never leave a
      // spinner turning if a status is ever added server-side.
      if (steps.includes('failed')) return 'error'
      // The mark reads the *decision*, not the execution: the proposal is
      // one unit, and it settles the moment the user answers it —
      // approved (ok) or denied (error).  A plan running under an approval
      // keeps this ✓ for the rest of the turn; how far the steps have got
      // is the todo trail's reading, printed beside it.
      //
      // A cancelled turn overrides that: the plan cannot be answered any
      // more (the ask is gone) and cannot be resumed (the tool moves with
      // the turn), so it failed — like the tools that were running beside
      // it.
      if (block.cancelled) return 'error'
      if (block.decision === 'rejected') return 'error'
      if (block.decision !== null) return 'ok'
      // No decision in this tab: a plan whose steps are all still pending
      // is the one the dock is asking about (running); anything past
      // pending was decided from another tab or replayed by hello, and
      // settles the way an approval here would.
      return steps.some((s) => s !== 'pending') ? 'ok' : 'running'
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
