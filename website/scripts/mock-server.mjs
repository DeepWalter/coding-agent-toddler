// Minimal toddler-protocol mock for headless UI tests: serves website/dist
// statically and speaks enough of the /ws protocol to drive a tool gate
// (hello → turn → stream → agent_paused).  Port 8100 — never collides with
// the real `tod serve` (:8000) or the vite dev server (:5173).
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { WebSocketServer } from 'ws'

const DIST = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'dist')
const PORT = 8100

// Context usage % the mock reports.  MOCK_CONTEXT_PCT lets a run start past
// the context pill's clickability gate; set_context and /compact move the
// number mid-run (the real server broadcasts session_info on every state
// change, and a fresh hello after a conversation-changing slash command).
const COMPACT_TO_PCT = Number(process.env.MOCK_COMPACT_TO_PCT ?? 34)

// The shell card's fixture: a command and an output both longer than the
// card's three-line clip, so the fade, the copy button and the click-through
// to the editor all have something to act on.  The tool is `shell` (what the
// backend calls it); the card renders it as `Bash`.
const SHELL_DESCRIPTION = 'count the Python files per directory'
const SHELL_COMMAND = [
  'for dir in toddler tests; do',
  '  echo "== $dir"',
  "  find \"$dir\" -name '*.py' -type f | wc -l",
  'done',
].join('\n')
const SHELL_OUTPUT = Array.from(
  { length: 10 },
  (_, i) => `line ${i + 1} of the listing`,
).join('\n') + '\n'

// The search card's fixture: a search call has no IN row — the pattern is a
// parameter, and the matches are the whole of the output — so the card is a
// title and one OUT row.  Twelve matches is past the card's three-line clip,
// which is what the fade, the clamped cursor and the click-through to the
// editor all key on.  The tool is `grep` (what the backend calls it); the card
// renders it as `Grep`.
const GREP_DESCRIPTION = 'find every place the search root is resolved'
const GREP_PATTERN = 'search_path'
const GREP_OUTPUT = Array.from(
  { length: 12 },
  (_, i) => `toddler/tools/search.py:${87 + i}:    search_path = Path(path).expanduser().resolve()`,
).join('\n') + '\n'
const GREP_MATCH_COUNT = 12

// A glob call whose result fits its three lines — the row that stays plain
// text: no fade, no cursor, nothing to open.  Two entries, and no trailing
// newline, so what the row measures is exactly what it shows.
const GLOB_DESCRIPTION = 'list the search tools'
const GLOB_PATTERN = 'tools/*.py'
const GLOB_OUTPUT = ['tools/base.py', 'tools/executor.py'].join('\n')

// The git cards' fixture: a git call reads as one OUT row under its title,
// with no row for what it was passed.  The status is past the three-line clip
// (the fade, the clamped cursor and the click-through all need that) and the
// log sits inside it, which is the other reading a row can have.  The tools
// are `git_status` and `git_log`; the cards name them `Git status` and
// `Git log`.
const GIT_STATUS_DESCRIPTION = 'check the working tree before committing'
const GIT_STATUS_OUTPUT = [
  ' M toddler/tools/git.py',
  ' M toddler/tools/search.py',
  ' M website/src/toolCard.ts',
  ' M website/src/components/ConsolePane.vue',
  ' M website/scripts/mock-server.mjs',
  '?? website/src/components/ConsolePaneToolCard.vue',
  '?? website/src/components/ConsolePaneToolRow.vue',
  '?? website/src/assets/styles/components/console-pane-tool-card.css',
].join('\n') + '\n'
const GIT_LOG_DESCRIPTION = 'list the last few commits'
// One line per commit and short ones at that: a row that fits its three lines
// is the other reading the card has to make, and the default long format
// wraps past the clip even in two entries.
const GIT_LOG_OUTPUT = [
  '62847ce fix(tools): cap git output',
  '3aa5c7c fix(tools): cap grep results',
].join('\n')

// The file cards' fixture: a file call is its title alone — the path, which
// opens that file in the editor — so the run covers all three names, two
// paths that are there to open (the seeded files the editor phase already
// serves), and one write that failed, whose path therefore has nothing
// behind it.  The paths are absolute because the tools' schema asks for
// absolute ones.
const READ_PATH = '/tmp/mock-a.md'
const READ_DESCRIPTION = 'read the seeded file'
const READ_OUTPUT = ['1\t# mock A', '2\t', '3\tfirst seeded file'].join('\n')
const EDIT_DESCRIPTION = 'tweak the heading'
const WRITE_PATH = '/tmp/mock-b.md'
const WRITE_DESCRIPTION = 'write the second seeded file'
const FAILED_WRITE_PATH = '/tmp/read-only/mock-c.md'
const FAILED_WRITE_DESCRIPTION = 'write a file that cannot be written'
const OVERWRITE_DESCRIPTION = 'rewrite the second seeded file'
// A path no file stands at, for the call whose title has nothing to open —
// the rule read from the side that is not a write.
const MISSING_PATH = '/tmp/mock-gone.md'
const MISSING_DESCRIPTION = 'read a file that is not there'
// A link standing in front of it, which is what macOS has: `/home` is a
// symlink to `/System/Volumes/Data/home`, so a call naming the short
// spelling and the file it opens are two different strings for one file.
// The fixtures reach the outside file through this one, so the card has to
// show the name the call used while the editor opens the file it resolved
// to.
const MOCK_HOME_LINK = '/tmp/mock-home'
function unlinked(path) {
  return path.startsWith(`${MOCK_HOME_LINK}/`)
    ? `${MOCK_HOME}/${path.slice(MOCK_HOME_LINK.length + 1)}`
    : path
}

// A file the repository does not hold, reached through the link above.  The
// tools take the model's `~` paths literally, so this is the shape a call
// names whenever it reaches outside the project — and the editor opens it
// read-only, under the absolute path the card resolves it to, while the card
// itself shows the name the call used.
const OUTSIDE_PATH = `${MOCK_HOME_LINK}/.toddler/notes.md`
const OUTSIDE_DESCRIPTION = 'read a file outside the repository'

// The gate's card: the title every call is asked about, and — for a shell
// call — the command as its own content block, long enough that a script
// overflows the six lines the card shows before scrolling.  The other two
// gates carry no content: their call is a file, and the card's file line is
// the whole of what there is to read.
const GATE_TITLE = 'Allow this tool call?'
const SCRIPT_COMMAND = [
  'set -e',
  'for dir in toddler tests website; do',
  '  echo "== $dir"',
  '  find "$dir" -name "*.py" -type f \\',
  '    | while read -r f; do',
  '        echo "  $f"',
  '      done',
  'done',
  "echo '---'",
  'wc -l toddler/tools/base.py',
  'wc -l toddler/agent/loop.py',
  'wc -l website/src/types.ts',
].join('\n')
const SCRIPT_DESCRIPTION = 'count the lines of the gate and its neighbours'

// The plan fixture: a `plan` turn proposes two steps and holds for the
// decision; approving streams one plan_step_update per beat — the trail the
// todo blocks print.  `markdown` is the server's
// Plan.format_for_display() output; the mock spells the shape out rather
// than sharing a formatter with Python, and the harness only checks that
// the document renders it.
const PLAN_ID = 'plan-mock-1'
const PLAN_TITLE = 'Split the search root out of the tool'
const PLAN_SUMMARY = 'Move the resolver out of search.py so both tools share it.'
const PLAN_RATIONALE = 'Reading before moving keeps the diff small.'
const PLAN_RISKS = ['The import graph may move with it']
const PLAN_STEPS = [
  {
    id: 'step-1',
    description: 'Read search.py and find the resolver',
    tool_calls_expected: ['read_file'],
    files_affected: ['toddler/tools/search.py'],
  },
  {
    id: 'step-2',
    description: 'Move the resolver into its own module',
    tool_calls_expected: ['write_file'],
    files_affected: ['toddler/tools/paths.py'],
  },
]
const PLAN_MARKDOWN = [
  `## Plan: ${PLAN_TITLE}`,
  '',
  PLAN_SUMMARY,
  '',
  `**Rationale**: ${PLAN_RATIONALE}`,
  '',
  '**Risks**:',
  ...PLAN_RISKS.map((risk) => `- ${risk}`),
  '',
  `**Steps** (${PLAN_STEPS.length}):`,
  ...PLAN_STEPS.map(
    (step, i) => `${i + 1}. **${step.description}** [${step.files_affected.join(', ')}]`,
  ),
  '',
  'Estimated files touched: 2',
].join('\n')
const PLAN_FRAME = {
  id: PLAN_ID,
  title: PLAN_TITLE,
  summary: PLAN_SUMMARY,
  steps: PLAN_STEPS,
  rationale: PLAN_RATIONALE,
  risks: PLAN_RISKS,
  estimated_files_touched: 2,
  markdown: PLAN_MARKDOWN,
}
// The real server mints a fresh plan id per proposal; the mock counts, so a
// second proposal (after a rejection) is its own plan — a new doc tab, its
// own decision.
let planSeq = 0
// The file turn's last call — the outside read — holds at a gate, so a test
// can watch the card name the file, and wear the external marker, before
// the call is allowed to run.  The pending call waits here for the
// approve/deny handler to finish it.
const FILE_GATE_ID = 't12'
// The overwrite turn's write holds at a gate of its own, for the same
// reason from the other side: the card's title is a control over a file that
// is already there, and the only way to see it before the call has run is to
// stop the call.
const OVERWRITE_GATE_ID = 't14'
let pendingFileCall = null
// The shell gate's call, kept for the same reason: approve/deny finishes
// whichever command the turn held, the plain one or the script.
let pendingShellCall = { description: 'list the files', command: 'ls -la' }
// One beat per plan_update call: the prose that call carried, then the whole
// step list as it stood afterwards.
const PLAN_BEATS = [
  ['reading the resolver\n', ['in_progress', 'pending']],
  ['resolver found — moving it out\n', ['completed', 'pending']],
  ['writing the new module\n', ['completed', 'in_progress']],
  ['done\n', ['completed', 'completed']],
]

const TYPES = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
  '.woff2': 'font/woff2',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.map': 'application/json',
}

// The repo the session runs against.  The agent's own file tools write
// absolute paths, so the fixture paths below are absolute too and the file
// handler resolves them against this root.
const REPO_ROOT = '/tmp'

const API = {
  '/api/meta': { repo_root: REPO_ROOT, model: 'mock-model', dev: false },
  '/api/sessions': { sessions: [] },
  '/api/tree': { root: '/tmp', entries: [] },
  '/api/git/status': {
    branch: null,
    files: {},
    dirs: {},
    sections: { staged: {}, unstaged: {} },
  },
}

// Files the editor phase opens, served with the same payload (and 404) as
// toddler/web/files.read_file.  The tree stays empty: the phase restores its
// tabs from localStorage, so the explorer needs no entries to drive them.
const FILES = new Map([
  ['mock-a.md', '# mock A\n\nfirst seeded file\n'],
  ['mock-b.md', '# mock B\n\nsecond seeded file\n'],
  // The external fixture: keyed by its resolved absolute path, which is what
  // the editor asks for once a card falls back to `path_abs`.
  ['/home/mock/.toddler/notes.md', '# notes\noutside the repo\n'],
])

// The home directory a `~` path expands against.  The real server asks the
// machine; the mock names one, so the fixture can show where a file outside
// the project actually lives.
const MOCK_HOME = '/home/mock'

// toddler/web/files.tool_path_abs: where a call's `file_path` actually is.
function absolutePath(raw) {
  if (typeof raw !== 'string' || !raw.trim()) return null
  if (raw.startsWith('~/')) return `${MOCK_HOME}/${raw.slice(2)}`
  return unlinked(raw.startsWith('/') ? raw : `${REPO_ROOT}/${raw}`)
}

// toddler/web/files.tool_path_named: the same file under the name the call
// used — expanded and absolute, links left alone.
function namedPath(raw) {
  if (typeof raw !== 'string' || !raw.trim()) return null
  if (raw.startsWith('~/')) return `${MOCK_HOME}/${raw.slice(2)}`
  return raw.startsWith('/') ? raw : `${REPO_ROOT}/${raw}`
}

// toddler/web/files.tool_path: the same file for a tab to hold — null when it
// lands outside the root, which is the case the outside fixture draws.
function rootRelative(raw) {
  const abs = absolutePath(raw)
  if (abs === null || !abs.startsWith(`${REPO_ROOT}/`)) return null
  return abs.slice(REPO_ROOT.length + 1)
}

// toddler/web/files.tool_path_is_file: whether there is a file behind the
// path a call named, which is what decides if the card's title is a control.
// Keyed the way /api/file keys FILES — repo-relative inside the root, the
// absolute spelling outside it — so the mock answers what the editor would.
function fileExists(raw) {
  const abs = absolutePath(raw)
  if (abs === null) return false
  const key = abs.startsWith(`${REPO_ROOT}/`) ? abs.slice(REPO_ROOT.length + 1) : abs
  return FILES.has(key)
}

// The file a call is about, in terms of everything a card's title reads it
// by.  One builder for the start frame, the end frame and the transcript
// entry, so the three can never drift.  A call naming no file gets the
// Nones (and the false) those resolvers answer for a non-string.
function cardPaths(raw) {
  return {
    path: rootRelative(raw),
    path_abs: absolutePath(raw),
    path_named: namedPath(raw),
    path_symlink: namedPath(raw) !== absolutePath(raw),
    path_is_file: fileExists(raw),
  }
}

function filePayload(rel, content) {
  // Same line count as read_file: newlines, plus one for a trailing line
  // that has no newline of its own.
  const newlines = (content.match(/\n/g) ?? []).length
  return { path: rel, content, total_lines: newlines + (content.endsWith('\n') ? 0 : 1) }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://${req.headers.host}`)
  if (url.pathname === '/api/file') {
    // toddler/web/files.read_file resolves any spelling — root-relative or
    // absolute, in or out of the root — so an in-root absolute path names the
    // same file as its relative form, and an external absolute path names the
    // outside fixture directly.
    const asked = url.searchParams.get('path') ?? ''
    const rel = asked.startsWith(`${REPO_ROOT}/`)
      ? asked.slice(REPO_ROOT.length + 1)
      : asked
    if (req.method === 'PUT') {
      // The write side stays root-scoped, exactly as the real endpoint does.
      // Every PUT is recorded on the mutations backdoor either way, so a test
      // can prove that a read-only tab never tried to save.
      mutations.push({ cmd: 'file_write', path: asked })
      req.resume() // drain the body — the mock has no use for it
      if (asked.startsWith('/') && !asked.startsWith(`${REPO_ROOT}/`)) {
        res.writeHead(400, { 'content-type': 'application/json' })
        res.end(JSON.stringify(
          { error: `path escapes the repository root: '${asked}'` },
        ))
        return
      }
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({
        ok: true,
        bytes: Number(req.headers['content-length'] ?? 0),
      }))
      return
    }
    const content = FILES.get(rel)
    const missing = content === undefined
    res.writeHead(missing ? 404 : 200, { 'content-type': 'application/json' })
    res.end(JSON.stringify(
      missing ? { error: `file not found: '${rel}'` } : filePayload(rel, content),
    ))
    return
  }
  if (url.pathname.startsWith('/api/')) {
    const body = API[url.pathname] ?? {}
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify(body))
    return
  }
  let file = url.pathname === '/' ? '/index.html' : url.pathname
  const full = path.join(DIST, file)
  try {
    const body = await readFile(full)
    res.writeHead(200, { 'content-type': TYPES[path.extname(full)] ?? 'application/octet-stream' })
    res.end(body)
  } catch {
    res.writeHead(404)
    res.end('not found')
  }
})

const session = {
  id: 'mock',
  title: 'mock session',
  mode_label: 'manual',
  permission_mode: 'manual',
  gating_editable: true,
  context_usage_pct: Number(process.env.MOCK_CONTEXT_PCT ?? 12),
  model: 'mock-model',
  // The slot the conversation's model was picked by — the row the picker
  // highlights.  Distinct from `model`: `flash` names the same spec as
  // `default`, so only this tells the two rows apart.
  model_slot: 'default',
  effort: 'high',
  // The real server ships all three slots on the same id; this mock splits
  // them so the picker's window column is observable and so a pick between
  // `default` and `flash` is a pure slot change — same spec, different row.
  model_slots: [
    { name: 'default', spec: 'mock-model', context_tokens: 200000 },
    { name: 'pro', spec: 'mock-model[1m]', context_tokens: 1000000 },
    { name: 'flash', spec: 'mock-model', context_tokens: 200000 },
  ],
  cwd: '/tmp',
}

// What the picker has asked the server to change — read back over the
// socket so a test can assert that a drag commits exactly once, however
// many stops it crossed.  (The real server has no such read; this is the
// test-only backdoor shape set_context already uses.)
const mutations = []

// The session_info a `set_effort` would have broadcast, parked by the
// hold_effort_echo backdoor: the real server's round trip is short enough
// to hide a client that only shows the picked tier once its echo lands, so
// a test holds the echo to look at what the client shows on its own.
let holdingEffortEcho = false
let heldEffortEcho = null

// The tiers toddler/config/defaults.py accepts — mirrored so a bad tier
// fails here the way the real server fails it.
const EFFORT_TIERS = [
  'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra',
]

// Reasoning bodies the thought-block assertions compare against — keep in
// sync with ui-test.mjs (SEED_REASONING / THINK_REASONING).
const SEED_REASONING =
  'seed thought: check `scrollable` before answering\n' +
  '```python\n' +
  'def fits(pane):\n' +
  '    return pane.scrollHeight > pane.clientHeight\n' +
  '```\n' +
  'second line — prose stays verbatim, fences do not'
const THINK_FRAGMENTS = [
  'live thought: stream the reasoning ',
  'before the answer — accumulated ',
  'fragment by fragment, kept verbatim',
]
const THINK_REASONING = THINK_FRAGMENTS.join('')

// How long a think turn holds between its reasoning and its answer, so the
// harness can observe the card mid-stream ("Thinking…", above its bubble).
const THINK_HOLD_MS = Number(process.env.MOCK_THINK_HOLD_MS ?? 600)

// Seed a tall console so the pane can scroll far before the gate pops.
// The first entry carries a reasoning block — the replayed thinking card.
function seedMessages() {
  const messages = []
  for (let i = 0; i < 14; i++) {
    const lines = []
    for (let j = 0; j < 22; j++) lines.push(`seed line ${i}.${j} — filler to make the console scrollable`)
    const entry = { role: 'assistant', content: lines.join('\n') }
    if (i === 0) entry.reasoning = SEED_REASONING
    messages.push(entry)
  }
  return messages
}

// The persisted transcript every hello replays: the seed history, plus what
// the live turns below append — assistant messages (a completed think turn,
// so a reload shows the same thinking card the live stream built) and the
// fold-tagged turn-cancelled repair markers serialize_transcript emits.
// Swapping conversations empties it, the way the real server replays a
// fresh conversation's transcript.
let transcript = seedMessages()

// The active conversation, mirrored into the hello / session_info payloads.
// The server auto-titles a conversation from the first user input of its
// first turn, and /clear starts a new, untitled one.
let conversation = { id: 'conv-1', sequence_num: 1, title: 'seed conversation' }

// The server's MAX_TITLE_LENGTH (toddler/session/models.py): the auto-title
// cuts a first user input to it, and a rename is clamped to it.
const MAX_TITLE_LENGTH = 80

// The real server broadcasts session_info, notices and post-slash hello
// replays to every subscribed client (only the connect-time hello is
// per-client) — the mock mirrors that so a second connection can script
// state (set_context) into the page under test.
const clients = new Set()

function helloFrame() {
  return {
    type: 'hello',
    session,
    conversation,
    busy: false,
    paused: null,
    plan: null,
    messages: [...transcript],
  }
}

function sessionInfoFrame() {
  return { type: 'session_info', session: { ...session }, conversation }
}

const wss = new WebSocketServer({ server, path: '/ws' })

wss.on('connection', (ws) => {
  clients.add(ws)
  ws.on('close', () => clients.delete(ws))
  // Direct replies (acks, this connection's hello) go only to this socket;
  // state that every tab must see goes through broadcast().
  const send = (frame) => ws.send(JSON.stringify(frame))
  const broadcast = (frame) => {
    const body = JSON.stringify(frame)
    for (const client of clients) {
      if (client.readyState === 1) client.send(body)
    }
  }
  send(helloFrame())

  ws.on('message', async (raw) => {
    let msg
    try {
      msg = JSON.parse(String(raw))
    } catch {
      return
    }
    switch (msg.cmd) {
      case 'turn': {
        // The real server announces each call twice: the provider phase
        // first — identity only, because no fragment of a JSON object
        // parses — then the execution phase with the input complete.
        const useStart = (id, name) =>
          send({ type: 'tool_use_start', tool_id: id, tool_name: name })
        // A slash-command turn never reaches the LLM: the real server
        // diverts /compact to the slash dispatcher, which answers with a
        // broadcast hello replay (folded transcript, fresh usage) followed
        // by a notice — the contract the context pill relies on.
        if (msg.input.trim() === '/compact') {
          const before = session.context_usage_pct
          // compact_to_pct is the set_context backdoor's knob for landing a
          // compact above the 50% gate (sticky-tooltip regression) — the
          // default lands below it, which disables the pill.
          session.context_usage_pct = session.compact_to_pct ?? COMPACT_TO_PCT
          broadcast(helloFrame())
          broadcast({
            type: 'notice',
            message: `Compacted context: 14 → 2 messages (${before}% → ${session.context_usage_pct}% of context window).`,
          })
          break
        }
        // /clear [title] swaps in a fresh conversation and broadcasts a full
        // hello replay whose transcript is empty.  The title names the
        // conversation being LEFT — the archived one, never the fresh one it
        // activates — so a new conversation always starts untitled.  An
        // empty conversation is renamed in place rather than archived.
        if (msg.input.trim() === '/clear' || msg.input.trim().startsWith('/clear ')) {
          const title = msg.input.trim().slice('/clear'.length).trim() || null
          if (title) conversation = { ...conversation, title }
          if (transcript.length) {
            conversation = {
              id: `conv-${conversation.sequence_num + 1}`,
              sequence_num: conversation.sequence_num + 1,
              title: null,
            }
            transcript = []
          }
          broadcast(helloFrame())
          broadcast({
            type: 'notice',
            message: 'Started new conversation. Your previous conversation was archived.',
          })
          break
        }
        // First turn gates on a Bash call; later turns stream and finish —
        // the harness needs a plain send (no ask) to test the send-pin alone.
        const gate = msg.input.startsWith('gate')
        // A "script" turn is the same shell gate holding a call whose content
        // block cannot fit the six lines the card shows — the cap, the
        // scrollbar under it, and the card's height change at the console's
        // bottom edge all need a command that overflows.
        const script = msg.input.startsWith('script')
        // A "think" turn streams reasoning before its answer, the way a
        // thinking-mode model does.  The hold keeps the card open long
        // enough for the harness to see "Thinking…" and its live position;
        // the finished entry is logged so a reload replays the same card.
        const think = msg.input.startsWith('think')
        // A "fail" turn is the one producer of the gutter's ✗: a tool call
        // that ends with success:false.  The seed only ever succeeds, and
        // the gate path is always approved and always returns a result.
        const fail = msg.input.startsWith('fail')
        // A "long" turn streams past a screenful.  That is where the console's
        // top float earns its keep — the prompt that asked for the answer is
        // off the top while the answer is still arriving — and a plain turn's
        // 26 lines end just short of it, at the tail where the harness has to
        // see the difference.
        const long = msg.input.startsWith('long')
        // A "shell" turn runs one long shell call — the card's three-line
        // clip, its copy button and its open-in-editor path all need a call
        // that overflows the clip on both sides.
        const shell = msg.input.startsWith('shell')
        // A "grep" turn runs one long search call — the search card's clip,
        // its single OUT row and its open-in-editor path all need matches
        // that overflow the three lines the card shows.
        const grep = msg.input.startsWith('grep')
        // A "glob" turn is the same card with a result that fits: one OUT
        // row that stays plain text.  It also pins the second name mapping.
        const glob = msg.input.startsWith('glob')
        // A "git" turn runs two git calls back to back — a status that
        // overflows the clip and a log that fits — so one turn covers both
        // readings of a single OUT row, and two names off the table.
        const git = msg.input.startsWith('git')
        // A "files" turn runs the three file calls back to back — a read, an
        // edit, and a write that lands — plus a write that failed, whose
        // path has nothing behind it to open.
        const files = msg.input.startsWith('files')
        // An "overwrite" turn is the file rule read from the other side: a
        // write over a file that is already there, held at a gate so the
        // title can be seen opening it before the call has run, and a read
        // of a path with nothing behind it, which is plain text.
        const overwrite = msg.input.startsWith('overwrite')
        // A "plan" turn proposes a two-step plan and holds for the
        // decision, the way a tool gate does — the approve_plan command
        // below streams the execution beats.
        const planTurn = msg.input.startsWith('plan')
        // The real server auto-titles a conversation from the first user
        // input of its first turn, before the state change whose observer
        // re-broadcasts session_info — so a live header follows untitled →
        // titled without a reload.
        if (!conversation.title) {
          conversation = { ...conversation, title: msg.input.trim().slice(0, MAX_TITLE_LENGTH) }
          broadcast(sessionInfoFrame())
        }
        send({ type: 'turn_started' })
        if (planTurn) {
          send({ type: 'content_delta', text_delta: 'putting a plan together\n' })
          send({
            type: 'plan_proposed',
            plan: { ...PLAN_FRAME, id: `plan-mock-${++planSeq}` },
          })
          break // holds for the decision, like the tool gate
        }
        if (fail) {
          send({ type: 'content_delta', text_delta: 'running the failing call\n' })
          useStart('t2', 'shell')
          send({
            type: 'tool_call_start',
            tool_id: 't2',
            tool_name: 'shell',
            input: { description: 'run the failing command', command: 'false' },
          })
          send({
            type: 'tool_call_end',
            tool_id: 't2',
            tool_name: 'shell',
            input: { description: 'run the failing command', command: 'false' },
            result: {
              success: false,
              output: null,
              error: 'exit status 1',
              checkpoint_id: null,
              metadata: null,
            },
          })
          send({ type: 'agent_finished', reason: 'completed', usage: null })
          send({ type: 'state', busy: false })
          break
        }
        if (shell) {
          send({ type: 'content_delta', text_delta: 'counting the Python files\n' })
          useStart('t3', 'shell')
          send({
            type: 'tool_call_start',
            tool_id: 't3',
            tool_name: 'shell',
            input: { description: SHELL_DESCRIPTION, command: SHELL_COMMAND },
          })
          send({
            type: 'tool_call_end',
            tool_id: 't3',
            tool_name: 'shell',
            input: { description: SHELL_DESCRIPTION, command: SHELL_COMMAND },
            result: {
              success: true,
              output: SHELL_OUTPUT,
              error: null,
              checkpoint_id: null,
              metadata: { command: SHELL_COMMAND, returncode: 0, cwd: '/tmp' },
            },
          })
          send({ type: 'agent_finished', reason: 'completed', usage: null })
          send({ type: 'state', busy: false })
          break
        }
        if (grep) {
          send({ type: 'content_delta', text_delta: 'searching for the search root\n' })
          useStart('t4', 'grep')
          send({
            type: 'tool_call_start',
            tool_id: 't4',
            tool_name: 'grep',
            input: { description: GREP_DESCRIPTION, pattern: GREP_PATTERN },
          })
          send({
            type: 'tool_call_end',
            tool_id: 't4',
            tool_name: 'grep',
            input: {
              description: GREP_DESCRIPTION,
              pattern: GREP_PATTERN,
              path: '.',
              max_results: 100,
              ignore_case: false,
            },
            result: {
              success: true,
              output: GREP_OUTPUT,
              error: null,
              checkpoint_id: null,
              // What the real Grep returns, capped and count in step: nothing
              // renders metadata today, but `truncated` has to agree with the
              // output, which carries no "more matches not shown" line.
              metadata: {
                match_count: GREP_MATCH_COUNT,
                pattern: GREP_PATTERN,
                path: '/tmp',
                truncated: false,
              },
            },
          })
          send({ type: 'agent_finished', reason: 'completed', usage: null })
          send({ type: 'state', busy: false })
          break
        }
        if (glob) {
          send({ type: 'content_delta', text_delta: 'listing the tools\n' })
          useStart('t5', 'glob')
          send({
            type: 'tool_call_start',
            tool_id: 't5',
            tool_name: 'glob',
            input: { description: GLOB_DESCRIPTION, pattern: GLOB_PATTERN },
          })
          send({
            type: 'tool_call_end',
            tool_id: 't5',
            tool_name: 'glob',
            input: { description: GLOB_DESCRIPTION, pattern: GLOB_PATTERN, path: 'toddler' },
            result: {
              success: true,
              output: GLOB_OUTPUT,
              error: null,
              checkpoint_id: null,
              metadata: { match_count: 2, pattern: GLOB_PATTERN, path: '/tmp', truncated: false },
            },
          })
          send({ type: 'agent_finished', reason: 'completed', usage: null })
          send({ type: 'state', busy: false })
          break
        }
        if (git) {
          send({ type: 'content_delta', text_delta: 'checking the working tree\n' })
          useStart('t6', 'git_status')
          send({
            type: 'tool_call_start',
            tool_id: 't6',
            tool_name: 'git_status',
            input: { description: GIT_STATUS_DESCRIPTION },
          })
          send({
            type: 'tool_call_end',
            tool_id: 't6',
            tool_name: 'git_status',
            input: { description: GIT_STATUS_DESCRIPTION },
            result: {
              success: true,
              output: GIT_STATUS_OUTPUT,
              error: null,
              checkpoint_id: null,
              metadata: { repo_path: '/tmp' },
            },
          })
          useStart('t7', 'git_log')
          send({
            type: 'tool_call_start',
            tool_id: 't7',
            tool_name: 'git_log',
            input: { description: GIT_LOG_DESCRIPTION, max_count: 20, oneline: true },
          })
          send({
            type: 'tool_call_end',
            tool_id: 't7',
            tool_name: 'git_log',
            input: { description: GIT_LOG_DESCRIPTION, max_count: 20, oneline: true },
            result: {
              success: true,
              output: GIT_LOG_OUTPUT,
              error: null,
              checkpoint_id: null,
              metadata: { repo_path: '/tmp', max_count: 20 },
            },
          })
          send({ type: 'agent_finished', reason: 'completed', usage: null })
          send({ type: 'state', busy: false })
          break
        }
        if (files) {
          send({ type: 'content_delta', text_delta: 'reading and writing\n' })
          // Five calls of one shape, so the frames are written once.  Each
          // lands in the transcript as well, which is where a reload's hello
          // replays them from — with the same resolved paths the live frame
          // carried, since the real server recomputes them rather than
          // storing them.
          const call = (id, name, input, result) => {
            const paths = cardPaths(input.file_path)
            useStart(id, name)
            send({
              type: 'tool_call_start',
              tool_id: id,
              tool_name: name,
              input,
              // The real server resolves the card's file on the execution
              // start frame too — the loop hands the call over with its
              // input already complete, before any gate — so the card names
              // the file (and marks it external) while it is still in flight.
              ...paths,
            })
            send({
              type: 'tool_call_end',
              tool_id: id,
              tool_name: name,
              input,
              ...paths,
              result,
            })
            transcript.push({
              role: 'tool',
              tool_id: id,
              tool_name: name,
              input,
              ...paths,
              result,
            })
          }
          transcript.push({ role: 'assistant', content: 'reading and writing' })
          call('t8', 'read_file',
            { description: READ_DESCRIPTION, file_path: READ_PATH },
            {
              success: true,
              output: READ_OUTPUT,
              error: null,
              checkpoint_id: null,
              metadata: { path: READ_PATH, total_lines: 3 },
            })
          call('t9', 'edit_file',
            {
              description: EDIT_DESCRIPTION,
              file_path: READ_PATH,
              old_string: 'first seeded file',
              new_string: 'first seeded file, edited',
            },
            {
              success: true,
              output: `Replaced 1 occurrence(s) in ${READ_PATH}.`,
              error: null,
              checkpoint_id: null,
              metadata: { path: READ_PATH, occurrences_replaced: 1, replace_all: false },
            })
          call('t10', 'write_file',
            {
              description: WRITE_DESCRIPTION,
              file_path: WRITE_PATH,
              content: '# mock B\n\nsecond seeded file\n',
            },
            {
              success: true,
              output: `Wrote 29 bytes to ${WRITE_PATH}.`,
              error: null,
              checkpoint_id: null,
              metadata: { path: WRITE_PATH, bytes: 29 },
            })
          // The one write that did not land: its path is named, and there is
          // nothing behind it to open.
          call('t11', 'write_file',
            {
              description: FAILED_WRITE_DESCRIPTION,
              file_path: FAILED_WRITE_PATH,
              content: '# mock C\n',
            },
            {
              success: false,
              output: null,
              error: `Failed to write ${FAILED_WRITE_PATH}: read-only file system`,
              checkpoint_id: null,
              metadata: null,
            })
          // A read of a file the served root cannot hold: no relative path
          // reaches the card, so its title names the file by the absolute
          // path it resolved to — and opens it read-only.  This one holds at
          // a gate, so the harness can see the card say all that while the
          // call has not been allowed to run yet; approve_tool finishes it.
          pendingFileCall = {
            tool_id: FILE_GATE_ID,
            tool_name: 'read_file',
            input: { description: OUTSIDE_DESCRIPTION, file_path: OUTSIDE_PATH },
            paths: cardPaths(OUTSIDE_PATH),
            result: {
              success: true,
              output: '1\t# notes\n2\toutside the repo\n',
              error: null,
              checkpoint_id: null,
              metadata: { path: OUTSIDE_PATH, total_lines: 2 },
            },
          }
          useStart(FILE_GATE_ID, 'read_file')
          send({
            type: 'tool_call_start',
            tool_id: FILE_GATE_ID,
            tool_name: 'read_file',
            input: pendingFileCall.input,
            ...pendingFileCall.paths,
          })
          send({
            type: 'agent_paused',
            prompt: {
              title: GATE_TITLE,
              content: null,
              description: OUTSIDE_DESCRIPTION,
            },
            choices: null,
          })
          break
        }
        if (overwrite) {
          send({ type: 'content_delta', text_delta: 'rewriting it\n' })
          transcript.push({ role: 'assistant', content: 'rewriting it' })
          // The rule is about the file, not the tool: this read names a path
          // no file stands at, so its title is plain text with the path on
          // its hover — the same reading a write that failed gets, from a
          // call that never had anything to land on.
          const gone = {
            description: MISSING_DESCRIPTION,
            file_path: MISSING_PATH,
          }
          const gonePaths = cardPaths(MISSING_PATH)
          useStart('t13', 'read_file')
          send({
            type: 'tool_call_start',
            tool_id: 't13',
            tool_name: 'read_file',
            input: gone,
            ...gonePaths,
          })
          const goneResult = {
            success: false,
            output: null,
            error: `File not found: ${MISSING_PATH}`,
            checkpoint_id: null,
            metadata: null,
          }
          send({
            type: 'tool_call_end',
            tool_id: 't13',
            tool_name: 'read_file',
            input: gone,
            ...gonePaths,
            result: goneResult,
          })
          transcript.push({
            role: 'tool',
            tool_id: 't13',
            tool_name: 'read_file',
            input: gone,
            ...gonePaths,
            result: goneResult,
          })
          // …and the write over a file that IS there, held at its gate: the
          // title opens the very file this call is about to replace, which
          // is what a reader wants while deciding whether to let it.
          pendingFileCall = {
            tool_id: OVERWRITE_GATE_ID,
            tool_name: 'write_file',
            input: {
              description: OVERWRITE_DESCRIPTION,
              file_path: WRITE_PATH,
              content: '# mock B\n\nsecond seeded file, rewritten\n',
            },
            paths: cardPaths(WRITE_PATH),
            result: {
              success: true,
              output: `Wrote 41 bytes to ${WRITE_PATH}.`,
              error: null,
              checkpoint_id: null,
              metadata: { path: WRITE_PATH, bytes: 41 },
            },
          }
          useStart(OVERWRITE_GATE_ID, 'write_file')
          send({
            type: 'tool_call_start',
            tool_id: OVERWRITE_GATE_ID,
            tool_name: 'write_file',
            input: pendingFileCall.input,
            ...pendingFileCall.paths,
          })
          send({
            type: 'agent_paused',
            prompt: {
              title: GATE_TITLE,
              content: null,
              description: OVERWRITE_DESCRIPTION,
            },
            choices: null,
          })
          break
        }
        if (think) {
          for (const fragment of THINK_FRAGMENTS) {
            send({ type: 'reasoning_delta', text_delta: fragment })
          }
          await new Promise((resolve) => setTimeout(resolve, THINK_HOLD_MS))
        }
        let content = ''
        const chunks = long ? 60 : 26
        for (let i = 0; i < chunks; i++) {
          const chunk = `more streaming output chunk ${i}\n`
          content += chunk
          send({ type: 'content_delta', text_delta: chunk })
        }
        if (!gate && !script) {
          if (think) {
            transcript.push({ role: 'assistant', content, reasoning: THINK_REASONING })
          }
          send({ type: 'agent_finished', reason: 'completed', usage: null })
          send({ type: 'state', busy: false })
          break
        }
        // One shell gate, two commands: the plain one whose content block is
        // a single line, and the script whose twelve lines are what the
        // card's six-line cap and its scrollbar exist for.  Both are held by
        // the same pending call, so either answer finishes the right one.
        pendingShellCall = script
          ? { description: SCRIPT_DESCRIPTION, command: SCRIPT_COMMAND }
          : { description: 'list the files', command: 'ls -la' }
        useStart('t1', 'shell')
        send({
          type: 'tool_call_start',
          tool_id: 't1',
          tool_name: 'shell',
          input: pendingShellCall,
        })
        send({
          type: 'agent_paused',
          prompt: {
            title: GATE_TITLE,
            content: pendingShellCall.command,
            description: pendingShellCall.description,
          },
          choices: null,
        })
        break
      }
      case 'approve_tool':
      case 'deny_tool':
        send({ type: 'ack', cmd: msg.cmd, accepted: true })
        if (pendingFileCall && msg.tool_id === pendingFileCall.tool_id) {
          // A gated file call — the outside read, or the write over a file
          // that is already there: either answer settles it and ends its
          // turn, a deny with the result a refusal produces.
          const result = msg.cmd === 'deny_tool'
            ? {
                success: false,
                output: null,
                error: 'the user denied the call',
                checkpoint_id: null,
                metadata: null,
              }
            : pendingFileCall.result
          const call = {
            tool_id: pendingFileCall.tool_id,
            tool_name: pendingFileCall.tool_name,
            input: pendingFileCall.input,
            ...pendingFileCall.paths,
          }
          send({ type: 'tool_call_end', ...call, result })
          transcript.push({ role: 'tool', ...call, result })
          pendingFileCall = null
          send({ type: 'agent_finished', reason: 'completed', usage: null })
          send({ type: 'state', busy: false })
          break
        }
        send({
          type: 'tool_call_end',
          tool_id: 't1',
          tool_name: 'shell',
          input: pendingShellCall,
          result: { success: true, output: 'ok\n', error: null, checkpoint_id: null, metadata: null },
        })
        send({ type: 'content_delta', text_delta: 'done.\n' })
        send({ type: 'agent_finished', reason: 'completed', usage: null })
        send({ type: 'state', busy: false })
        break
      case 'approve_plan': {
        send({ type: 'ack', cmd: 'approve_plan', accepted: true })
        for (const [text, statuses] of PLAN_BEATS) {
          send({ type: 'content_delta', text_delta: text })
          send({
            type: 'plan_step_update',
            steps: PLAN_STEPS.map((step, i) => [step.id, step.description, statuses[i]]),
          })
          await new Promise((resolve) => setTimeout(resolve, 120))
        }
        send({ type: 'agent_finished', reason: 'completed', usage: null })
        send({ type: 'state', busy: false })
        break
      }
      case 'reject_plan':
        send({ type: 'ack', cmd: 'reject_plan', accepted: true })
        send({ type: 'agent_finished', reason: 'completed', usage: null })
        send({ type: 'state', busy: false })
        break
      case 'cancel':
        send({ type: 'ack', cmd: 'cancel', accepted: true })
        send({ type: 'turn_cancelled' })
        // Mirrors the real contract: cancel_turn() persists the repair
        // marker, then the runner's finally broadcasts busy:false.
        transcript.push({
          role: 'user',
          content: '[The previous turn was cancelled by the user.]',
          fold: 'cancelled',
        })
        send({ type: 'state', busy: false })
        break
      case 'ping':
        send({ type: 'pong' })
        break
      case 'rename_conversation': {
        // Mirrors the real command: strip and clamp like the auto-title,
        // refuse an empty one, ack the caller, then broadcast the metadata
        // so every tab relabels its header without a hello replay.
        const title = typeof msg.title === 'string' ? msg.title.trim().slice(0, MAX_TITLE_LENGTH) : ''
        if (!title) {
          send({
            type: 'error',
            code: 'invalid_title',
            message: 'A conversation title cannot be empty.',
          })
          break
        }
        conversation = { ...conversation, title }
        send({ type: 'ack', cmd: 'rename_conversation', accepted: true })
        broadcast(sessionInfoFrame())
        break
      }
      case 'set_mode':
        session.permission_mode = msg.mode
        broadcast(sessionInfoFrame())
        break
      // The picker's two commands.  Both validate, ack the caller and
      // broadcast session_info — the real handlers' shape (ws.py), so a
      // test can await the round trip on an ack before asserting.
      case 'set_model': {
        const slot = session.model_slots.find((s) => s.name === msg.slot)
        if (!slot) {
          send({
            type: 'error',
            code: 'invalid_slot',
            message: 'slot must be one of default, pro, flash.',
          })
          break
        }
        mutations.push({ cmd: 'set_model', slot: msg.slot })
        session.model = slot.spec
        // The slot is stamped even when the spec does not move — that is
        // the whole point of storing it.
        session.model_slot = slot.name
        send({ type: 'ack', cmd: 'set_model', accepted: true })
        broadcast(sessionInfoFrame())
        break
      }
      case 'set_effort': {
        if (!EFFORT_TIERS.includes(msg.tier)) {
          send({
            type: 'error',
            code: 'invalid_effort',
            message: `unknown effort tier: ${msg.tier}`,
          })
          break
        }
        mutations.push({ cmd: 'set_effort', tier: msg.tier })
        session.effort = msg.tier
        send({ type: 'ack', cmd: 'set_effort', accepted: true })
        // The ack still goes out: only the broadcast every tab follows is
        // held, and only when a test asked for it (see hold_effort_echo).
        if (holdingEffortEcho) heldEffortEcho = sessionInfoFrame()
        else broadcast(sessionInfoFrame())
        break
      }
      case 'hold_effort_echo': {
        // Test-only backdoor: park the next set_effort session_info behind
        // an explicit release.  With it parked, the tier on screen can only
        // be the client's own — the server has not said anything yet — and
        // releasing replays exactly what it would have broadcast.
        holdingEffortEcho = Boolean(msg.hold)
        if (!holdingEffortEcho && heldEffortEcho) {
          broadcast(heldEffortEcho)
          heldEffortEcho = null
        }
        send({ type: 'ack', cmd: 'hold_effort_echo', accepted: true })
        break
      }
      case 'set_busy': {
        // Test-only backdoor: hold the app busy without a turn in flight, so
        // the input bar stays mounted (a real turn's tool gate *replaces* the
        // bar, which is exactly the state the busy assertions cannot use).
        // The runner's own frame is `state {busy}`, which is what this sends.
        send({ type: 'ack', cmd: 'set_busy', accepted: true })
        broadcast({ type: 'state', busy: Boolean(msg.busy) })
        break
      }
      case 'retarget_slot': {
        // Test-only backdoor: point a slot at a different model, the way
        // restarting the server with a new TODDLER_<SLOT>_MODEL would.  The
        // conversation's own model and slot are deliberately untouched —
        // that divergence is what the picker has to survive.
        const slot = session.model_slots.find((s) => s.name === msg.slot)
        if (!slot) {
          send({ type: 'error', code: 'invalid_slot', message: 'unknown slot' })
          break
        }
        slot.spec = String(msg.spec)
        send({ type: 'ack', cmd: 'retarget_slot', accepted: true })
        broadcast(sessionInfoFrame())
        break
      }
      case 'force_effort': {
        // Test-only backdoor: write a tier straight into the session, the
        // way a bad TODDLER_EFFORT_LEVEL does.  Nothing validates that
        // setting, so an unrecognized tier really can reach a conversation
        // row — and the picker has to render it rather than fall over.
        session.effort = String(msg.tier)
        send({ type: 'ack', cmd: 'force_effort', accepted: true })
        broadcast(sessionInfoFrame())
        break
      }
      case 'get_mutations':
        send({
          type: 'ack',
          cmd: 'get_mutations',
          accepted: true,
          mutations: [...mutations],
        })
        break
      case 'set_context': {
        // Test-only backdoor: step context_usage_pct across the pill's 50%
        // clickability gate.  compact_to sets where the *next* /compact
        // lands, letting a test click that leaves the pill enabled and
        // focused.  The real server can't be told to lie, but it does
        // broadcast session_info on every state change — same shape.
        const pct = Number(msg.pct)
        const land = msg.compact_to === undefined ? undefined : Number(msg.compact_to)
        if (!Number.isFinite(pct) || (land !== undefined && !Number.isFinite(land))) break
        session.context_usage_pct = pct
        if (land !== undefined) session.compact_to_pct = land
        send({ type: 'ack', cmd: 'set_context', accepted: true })
        broadcast(sessionInfoFrame())
        break
      }
      default:
        break
    }
  })
})

server.listen(PORT, () => console.log(`mock server on :${PORT}`))
