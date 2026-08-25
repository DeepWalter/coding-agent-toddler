import type { ReplayMessage, SessionSummary } from './types'

/**
 * Thin client for the /api REST endpoints.  Paths are relative so the
 * dev server (Vite proxy) and production (same-origin static mount)
 * both work unchanged.
 */

async function request(path: string, init?: RequestInit): Promise<unknown> {
  const res = await fetch(path, init)
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    try {
      const body = (await res.json()) as { error?: string }
      if (body.error) message = body.error
    } catch {
      // Not JSON — keep the status text.
    }
    throw new Error(message)
  }
  return res.json()
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export const api = {
  meta(): Promise<{ repo_root: string; model: string; dev: boolean }> {
    return request('/api/meta') as Promise<{ repo_root: string; model: string; dev: boolean }>
  },

  sessions(): Promise<{ sessions: SessionSummary[] }> {
    return request('/api/sessions') as Promise<{ sessions: SessionSummary[] }>
  },

  createSession(title?: string): Promise<SessionSummary> {
    return request('/api/sessions', jsonBody({ title })) as Promise<SessionSummary>
  },

  messages(sessionId: string, conversationId?: string): Promise<{ messages: ReplayMessage[] }> {
    const q = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''
    return request(`/api/sessions/${sessionId}/messages${q}`) as Promise<{ messages: ReplayMessage[] }>
  },

  tree(depth = 4): Promise<{ root: string; entries: { path: string; type: string }[] }> {
    return request(`/api/tree?depth=${depth}`) as Promise<{
      root: string
      entries: { path: string; type: string }[]
    }>
  },

  readFile(path: string): Promise<{ path: string; content: string; total_lines: number }> {
    return request(`/api/file?path=${encodeURIComponent(path)}`) as Promise<{
      path: string
      content: string
      total_lines: number
    }>
  },

  writeFile(path: string, content: string): Promise<{ ok: boolean; bytes: number }> {
    return fetch(`/api/file?path=${encodeURIComponent(path)}`, {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }).then(async (res) => {
      const body = (await res.json()) as { ok?: boolean; bytes?: number; error?: string }
      if (!res.ok) throw new Error(body.error ?? `${res.status} ${res.statusText}`)
      return body as { ok: boolean; bytes: number }
    })
  },
}
