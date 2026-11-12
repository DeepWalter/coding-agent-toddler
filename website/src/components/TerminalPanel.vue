<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'

/**
 * The editor's integrated terminal — xterm.js over the PTY-backed shell at
 * /ws/terminal.
 *
 * The component stays mounted with the editor pane but does nothing until
 * the panel is first shown: the first `visible` creates the terminal,
 * connects the socket, and spawns the shell.  Hiding the panel does not
 * disconnect — the shell is a live process, and closing the overlay is not
 * a request to kill it.  Unmount (a page reload) is.  Once a shell exits,
 * the next show starts a fresh one.
 *
 * Output arrives as binary frames and is handed to xterm undecoded;
 * control frames (ready / error / exit) are JSON text.
 */

const props = defineProps<{
  visible: boolean
  theme: 'tokyo-night' | 'github-light'
}>()

const host = ref<HTMLElement | null>(null)

let term: Terminal | null = null
let fit: FitAddon | null = null
let socket: WebSocket | null = null
let observer: ResizeObserver | null = null
let resizeFrame = 0
let started = false
// The last session ended (exit, refused, or disconnected) — a later show
// resets the screen and connects again.
let sessionDead = false

/** Concrete colors for xterm's canvas; CSS variables cannot be resolved
 *  by the renderer.  Rebuilt whenever the app theme flips. */
function terminalTheme() {
  const style = getComputedStyle(document.documentElement)
  const read = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback
  const background = read('--editor-bg', '#1a1b26')
  const text = read('--text', '#c0caf5')
  return {
    background,
    foreground: text,
    cursor: read('--cursor', text),
    cursorAccent: background,
    selectionBackground: read('--selection', '#33467c'),
    black: read('--editor-surface', background),
    red: read('--error', '#f7768e'),
    green: read('--success', '#9ece6a'),
    yellow: read('--warning', '#e0af68'),
    blue: read('--function', '#7aa2f7'),
    magenta: read('--keyword', '#bb9af7'),
    cyan: read('--attribute', '#7dcfff'),
    white: text,
    brightBlack: read('--comment', '#565f89'),
    brightRed: read('--error', '#f7768e'),
    brightGreen: read('--success', '#9ece6a'),
    brightYellow: read('--warning', '#e0af68'),
    brightBlue: read('--function', '#7aa2f7'),
    brightMagenta: read('--keyword', '#bb9af7'),
    brightCyan: read('--attribute', '#7dcfff'),
    brightWhite: read('--text-bright', text),
  }
}

function refit() {
  const el = host.value
  if (!el || el.clientWidth === 0 || el.clientHeight === 0) return
  try {
    fit?.fit()
  } catch {
    // The panel went away between the check and the measure.
  }
}

function scheduleFit() {
  if (resizeFrame) return
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = 0
    refit()
  })
}

function handleControl(text: string) {
  let frame: { type?: string; message?: string; code?: number }
  try {
    frame = JSON.parse(text) as typeof frame
  } catch {
    return // not ours — a stray text frame is not worth painting
  }
  if (frame.type === 'error') {
    sessionDead = true
    term?.write(`\r\n\x1b[31m${frame.message ?? 'terminal unavailable'}\x1b[0m\r\n`)
  } else if (frame.type === 'exit') {
    sessionDead = true
    term?.write(`\r\n\x1b[2m[process exited with code ${frame.code ?? 0}]\x1b[0m\r\n`)
  }
}

function connect() {
  if (!term) return
  sessionDead = false
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${proto}://${location.host}/ws/terminal`)
  ws.binaryType = 'arraybuffer'
  socket = ws

  ws.onopen = () => {
    if (socket !== ws || !term) return
    refit()
    // The first fit may have run before the socket opened; the current
    // geometry is what the shell needs.
    ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    term.focus()
  }
  ws.onmessage = (event: MessageEvent) => {
    if (socket !== ws) return
    if (typeof event.data === 'string') {
      handleControl(event.data)
      return
    }
    if (event.data instanceof ArrayBuffer) {
      term?.write(new Uint8Array(event.data))
      return
    }
    if (event.data instanceof Blob) {
      void event.data.arrayBuffer().then((buffer) => {
        if (socket === ws) term?.write(new Uint8Array(buffer))
      })
    }
  }
  ws.onclose = () => {
    if (socket !== ws) return
    socket = null
    if (!sessionDead) {
      sessionDead = true
      term?.write('\r\n\x1b[2m[terminal disconnected]\x1b[0m\r\n')
    }
  }
  ws.onerror = () => ws.close()
}

function ensureTerminal() {
  if (started || !host.value) return
  started = true
  const mono = getComputedStyle(document.documentElement)
    .getPropertyValue('--mono')
    .trim()
  term = new Terminal({
    fontFamily: mono || 'monospace',
    fontSize: 13,
    lineHeight: 1.2,
    cursorBlink: true,
    scrollback: 5000,
    theme: terminalTheme(),
  })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.open(host.value)
  term.onData((data) => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'input', data }))
    }
  })
  term.onResize(({ cols, rows }) => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'resize', cols, rows }))
    }
  })
  observer = new ResizeObserver(scheduleFit)
  observer.observe(host.value)
  refit()
  connect()
}

/** Show-time work: first init, a fresh shell after a dead one, and focus. */
function activate() {
  if (!props.visible) return
  void nextTick(() => {
    if (!props.visible || !host.value) return
    if (!started) {
      ensureTerminal()
    } else if (sessionDead && socket === null) {
      term?.reset()
      connect()
    }
    refit()
    term?.focus()
  })
}

onMounted(activate)
watch(() => props.visible, (visible) => {
  if (visible) activate()
})
watch(
  () => props.theme,
  () => {
    if (term) term.options.theme = terminalTheme()
  },
)

onBeforeUnmount(() => {
  if (resizeFrame) cancelAnimationFrame(resizeFrame)
  observer?.disconnect()
  observer = null
  const ws = socket
  socket = null
  ws?.close()
  term?.dispose()
  term = null
})
</script>

<template>
  <div ref="host" class="terminal-host" />
</template>
