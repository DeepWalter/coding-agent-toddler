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

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://${req.headers.host}`)
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
  cwd: '/tmp',
}

// Seed a tall console so the pane can scroll far before the gate pops.
function seedMessages() {
  const messages = []
  for (let i = 0; i < 14; i++) {
    const lines = []
    for (let j = 0; j < 22; j++) lines.push(`seed line ${i}.${j} — filler to make the console scrollable`)
    messages.push({ role: 'assistant', content: lines.join('\n') })
  }
  return messages
}

// The real server persists a turn-cancelled repair marker and tags it at
// replay — the mock stands in for that pipeline, so cancels accumulate here
// (fold-tagged, like serialize_transcript emits) and every hello replays them.
const cancelMarkers = []

// The real server broadcasts session_info, notices and post-slash hello
// replays to every subscribed client (only the connect-time hello is
// per-client) — the mock mirrors that so a second connection can script
// state (set_context) into the page under test.
const clients = new Set()

function helloFrame() {
  return {
    type: 'hello',
    session,
    conversation: { id: null, sequence_num: null, title: null },
    busy: false,
    paused: null,
    plan: null,
    messages: [...seedMessages(), ...cancelMarkers],
  }
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

  ws.on('message', (raw) => {
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
          session.context_usage_pct = COMPACT_TO_PCT
          broadcast(helloFrame())
          broadcast({
            type: 'notice',
            message: `Compacted context: 14 → 2 messages (${before}% → ${session.context_usage_pct}% of context window).`,
          })
          break
        }
        // First turn gates on a Bash call; later turns stream and finish —
        // the harness needs a plain send (no ask) to test the send-pin alone.
        const gate = msg.input.startsWith('gate')
        send({ type: 'turn_started' })
        for (let i = 0; i < 26; i++) {
          send({ type: 'text_delta', text: `more streaming output chunk ${i}\n` })
        }
        if (!gate) {
          send({ type: 'agent_finished', reason: 'completed', usage: null })
          send({ type: 'state', busy: false })
          break
        }
        send({
          type: 'tool_call_start',
          tool_id: 't1',
          tool_name: 'Bash',
          partial_input: { command: 'ls -la' },
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
          tool_name: 'Bash',
          input: { command: 'ls -la' },
          result: { success: true, output: 'ok\n', error: null, checkpoint_id: null, metadata: null },
        })
        send({ type: 'text_delta', text: 'done.\n' })
        send({ type: 'agent_finished', reason: 'completed', usage: null })
        send({ type: 'state', busy: false })
        break
      case 'cancel':
        send({ type: 'ack', cmd: 'cancel', accepted: true })
        send({ type: 'turn_cancelled' })
        // Mirrors the real contract: cancel_turn() persists the repair
        // marker, then the runner's finally broadcasts busy:false.
        cancelMarkers.push({
          role: 'user',
          content: '[The previous turn was cancelled by the user.]',
          fold: 'cancelled',
        })
        send({ type: 'state', busy: false })
        break
      case 'ping':
        send({ type: 'pong' })
        break
      case 'set_mode':
        broadcast({ type: 'session_info', session: { ...session, permission_mode: msg.mode }, conversation: { id: null, sequence_num: null, title: null } })
        break
      case 'set_context': {
        // Test-only backdoor: step context_usage_pct across the pill's 50%
        // clickability gate.  The real server can't be told to lie, but it
        // does broadcast session_info on every state change — same shape.
        const pct = Number(msg.pct)
        if (!Number.isFinite(pct)) break
        session.context_usage_pct = pct
        send({ type: 'ack', cmd: 'set_context', accepted: true })
        broadcast({ type: 'session_info', session: { ...session }, conversation: { id: null, sequence_num: null, title: null } })
        break
      }
      default:
        break
    }
  })
})

server.listen(PORT, () => console.log(`mock server on :${PORT}`))
