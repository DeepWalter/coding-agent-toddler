import { blockStatus } from './blockStatus'
import type { Block, TabEntry, ToolResult } from './types'
import { basename } from './utils'

type ToolBlock = Extract<Block, { kind: 'tool' }>

interface CardSpec {
  /** What the reader calls the tool — the backend's `shell` is `Bash`. */
  name: string
  /** The path this call is about: named in the title in place of the
   *  description — by its basename, since the heading is a glance and the
   *  directory is usually the same one the call beside it names — and
   *  opening in the editor on a click.
   *
   *  `written` marks the calls that bring the file into being: a read or an
   *  edit names a path that is already there, whatever the call itself did,
   *  where a write has nothing to open until it has succeeded. */
  path?: { param: string; written?: boolean }
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
  // A file call is its title and nothing else: the path is what identifies
  // it, and the file itself opens in the editor on a click, so a row of
  // content here would only be a worse copy of the pane next door.
  ['read_file', { name: 'Read', titleOnly: true, path: { param: 'file_path' } }],
  ['write_file', {
    name: 'Write',
    titleOnly: true,
    path: { param: 'file_path', written: true },
  }],
  ['edit_file', { name: 'Edit', titleOnly: true, path: { param: 'file_path' } }],
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
   *  prints in place of the description, and what it does.  Three spellings
   *  of the same file, because a card does three things with it — `label`
   *  is the basename the title shows, `rel` the root-relative path a click
   *  hands the editor, and `abs` where it actually is, which the title
   *  offers on hover.  Null for a call that names no file. */
  path: {
    label: string
    rel: string | null
    abs: string
    opens: boolean
  } | null
  /** Top to bottom: what ran, then what came back — the OUT row joining
   *  the box only once it has. */
  rows: ToolCardRow[]
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
  // The result row is the result, so it appears with one: a call still
  // running holds what it was told to do and nothing else — the gutter's
  // spinner is what says it is live — and a call the turn abandoned never
  // produced a result for a row to hold.  Its ✗ and the fold line above
  // already say how that ended.
  const result = block.result
  if (!spec?.titleOnly && result) {
    rows.push({
      side: 'out',
      label: 'OUT',
      text: outputText(result),
      language: null,
      error: !result.success,
    })
  }
  const named = spec?.path ? param(block, spec.path.param) : ''
  return {
    name: spec?.name ?? block.tool_name,
    description: param(block, 'description'),
    // A path the model left out is no path: the title falls back to the
    // call's own description rather than showing an empty link.
    //
    // Both spellings come from the server, which is the only side that can
    // resolve what the call named: `~` needs the home directory, and a
    // relative path starts at its cwd.  What it resolved is a whole path —
    // the hover names it, which is where a reader checks a file they cannot
    // open — but the title prints the basename and the click hands over the
    // root-relative form, the one every pane names a file by.  A call whose
    // file lies outside the root has no such form, and no tab here to open
    // in: it reads as the failed write does.  Until the call ends nothing
    // has resolved, so the title names the file as the call spelled it and
    // offers no click.
    path: spec?.path && named
      ? {
          abs: block.path_abs ?? named,
          label: basename(block.path_abs ?? named),
          rel: block.path,
          opens: block.path !== null
            && (!spec.path.written || blockStatus(block) === 'ok'),
        }
      : null,
    rows,
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
