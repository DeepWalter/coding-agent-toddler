import { blockStatus } from './blockStatus'
import type { Block, TabEntry } from './types'

/** The name a shell call is shown under.  The tool is `shell`; `Bash` is
 *  what the reader calls it. */
export const SHELL_CARD_NAME = 'Bash'

/** Which side of the card a reader opened in the editor. */
export type ShellSide = 'in' | 'out'

type ShellBlock = Extract<Block, { kind: 'tool' }>

/** Whether this block is a shell call rather than any other tool's. */
export function isShellCall(block: ShellBlock): boolean {
  return block.tool_name === 'shell'
}

/** Read a parameter the model wrote.  Parameters are `unknown` because they
 *  arrive as JSON, and a replayed call may carry none of them. */
function param(block: ShellBlock, key: string): string {
  const value = block.input[key]
  return typeof value === 'string' ? value : ''
}

/** The call's own line about what it does — absent on calls made before the
 *  parameter existed, which the title then simply omits. */
export function shellDescription(block: ShellBlock): string {
  return param(block, 'description')
}

/** The command, as the model wrote it. */
export function shellCommandText(block: ShellBlock): string {
  return param(block, 'command')
}

/** What the row under the command reads.
 *
 *  Read through the block's status rather than `result.success`: a turn
 *  cancelled before the call ran leaves `result` null, and a call still
 *  running has none at all.  Every state but `ok` and `error` is a single
 *  line, so those never clip and never open. */
export function shellOutputText(block: ShellBlock): string {
  switch (blockStatus(block)) {
    case 'running':
      return 'waiting for result…'
    case 'cancelled':
      return 'cancelled before execution'
    case 'error':
      return block.result?.error || block.result?.output || 'tool failed'
    default:
      return block.result?.output || '(no output)'
  }
}

/** Whether the output row shows a failure, which tints the card. */
export function shellFailed(block: ShellBlock): boolean {
  return blockStatus(block) === 'error'
}

/** The editor tab that shows this side in full.
 *
 *  Keyed by the block's id — monotonic per page load, so two clicks on the
 *  same row land on one tab and two different calls never collide. */
export function shellTextTab(block: ShellBlock, side: ShellSide): Extract<
  TabEntry,
  { kind: 'text' }
> {
  const description = shellDescription(block)
  // The two sides are separate tabs, so their names have to tell them apart
  // — the card's own IN/OUT vocabulary, spelled out for the strip.
  const name = side === 'in' ? SHELL_CARD_NAME : `${SHELL_CARD_NAME} output`
  return {
    kind: 'text',
    id: `tool:${block.id}:${side}`,
    title: description ? `${name} — ${description}` : name,
    // The command is shell script; the output is just text.
    language: side === 'in' ? 'bash' : null,
    content: side === 'in' ? shellCommandText(block) : shellOutputText(block),
  }
}
