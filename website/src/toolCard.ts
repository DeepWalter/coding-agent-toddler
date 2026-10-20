import type { Block, EditSelection, TabEntry, ToolResult } from './types'
import { basename } from './utils'
import { editDiff, editDiffText, type EditDiffRow } from './diff'

type ToolBlock = Extract<Block, { kind: 'tool' }>

interface CardSpec {
  /** What the reader calls the tool — the backend's `shell` is `Bash`. */
  name: string
  /** The path this call is about: named in the title in place of the
   *  description — by its basename, since the heading is a glance and the
   *  directory is usually the same one the call beside it names — and
   *  opening in the editor on a click.  The click is offered where there
   *  is a file to open, whatever the tool: the frames say so, and no call
   *  is special-cased here. */
  path?: { param: string }
  /** No box at all: a call that has said everything it has to say in its
   *  title.  A file call is one — its result is the file, which the title
   *  already opens. */
  titleOnly?: boolean
  /** What this call was told to do, and how its row reads it: the parameter
   *  holding it, the label over it, the language its editor tab opens in,
   *  and whether the value is worth a copy button.  Absent for a call whose
   *  instruction needs no row of its own — a git call says what it did in
   *  its description, and nothing it was passed is worth reading back. */
  input?: {
    param: string
    label: string
    language: string | null
    copy?: boolean
  }
  /** The call's body is the change it makes: the line diff of these two
   *  parameters, printed in place of a result row.  A failed call keeps the
   *  diff — it is what the call tried to do — and adds its reason as the one
   *  OUT row a reader gets. */
  diff?: {
    oldParam: string
    newParam: string
  }
}

/** The tools whose call reads as a tool card, and what each of them shows.
 *  This map is the whole of the per-tool presentation switch: the name, and
 *  the shape of the box under it.  A tool missing from it still gets a card
 *  — the backend's own name over its result — just not one shaped for it. */
const CARDS = new Map<string, CardSpec>([
  // A shell call's first row is the command itself: shell script, and the
  // one thing the reader is most likely to want to run again.  It is what
  // the call was handed, so it reads as IN beside OUT.
  ['shell', {
    name: 'Bash',
    input: { param: 'command', label: 'IN', language: 'bash', copy: true },
  }],
  // A search's first row is the pattern it was given — plain text, and
  // rarely past the clip, so its row is usually just something to read.
  // Named for what it holds rather than for its place in the pair: PAT says
  // what the row is, where IN would only say where it sits.
  ['grep', {
    name: 'Grep',
    input: { param: 'pattern', label: 'PAT', language: null },
  }],
  ['glob', {
    name: 'Glob',
    input: { param: 'pattern', label: 'PAT', language: null },
  }],
  // A git call reads as its result alone: one OUT row under the title.
  // None of its parameters is the story the way a command or a pattern is —
  // a status has no arguments at all, and a commit's message is already said
  // by the call's own description.
  ['git_status', { name: 'Git status' }],
  ['git_diff', { name: 'Git diff' }],
  ['git_log', { name: 'Git log' }],
  ['git_commit', { name: 'Git commit' }],
  ['git_branch', { name: 'Git branch' }],
  // A read or a write is its title and nothing else: the path is what
  // identifies it, and the file itself opens in the editor on a click, so a
  // row of content here would only be a worse copy of the pane next door.
  // An edit is the exception: its two strings are the change itself, so the
  // card draws them as a diff rather than sending the reader to the file to
  // work out what moved.
  ['read_file', { name: 'Read', titleOnly: true, path: { param: 'file_path' } }],
  ['write_file', { name: 'Write', titleOnly: true, path: { param: 'file_path' } }],
  ['edit_file', {
    name: 'Edit',
    path: { param: 'file_path' },
    diff: { oldParam: 'old_string', newParam: 'new_string' },
  }],
])

/** Which side of the card a row is, and the key its editor tab is filed
 *  under. */
export type ToolSide = 'in' | 'out'

/** One row of the box.  The card is a list of these, which is what lets one
 *  component draw a command and a pattern, a single row or a pair: the row
 *  carries its own label, so the box holds whatever its tool's spec asks
 *  for. */
export interface ToolCardRow {
  side: ToolSide
  /** The row's label, spelled by the shared section-title rule. */
  label: string
  text: string
  /** The language for this row's editor tab; null is plain text. */
  language: string | null
  /** Offer the copy button — the command, and nothing else. */
  copy?: boolean
  /** The text is a failure, which tints it. */
  error?: boolean
}

export interface ToolCard {
  /** The reader's name for the tool. */
  name: string
  /** The call's own line about what it does; empty when it made none. */
  description: string
  /** The file this call is about, when it is about one: what the title
   *  prints in place of the description, and what it does.  `label` is the
   *  basename the title shows, `target` the name a click hands the editor
   *  (root-relative inside the root, absolute outside), and `abs` the path
   *  the title offers on hover — the file as the call named it, since that
   *  is the spelling whoever asked for it recognises.  `exists` is whether
   *  there is a file at the end of it to open: true is a title that opens,
   *  false a title that only reads, whatever the tool and whenever the
   *  frame that said so arrived.  `external` marks the outside case, whose
   *  tab opens read-only; `symlink` marks a name that reaches its file
   *  through a link, and wears `resolved` on its hover so the two spellings
   *  are not read as two files.  Null for a call that names no file. */
  path: {
    label: string
    target: string | null
    abs: string
    resolved: string
    external: boolean
    symlink: boolean
    exists: boolean
  } | null
  /** Top to bottom: what ran, then what came back — the OUT row joining
   *  the box only once it has. */
  rows: ToolCardRow[]
  /** The change an edit makes, drawn above the rows; null for every other
   *  tool and for an edit with nothing to change. */
  diff: EditDiffRow[] | null
}

/** Read a parameter the model wrote.  Parameters are `unknown` because they
 *  arrive as JSON, and a replayed call may carry none of them. */
function param(block: ToolBlock, key: string): string {
  const value = block.input[key]
  return typeof value === 'string' ? value : ''
}

/** What the OUT row reads, for a call that has a result.
 *
 *  There is no placeholder for the wait: the row is only built when
 *  something came back (below), so this never speaks for a call still
 *  running or one the turn abandoned.  A failure reads its reason where the
 *  tool left one, and says only that it failed when it left none. */
function outputText(result: ToolResult): string {
  if (!result.success) return result.error || result.output || 'tool failed'
  return result.output || '(no output)'
}

/** The card a call reads as: its title, and the rows under it. */
export function toolCard(block: ToolBlock): ToolCard {
  const spec = CARDS.get(block.tool_name)
  const rows: ToolCardRow[] = []
  // The first row is what the call was told to do, where the tool has one
  // worth a row; the second is what came back, where the call has anything
  // to say beyond its title.  A search's other parameters stay unsaid — the
  // call's own description is what names the path and the filter when they
  // matter, and the pattern is the part the reader came to check.
  if (spec?.input) {
    rows.push({
      side: 'in',
      label: spec.input.label,
      text: param(block, spec.input.param),
      language: spec.input.language,
      copy: spec.input.copy,
    })
  }
  // An edit's body is its change: the two strings diffed, drawn even while
  // the call is still waiting at the gate — the input arrives complete in
  // the start frame, and the diff is what the reader is being asked to
  // approve.  A failed call keeps it too, with the reason below.
  let diff: EditDiffRow[] | null = null
  if (spec?.diff) {
    const oldText = param(block, spec.diff.oldParam)
    const newText = param(block, spec.diff.newParam)
    if (oldText || newText) diff = editDiff(oldText, newText)
  }
  // The result row is the result, so it appears with one: a call still
  // running holds what it was told to do and nothing else — the gutter's
  // spinner is what says it is live — and a call the turn abandoned never
  // produced a result for a row to hold.  Its ✗ and the fold line above
  // already say how that ended.  A diff tool's success is already the diff,
  // so its OUT row is the failure reason alone.
  const result = block.result
  if (spec?.diff) {
    if (result && !result.success) {
      rows.push({
        side: 'out',
        label: 'OUT',
        text: outputText(result),
        language: null,
        error: true,
      })
    }
  } else if (!spec?.titleOnly && result) {
    rows.push({
      side: 'out',
      label: 'OUT',
      text: outputText(result),
      language: null,
      error: !result.success,
    })
  }
  const named = spec?.path ? param(block, spec.path.param) : ''
  // The name a click hands the editor.  A file inside the root has a
  // root-relative name — the one every pane names a file by — and the
  // server resolves nothing else to one, so the absolute fallback fires
  // only outside the root, where it opens read-only.
  const target = block.path ?? block.path_abs
  return {
    name: spec?.name ?? block.tool_name,
    description: param(block, 'description'),
    // A path the model left out is no path: the title falls back to the
    // call's own description rather than showing an empty link.
    //
    // The spellings come from the server, which is the only side that can
    // resolve what the call named: `~` needs the home directory, and a
    // relative path starts at its cwd.  `path_named` is the readable one —
    // the file under the name the call used, which is what the hover shows,
    // where a reader checks a file they may not be able to open — and
    // `path_abs` the resolved one, the fallback for a frame from a server
    // too old to send it.  The title prints the basename either way.  It
    // arrives with the execution `tool_call_start` frame, before the tool
    // runs and before any permission gate, so a card in flight already
    // names its file (and wears the external marker); the provider-phase
    // skeleton has no input at all yet, so its title is the bare tool name.
    //
    // Whether that name is a control is the server's answer too, and the
    // only question here it cannot be asked twice: there is a file at the
    // end of the path, or there is not.  A write creating one is the case
    // that moves — its own result frame is what makes the file exist — and
    // a frame from a server too old to say falls back to plain text, which
    // still hovers the path rather than opening a tab onto a 404.
    path: spec?.path && named
      ? {
          abs: block.path_named ?? block.path_abs ?? named,
          label: basename(block.path_named ?? block.path_abs ?? named),
          resolved: block.path_abs ?? named,
          target,
          external: block.path === null && block.path_abs !== null,
          symlink: block.path_symlink ?? false,
          exists: target !== null && block.path_is_file,
        }
      : null,
    rows,
    diff,
  }
}

/** A recorded 1-based line or 0-based column, when the tool sent one. */
function recordedPosition(value: unknown, min: number): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= min
    ? value
    : null
}

/** The text a click on the card's file title should reveal: the replacement
 *  once the call has landed, and the text it replaced while the call is
 *  pending, denied, or failed — the file is still the old one then.  Null for
 *  a tool with no diff body, and for an empty replacement (a deletion leaves
 *  no new text to select).
 *
 *  A successful edit's result also records where the replacement starts.
 *  That position is what picks the edited occurrence when new_string already
 *  appears elsewhere in the file; without it the editor falls back to the
 *  first text match. */
export function editSelection(block: ToolBlock): EditSelection | null {
  const spec = CARDS.get(block.tool_name)
  if (!spec?.diff) return null
  const succeeded = block.result?.success === true
  const param = succeeded ? spec.diff.newParam : spec.diff.oldParam
  const value = block.input[param]
  if (typeof value !== 'string' || value.length === 0) return null
  const meta = succeeded ? block.result?.metadata : null
  const line = recordedPosition(meta?.line_start, 1)
  const column = recordedPosition(meta?.column_start, 0)
  const recorded = line !== null && column !== null
  return {
    needle: value,
    line: recorded ? line : null,
    column: recorded ? column : null,
  }
}

/** The editor tab that shows a row in full.
 *
 *  Keyed by the block's id — monotonic per page load, so two clicks on the
 *  same row land on one tab and two different calls never collide. */
export function toolTab(
  block: ToolBlock,
  card: ToolCard,
  row: ToolCardRow,
): Extract<TabEntry, { kind: 'text' }> {
  // A tab names its side only when the card has two of them: `Bash` and
  // `Bash output` — `Grep` and `Grep output` — have to tell each other apart
  // in the strip, where a card with a single row has nothing to be confused
  // with.
  const name =
    card.rows.length > 1
      ? `${card.name}${row.side === 'out' ? ' output' : ''}`
      : card.name
  return {
    kind: 'text',
    id: `tool:${block.id}:${row.side}`,
    title: card.description ? `${name} — ${card.description}` : name,
    language: row.language,
    content: row.text,
  }
}

/** The editor tab a clipped edit diff opens: the whole change, read by the
 *  editor's diff grammar (`-`/`+` at the line head), which is what colors it.
 *  Keyed by the block's id and a side no row uses, so it never collides with
 *  the failure row's tab. */
export function toolDiffTab(
  block: ToolBlock,
  card: ToolCard,
): Extract<TabEntry, { kind: 'text' }> | null {
  if (!card.diff) return null
  const name = 'Edit diff'
  return {
    kind: 'text',
    id: `tool:${block.id}:diff`,
    title: card.path ? `${name} — ${card.path.label}` : name,
    language: 'diff',
    content: editDiffText(card.diff),
  }
}
