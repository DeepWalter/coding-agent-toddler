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
  context_usage_pct: 12,
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

const wss = new WebSocketServer({ server, path: '/ws' })

wss.on('connection', (ws) => {
  const send = (frame) => ws.send(JSON.stringify(frame))
  send({
    type: 'hello',
    session,
    conversation: { id: null, sequence_num: null, title: null },
    busy: false,
    paused: null,
    plan: null,
    messages: seedMessages(),
  })

  ws.on('message', (raw) => {
    let msg
    try {
      msg = JSON.parse(String(raw))
    } catch {
      return
    }
    switch (msg.cmd) {
      case 'turn':
        send({ type: 'turn_started' })
        for (let i = 0; i < 26; i++) {
          send({ type: 'text_delta', text: `more streaming output chunk ${i}\n` })
        }
        send({
          type: 'tool_call_start',
          tool_id: 't1',
          tool_name: 'Bash',
          partial_input: { command: 'ls -la' },
        })
        send({
          type: 'agent_paused',
          prompt: 'The agent wants to run Bash: ls -la — approve?',
          choices: null,
        })
        break
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
        break
      case 'cancel':
        send({ type: 'ack', cmd: 'cancel', accepted: true })
        send({ type: 'turn_cancelled' })
        break
      case 'ping':
        send({ type: 'pong' })
        break
      case 'set_mode':
        send({ type: 'session_info', session: { ...session, permission_mode: msg.mode }, conversation: { id: null, sequence_num: null, title: null } })
        break
      default:
        break
    }
  })
})

server.listen(PORT, () => console.log(`mock server on :${PORT}`))
