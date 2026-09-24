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

const API = {
  '/api/meta': { repo_root: '/tmp', model: 'mock-model', dev: false },
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
])

function filePayload(rel, content) {
  // Same line count as read_file: newlines, plus one for a trailing line
  // that has no newline of its own.
  const newlines = (content.match(/\n/g) ?? []).length
  return { path: rel, content, total_lines: newlines + (content.endsWith('\n') ? 0 : 1) }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://${req.headers.host}`)
  if (url.pathname === '/api/file') {
    const rel = url.searchParams.get('path') ?? ''
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
        // The real server auto-titles a conversation from the first user
        // input of its first turn, before the state change whose observer
        // re-broadcasts session_info — so a live header follows untitled →
        // titled without a reload.
        if (!conversation.title) {
          conversation = { ...conversation, title: msg.input.trim().slice(0, MAX_TITLE_LENGTH) }
          broadcast(sessionInfoFrame())
        }
        send({ type: 'turn_started' })
        if (fail) {
          send({ type: 'content_delta', text_delta: 'running the failing call\n' })
          send({
            type: 'tool_call_start',
            tool_id: 't2',
            tool_name: 'shell',
            partial_input: { description: 'run the failing command', command: 'false' },
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
          send({
            type: 'tool_call_start',
            tool_id: 't3',
            tool_name: 'shell',
            partial_input: { description: SHELL_DESCRIPTION, command: SHELL_COMMAND },
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
        if (!gate) {
          if (think) {
            transcript.push({ role: 'assistant', content, reasoning: THINK_REASONING })
          }
          send({ type: 'agent_finished', reason: 'completed', usage: null })
          send({ type: 'state', busy: false })
          break
        }
        send({
          type: 'tool_call_start',
          tool_id: 't1',
          tool_name: 'shell',
          partial_input: { description: 'list the files', command: 'ls -la' },
        })
        send({
          type: 'agent_paused',
          tool_id: 't1',
          prompt: 'The agent wants to run Bash: ls -la — approve?',
          choices: null,
        })
        break
      }
      case 'approve_tool':
      case 'deny_tool':
        send({ type: 'ack', cmd: msg.cmd, accepted: true })
        send({
          type: 'tool_call_end',
          tool_id: 't1',
          tool_name: 'shell',
          input: { description: 'list the files', command: 'ls -la' },
          result: { success: true, output: 'ok\n', error: null, checkpoint_id: null, metadata: null },
        })
        send({ type: 'content_delta', text_delta: 'done.\n' })
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
