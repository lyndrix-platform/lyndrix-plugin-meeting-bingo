import React, { useCallback, useEffect, useRef, useState } from 'react'
import confetti from 'canvas-confetti'
import { bingoApi } from './lib/api'
import type { Cell, LobbyOut, SessionView } from './lib/api'

// ─── Responsive styles (injected once) ─────────────────────────────────────────

const MB_STYLES = `
.mb-page { max-width: 1000px; margin: 0 auto; padding: 28px 20px 48px; }
.mb-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 22px; }
.mb-h1 { margin: 0; font-size: 1.25rem; font-weight: 700; letter-spacing: -0.01em; color: var(--lx-text); }
.mb-sub { margin: 4px 0 0; font-size: 0.8125rem; color: var(--lx-text-muted); }
.mb-lobby-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; }
.mb-game-grid { display: grid; grid-template-columns: minmax(0,2fr) minmax(0,1fr); gap: 22px; align-items: start; }
.mb-card-pad { padding: 18px; display: flex; flex-direction: column; gap: 12px; }
.mb-accent { height: 4px; width: 100%; border-radius: 6px 6px 0 0; }
.mb-board { display: grid; gap: 8px; width: 100%; }
.mb-board--mini { gap: 3px; }
.mb-cell { aspect-ratio: 1 / 1; display: flex; align-items: center; justify-content: center; text-align: center; padding: 6px; border-radius: var(--lx-radius-sm, 8px); border: 1px solid var(--lx-border-soft); background: var(--lx-surface); color: var(--lx-text); cursor: pointer; font-size: 0.72rem; font-weight: 600; line-height: 1.12; user-select: none; transition: transform .08s ease, background .12s ease, border-color .12s ease; overflow: hidden; }
.mb-cell:not(:disabled):hover { border-color: var(--lx-border); }
.mb-cell:not(:disabled):active { transform: scale(0.95); }
.mb-cell span { display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
.mb-cell--mini { aspect-ratio: 1 / 1; padding: 2px; font-size: 0.5rem; font-weight: 600; line-height: 1.05; cursor: default; border-radius: 4px; color: var(--lx-text-muted); }
.mb-cell--mini span { -webkit-line-clamp: 3; }
.mb-toast { position: fixed; top: 18px; left: 50%; transform: translateX(-50%); z-index: 50; padding: 12px 18px; border-radius: var(--lx-radius-md, 10px); font-size: 0.875rem; font-weight: 600; box-shadow: 0 8px 32px rgba(0,0,0,.35); max-width: calc(100vw - 24px); text-align: center; }
.mb-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 14px; border: 1px solid var(--lx-border-soft); border-radius: var(--lx-radius-md, 10px); background: var(--lx-surface); }
.mb-score-row { display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; }
@media (max-width: 760px) {
  .mb-page { padding: 16px 12px 40px; }
  .mb-lobby-grid, .mb-game-grid { grid-template-columns: 1fr; }
  .mb-cell { font-size: 0.66rem; }
}
`

function MBStyles() {
  return <style dangerouslySetInnerHTML={{ __html: MB_STYLES }} />
}

// ─── Helpers ────────────────────────────────────────────────────────────────────

// Self-scheduling poll: run fn, wait for it to settle, THEN wait `ms` before the
// next run. Unlike setInterval this never overlaps requests — so a slow backend
// can't make polls pile up and exhaust the browser's per-host connection limit
// (which would freeze the whole UI, not just this plugin).
function usePolling(fn: () => Promise<void>, ms: number) {
  const fnRef = useRef(fn)
  useEffect(() => {
    fnRef.current = fn
  })
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      if (cancelled) return
      // Skip the request while the tab is hidden/backgrounded (don't poll a
      // bfcached/background document); the cheap timer keeps the loop alive so it
      // resumes automatically when the tab is visible again.
      if (typeof document === 'undefined' || !document.hidden) {
        try {
          await fnRef.current()
        } catch {
          /* keep polling */
        }
      }
      if (cancelled) return
      timer = setTimeout(tick, ms)
    }
    timer = setTimeout(tick, ms)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [ms])
}

function fireConfetti() {
  // Bundled locally (no CDN) so it works on an offline/no-internet network and
  // never accumulates hanging <script> tags.
  confetti({ particleCount: 150, spread: 100, origin: { y: 0.6 } })
}

const MARKED = '#6366f1'
const WON = '#10b981'

function Notice({ ok, msg }: { ok: boolean; msg: string }) {
  const color = ok ? 'var(--lx-state-up)' : 'var(--lx-state-down)'
  return (
    <div
      style={{
        padding: '10px 14px',
        borderRadius: 'var(--lx-radius-md, 10px)',
        fontSize: '0.8125rem',
        color,
        background: `color-mix(in srgb, ${color} 12%, transparent)`,
        border: `1px solid color-mix(in srgb, ${color} 28%, transparent)`,
      }}
    >
      {msg}
    </div>
  )
}

// ─── Boards ─────────────────────────────────────────────────────────────────────

function Board({
  cells,
  size,
  won,
  onMark,
}: {
  cells: Cell[]
  size: number
  won: boolean
  onMark: (i: number) => void
}) {
  return (
    <div className="mb-board" style={{ gridTemplateColumns: `repeat(${size}, 1fr)` }}>
      {cells.map((c, i) => {
        const marked = c.marked
        const bg = marked ? (won ? WON : MARKED) : undefined
        return (
          <button
            key={i}
            className="mb-cell"
            disabled={won}
            onClick={() => onMark(i)}
            style={marked ? { background: bg, color: '#fff', borderColor: bg } : undefined}
          >
            <span>{c.word}</span>
          </button>
        )
      })}
    </div>
  )
}

function MiniBoard({ cells, size }: { cells: Cell[]; size: number }) {
  return (
    <div
      className="mb-board mb-board--mini"
      style={{ gridTemplateColumns: `repeat(${size}, 1fr)` }}
    >
      {cells.map((c, i) => (
        <div
          key={i}
          className="mb-cell mb-cell--mini"
          style={c.marked ? { background: MARKED, color: '#fff', borderColor: MARKED } : undefined}
        >
          <span>{c.word}</span>
        </div>
      ))}
    </div>
  )
}

// ─── Scoreboard ─────────────────────────────────────────────────────────────────

function Scoreboard({ scores }: { scores: { player: string; wins: number }[] }) {
  return (
    <div className="lx-card" style={{ overflow: 'hidden' }}>
      <div className="mb-accent" style={{ background: 'linear-gradient(90deg,#fbbf24,#f59e0b,#eab308)' }} />
      <div className="mb-card-pad">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="material-icons" style={{ fontSize: 20, color: '#f59e0b' }}>
            emoji_events
          </span>
          <span className="lx-section-title">Wall of Shame</span>
        </div>
        {scores.length === 0 ? (
          <p style={{ margin: 0, fontSize: '0.8125rem', color: 'var(--lx-text-muted)', fontStyle: 'italic' }}>
            Noch keine Gewinner. Zeit für das nächste Meeting!
          </p>
        ) : (
          <div style={{ borderRadius: 10, overflow: 'hidden', border: '1px solid var(--lx-border-soft)' }}>
            {scores.map((s, rank) => {
              const medal = rank === 0 ? '🥇' : rank === 1 ? '🥈' : rank === 2 ? '🥉' : `${rank + 1}.`
              const top = rank === 0
              return (
                <div
                  key={s.player}
                  className="mb-score-row"
                  style={{
                    background: top ? 'color-mix(in srgb, #10b981 18%, transparent)' : 'transparent',
                    borderTop: rank ? '1px solid var(--lx-border-soft)' : 'none',
                  }}
                >
                  <span style={{ fontWeight: 700, color: 'var(--lx-text)' }}>
                    {medal} {s.player}
                  </span>
                  <span className="lx-mono" style={{ color: 'var(--lx-text-muted)' }}>
                    {s.wins} Siege
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Lobby ──────────────────────────────────────────────────────────────────────

// In-SPA navigation the shell's BrowserRouter picks up — no full page reload, so
// no cold-load redirect and no document-boundary that breaks the back button.
function spaNavigate(path: string) {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

function goSettings() {
  const p = window.location.pathname.replace(/\/+$/, '')
  const target = p.endsWith('/bingo') ? `${p}/settings` : `${p}/bingo/settings`
  spaNavigate(target)
}

function goBackFromSettings() {
  const p = window.location.pathname.replace(/\/+$/, '')
  const target = p.endsWith('/settings') ? p.slice(0, -'/settings'.length) : p
  spaNavigate(target)
}

function Lobby({
  nick,
  setNick,
  onJoin,
}: {
  nick: string
  setNick: (n: string) => void
  onJoin: (sid: string) => void
}) {
  const [data, setData] = useState<LobbyOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [size, setSize] = useState(3)
  const [terms, setTerms] = useState('')
  const [termsInit, setTermsInit] = useState(false)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const d = await bingoApi.lobby()
      setData(d)
      setError(null)
      if (!termsInit && d.default_terms.length) {
        setTerms(d.default_terms.join('\n'))
        setTermsInit(true)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Fehler beim Laden')
    }
  }, [termsInit])

  useEffect(() => {
    void load()
  }, [load])
  usePolling(load, 2500)

  async function createSession() {
    setBusy(true)
    setNotice(null)
    setError(null)
    try {
      const t = terms.split('\n').map((s) => s.trim()).filter(Boolean)
      await bingoApi.createSession({ name, size, terms: t })
      setName('')
      setNotice(`Session erstellt — tritt ihr unten als „${nick || '…'}" bei.`)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erstellen fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  async function join(sid: string) {
    if (!nick.trim()) {
      setError('Bitte zuerst einen Nickname eingeben.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await bingoApi.join(sid, nick.trim())
      onJoin(sid)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Beitritt fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  const sessions = data?.sessions ?? []
  const minTerms = size * size
  const termCount = terms.split('\n').filter((s) => s.trim()).length

  return (
    <div className="mb-page">
      <div className="mb-header">
        <div>
          <h1 className="mb-h1">Meeting Bingo</h1>
          <p className="mb-sub">Bullshit-Bingo für langatmige Meetings · {sessions.length} aktive Session{sessions.length !== 1 ? 's' : ''}</p>
        </div>
        <button className="lx-btn lx-btn--secondary lx-btn--sm" onClick={goSettings}>
          <span className="material-icons" style={{ fontSize: 15 }}>settings</span>
          Settings
        </button>
      </div>

      {error && <Notice ok={false} msg={error} />}
      {notice && <Notice ok msg={notice} />}

      <div className="mb-lobby-grid" style={{ marginTop: 14 }}>
        {/* Create session */}
        <div className="lx-card" style={{ overflow: 'hidden' }}>
          <div className="mb-accent" style={{ background: 'linear-gradient(90deg,#818cf8,#38bdf8,#22d3ee)' }} />
          <div className="mb-card-pad">
            <span className="lx-section-title">Neue Session starten</span>
            <div>
              <label className="lx-label">Session-Name</label>
              <input
                className="lx-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Daily Standup"
              />
            </div>
            <div>
              <label className="lx-label">Feldgröße: {size}×{size}</label>
              <input
                type="range"
                min={3}
                max={5}
                value={size}
                onChange={(e) => setSize(Number(e.target.value))}
                style={{ width: '100%', accentColor: 'var(--lx-accent)' }}
              />
            </div>
            <div>
              <label className="lx-label">
                Begriffe (eine Zeile pro Begriff) ·{' '}
                <span style={{ color: termCount < minTerms ? 'var(--lx-state-down)' : 'var(--lx-text-muted)' }}>
                  {termCount}/{minTerms}
                </span>
              </label>
              <textarea
                className="lx-input"
                rows={6}
                value={terms}
                onChange={(e) => setTerms(e.target.value)}
                style={{ resize: 'vertical', fontFamily: 'inherit' }}
              />
            </div>
            <button
              className="lx-btn lx-btn--primary"
              onClick={() => void createSession()}
              disabled={busy || !name.trim() || termCount < minTerms}
            >
              Session erstellen
            </button>
          </div>
        </div>

        {/* Join session */}
        <div className="lx-card" style={{ overflow: 'hidden' }}>
          <div className="mb-accent" style={{ background: 'linear-gradient(90deg,#34d399,#2dd4bf,#4ade80)' }} />
          <div className="mb-card-pad">
            <span className="lx-section-title">Session beitreten</span>
            <div>
              <label className="lx-label">Dein Nickname</label>
              <input
                className="lx-input"
                value={nick}
                onChange={(e) => setNick(e.target.value)}
                placeholder="Gast"
              />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {sessions.length === 0 ? (
                <p style={{ margin: 0, fontSize: '0.8125rem', color: 'var(--lx-text-muted)', fontStyle: 'italic' }}>
                  Keine aktiven Sessions.
                </p>
              ) : (
                sessions.map((s) => (
                  <div key={s.id} className="mb-row">
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontWeight: 700, fontSize: '0.8125rem', color: 'var(--lx-text)' }}>{s.name}</div>
                      <div style={{ fontSize: '0.6875rem', color: 'var(--lx-text-muted)' }}>
                        {s.players} Spieler · {s.size}×{s.size}
                        {s.winners > 0 && ` · ${s.winners} 🏆`}
                      </div>
                    </div>
                    <button
                      className="lx-btn lx-btn--primary lx-btn--sm"
                      onClick={() => void join(s.id)}
                      disabled={busy}
                    >
                      Beitreten
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>

      {data?.scoreboard_enabled && (
        <div style={{ marginTop: 18 }}>
          <Scoreboard scores={data.scoreboard} />
        </div>
      )}
    </div>
  )
}

// ─── Game ───────────────────────────────────────────────────────────────────────

function Game({ sid, nick, onLeave }: { sid: string; nick: string; onLeave: () => void }) {
  const [session, setSession] = useState<SessionView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<{ msg: string; kind: 'win' | 'meh' } | null>(null)

  const load = useCallback(async () => {
    try {
      const s = await bingoApi.getSession(sid, nick)
      setSession(s)
      setError(null)
    } catch (e) {
      const m = e instanceof Error ? e.message : 'Fehler'
      if (m.includes('nicht gefunden')) onLeave()
      else setError(m)
    }
  }, [sid, nick, onLeave])

  useEffect(() => {
    void load()
  }, [load])
  usePolling(load, 1500)

  useEffect(() => {
    if (!toast) return
    const id = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(id)
  }, [toast])

  async function mark(i: number) {
    if (!session || session.you.won) return
    try {
      const { result, session: s } = await bingoApi.mark(sid, nick, i)
      setSession(s)
      if (result.first_winner) {
        fireConfetti()
        setToast({ msg: 'BINGO! 🎉 Du hast das Meeting überlebt!', kind: 'win' })
      } else if (result.sarcastic) {
        setToast({ msg: result.sarcastic, kind: 'meh' })
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Fehler')
    }
  }

  if (!session) {
    return (
      <div className="mb-page">
        {error ? <Notice ok={false} msg={error} /> : <div className="lx-spinner" />}
      </div>
    )
  }

  const board = session.you.board ?? []

  return (
    <div className="mb-page">
      {toast && (
        <div
          className="mb-toast"
          style={{
            background: toast.kind === 'win' ? WON : 'var(--lx-state-paused)',
            color: toast.kind === 'win' ? '#04210f' : '#241a02',
          }}
        >
          {toast.msg}
        </div>
      )}

      <div className="mb-header">
        <div style={{ minWidth: 0 }}>
          <h1 className="mb-h1">{session.name}</h1>
          <p className="mb-sub">
            Dein Board · {nick}
            {session.you.won && ' · 🏆 Bingo!'}
          </p>
        </div>
        <button className="lx-btn lx-btn--secondary lx-btn--sm" onClick={onLeave}>
          <span className="material-icons" style={{ fontSize: 15 }}>logout</span>
          Verlassen
        </button>
      </div>

      {error && <Notice ok={false} msg={error} />}

      <div className="mb-game-grid" style={{ marginTop: 8 }}>
        {/* Your board */}
        <div style={{ minWidth: 0 }}>
          <Board cells={board} size={session.size} won={session.you.won} onMark={(i) => void mark(i)} />
        </div>

        {/* Other players */}
        <div className="lx-card" style={{ overflow: 'hidden' }}>
          <div className="mb-accent" style={{ background: 'linear-gradient(90deg,#a78bfa,#8b5cf6,#818cf8)' }} />
          <div className="mb-card-pad">
            <span className="lx-section-title">Andere Spieler</span>
            {session.others.length === 0 ? (
              <p style={{ margin: 0, fontSize: '0.8125rem', color: 'var(--lx-text-muted)', fontStyle: 'italic' }}>
                Noch niemand sonst dabei.
              </p>
            ) : (
              session.others.map((p) => (
                <div
                  key={p.nick}
                  style={{
                    border: '1px solid var(--lx-border-soft)',
                    borderRadius: 10,
                    padding: 10,
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 8,
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontWeight: 700, fontSize: '0.8125rem', color: 'var(--lx-accent)' }}>{p.nick}</div>
                      <div style={{ fontSize: '0.6875rem', color: 'var(--lx-text-muted)' }}>
                        {p.marks}/{p.total} markiert
                      </div>
                    </div>
                    {p.won ? (
                      <span
                        className="material-icons"
                        title={p.place ? `${p.place}. Platz` : 'Bingo'}
                        style={{ fontSize: 20, color: p.place === 1 ? '#f59e0b' : 'var(--lx-text-muted)' }}
                      >
                        emoji_events
                      </span>
                    ) : (
                      <span className="material-icons" style={{ fontSize: 16, color: 'var(--lx-text-muted)', opacity: 0.5 }}>
                        more_horiz
                      </span>
                    )}
                  </div>
                  <MiniBoard cells={p.board} size={session.size} />
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ─── Settings ───────────────────────────────────────────────────────────────────

function SettingsView() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    bingoApi
      .getSettings()
      .then((s) => setEnabled(s.scoreboard_enabled))
      .catch((e) => setError(e instanceof Error ? e.message : 'Fehler'))
  }, [])

  async function save(next: boolean) {
    setBusy(true)
    setNotice(null)
    setError(null)
    try {
      const s = await bingoApi.setSettings(next)
      setEnabled(s.scoreboard_enabled)
      setNotice('Einstellungen gespeichert.')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Speichern fehlgeschlagen (nur Admins)')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mb-page" style={{ maxWidth: 680 }}>
      <div className="mb-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button className="lx-icon-btn" title="Zurück" onClick={goBackFromSettings}>
            <span className="material-icons" style={{ fontSize: 18 }}>arrow_back</span>
          </button>
          <div>
            <h1 className="mb-h1">Meeting Bingo</h1>
            <p className="mb-sub">Einstellungen</p>
          </div>
        </div>
      </div>

      {error && <Notice ok={false} msg={error} />}
      {notice && <Notice ok msg={notice} />}

      <div className="lx-card" style={{ overflow: 'hidden', marginTop: 12 }}>
        <div className="mb-accent" style={{ background: 'linear-gradient(90deg,#34d399,#2dd4bf,#4ade80)' }} />
        <div className="mb-card-pad">
          <span className="lx-section-title">Ethik-Einstellungen</span>
          {enabled === null ? (
            <div className="lx-spinner" />
          ) : (
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={enabled}
                disabled={busy}
                onChange={(e) => void save(e.target.checked)}
                style={{ width: 18, height: 18, accentColor: 'var(--lx-accent)' }}
              />
              <span style={{ fontSize: '0.875rem', color: 'var(--lx-text)' }}>
                Scoreboard aktivieren (erfasst Gewinner permanent)
              </span>
            </label>
          )}
          <p style={{ margin: 0, fontSize: '0.75rem', color: '#f59e0b', fontStyle: 'italic' }}>
            Tipp: Je weniger Beweise, desto besser für den Ethikcodex!
          </p>
        </div>
      </div>
    </div>
  )
}

// ─── Root — path-based routing ──────────────────────────────────────────────────

function BingoRoot() {
  const [nick, setNickState] = useState<string>(
    () => localStorage.getItem('lyndrix_bingo_nick') || '',
  )
  // Stable callbacks — passing fresh arrow fns would change Game's `load`
  // identity every render and spin its mount effect into an infinite fetch loop.
  const setNick = useCallback((n: string) => {
    setNickState(n)
    localStorage.setItem('lyndrix_bingo_nick', n)
  }, [])
  const [sid, setSid] = useState<string | null>(null)
  const onJoin = useCallback((s: string) => setSid(s), [])
  const onLeave = useCallback(() => setSid(null), [])

  return sid ? (
    <Game sid={sid} nick={nick} onLeave={onLeave} />
  ) : (
    <Lobby nick={nick} setNick={setNick} onJoin={onJoin} />
  )
}

export default function PluginApp() {
  const isSettings = window.location.pathname.endsWith('/settings')
  return (
    <>
      <MBStyles />
      {isSettings ? <SettingsView /> : <BingoRoot />}
    </>
  )
}
