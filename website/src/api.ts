import type { GitCommitRequest, GitDiffPayload, GitFileRequest, GitStatusPayload, HunkApplyRequest, ReplayMessage, SessionSummary, TreeEntry } from './types'

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

  gitStatus(): Promise<GitStatusPayload> {
    // no-store: polled on every refresh trigger; a heuristically cached
    // GET would show stale badges after edits.
    return request('/api/git/status', { cache: 'no-store' }) as Promise<GitStatusPayload>
  },

  gitDiff(path: string, staged: boolean): Promise<GitDiffPayload> {
    // no-store: refetched on tab activation; stale hunks after edits
    // would be misleading.
    return request(`/api/git/diff?path=${encodeURIComponent(path)}&staged=${staged ? 1 : 0}`, {
      cache: 'no-store',
    }) as Promise<GitDiffPayload>
  },

  gitApplyHunk(body: HunkApplyRequest): Promise<{ ok: boolean }> {
    return request('/api/git/hunk', jsonBody(body)) as Promise<{ ok: boolean }>
  },

  gitApplyFile(body: GitFileRequest): Promise<{ ok: boolean }> {
    return request('/api/git/file', jsonBody(body)) as Promise<{ ok: boolean }>
  },

  gitCommit(body: GitCommitRequest): Promise<{ ok: boolean }> {
    return request('/api/git/commit', jsonBody(body)) as Promise<{ ok: boolean }>
  },

  tree(depth = 10): Promise<{ root: string; entries: TreeEntry[] }> {
    return request(`/api/tree?depth=${depth}`) as Promise<{
      root: string
      entries: TreeEntry[]
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
