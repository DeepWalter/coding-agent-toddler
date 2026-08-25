import { onUnmounted, ref } from 'vue'
import type { Command, Frame } from '../types'

/**
 * WebSocket transport for the /ws endpoint.
 *
 * Reconnects automatically with exponential backoff (1s → 10s, capped)
 * and resets the backoff on a successful open.  A 25s ping keeps the
 * connection alive so a dead server surfaces as a close instead of a
 * silent hang.  Frames are JSON-parsed and handed to the onFrame
 * callback (the useConsole reducer) in arrival order.
 */
export function useWebSocket() {
  const connected = ref(false)
  const connecting = ref(false)

  let socket: WebSocket | null = null
  let retryCount = 0
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let heartbeat: ReturnType<typeof setInterval> | null = null
  let handler: ((frame: Frame) => void) | null = null

  function wsUrl(): string {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    return `${proto}://${location.host}/ws`
  }

  function send(payload: Command) {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload))
    }
  }

  function startHeartbeat() {
    stopHeartbeat()
    heartbeat = setInterval(() => send({ cmd: 'ping' }), 25_000)
  }

  function stopHeartbeat() {
    if (heartbeat !== null) {
      clearInterval(heartbeat)
      heartbeat = null
    }
  }

  function scheduleReconnect() {
    if (retryTimer !== null) return
    const delay = Math.min(1000 * 2 ** retryCount, 10_000)
    retryTimer = setTimeout(() => {
      retryTimer = null
      connect()
    }, delay)
  }

  function connect() {
    connecting.value = true
    socket = new WebSocket(wsUrl())
    socket.onopen = () => {
      connected.value = true
      connecting.value = false
      retryCount = 0
      startHeartbeat()
    }
    socket.onmessage = (event: MessageEvent) => {
      try {
        handler?.(JSON.parse(String(event.data)) as Frame)
      } catch {
        // Not JSON (or truncated) — drop the frame; the next one heals.
      }
    }
    socket.onclose = () => {
      connected.value = false
      connecting.value = false
      stopHeartbeat()
      socket = null
      scheduleReconnect()
    }
    socket.onerror = () => {
      // close follows; onclose does the state + reconnect work.
      socket?.close()
    }
  }

  function onFrame(callback: (frame: Frame) => void) {
    handler = callback
  }

  onUnmounted(() => {
    stopHeartbeat()
    if (retryTimer !== null) clearTimeout(retryTimer)
    socket?.close()
  })

  connect()

  return { connected, connecting, send, onFrame }
}
