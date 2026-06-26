const PLUGIN_ID_PREFIX = 'lyndrix.plugin.bingo'
const TOKEN_KEY = 'lyndrix_token'

// ─── Shared types (mirror app/api.py) ──────────────────────────────────────────

export interface Cell {
  word: string
  marked: boolean
}

export interface SessionSummary {
  id: string
  name: string
  size: number
  players: number
  winners: number
}

export interface ScoreEntry {
  player: string
  wins: number
}

export interface LobbyOut {
  sessions: SessionSummary[]
  scoreboard_enabled: boolean
  scoreboard: ScoreEntry[]
  default_terms: string[]
}

export interface OtherPlayer {
  nick: string
  marks: number
  total: number
  won: boolean
  place: number | null
  board: Cell[]
}

export interface SessionView {
  id: string
  name: string
  size: number
  you: { nick: string; board: Cell[] | null; won: boolean; joined: boolean }
  others: OtherPlayer[]
  winners: string[]
  scoreboard_enabled: boolean
}

export interface MarkResult {
  won: boolean
  first_winner: boolean
  sarcastic: string | null
}

// ─── Fetch helper ──────────────────────────────────────────────────────────────

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(init.headers as Record<string, string> | undefined),
  }

  const res = await fetch(path, { ...init, headers })

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    window.location.href = '/login'
    throw new Error('Nicht autorisiert')
  }

  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      msg = body.detail ?? msg
    } catch {
      /* ignore */
    }
    throw new Error(msg)
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

const base = (subpath: string) => `/api/plugins/${PLUGIN_ID_PREFIX}/${subpath}`

export const bingoApi = {
  lobby: () => apiFetch<LobbyOut>(base('lobby')),

  createSession: (body: { name: string; size: number; terms: string[] }) =>
    apiFetch<{ session_id: string }>(base('sessions'), {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  join: (sid: string, nick: string) =>
    apiFetch<SessionView>(base(`sessions/${encodeURIComponent(sid)}/join`), {
      method: 'POST',
      body: JSON.stringify({ nick }),
    }),

  getSession: (sid: string, nick: string) =>
    apiFetch<SessionView>(
      base(`sessions/${encodeURIComponent(sid)}?nick=${encodeURIComponent(nick)}`),
    ),

  mark: (sid: string, nick: string, index: number) =>
    apiFetch<{ result: MarkResult; session: SessionView }>(
      base(`sessions/${encodeURIComponent(sid)}/mark`),
      { method: 'POST', body: JSON.stringify({ nick, index }) },
    ),

  getSettings: () => apiFetch<{ scoreboard_enabled: boolean }>(base('settings')),

  setSettings: (scoreboard_enabled: boolean) =>
    apiFetch<{ scoreboard_enabled: boolean }>(base('settings'), {
      method: 'PUT',
      body: JSON.stringify({ scoreboard_enabled }),
    }),
}
