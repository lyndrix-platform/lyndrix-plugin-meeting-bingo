"""Meeting Bingo — game state + logic (single source of truth).

Sessions, players, the scoreboard and the default term list live in the shared
database (``app/model/models.py``) so every core instance behind the load
balancer serves the same lobby and boards. Only ``scoreboard_enabled`` stays in
Vault; it is cached per process and re-read when a peer announces a change
(``bingo:settings_changed``, cluster-wide) or the core reports a settings save.

The React bundle drives this service over the HTTP router in ``app/api.py``;
keeping the rules here (``check_win``, scoring) means the UI never re-implements
game logic. Every public method except ``check_win`` is blocking (DB and/or
Vault) — async callers offload them with ``asyncio.to_thread``.

Concurrency: writes are short READ COMMITTED transactions retried on MariaDB
1020/1205/1213; a mark applies only to the board version it read (optimistic
lock), and the first winner is claimed with ``UPDATE … WHERE winner IS NULL``
so two instances can never both award it.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from core.api import get_logger

from ..model.database import _bootstrap_lock, db_ready, ensure_tables, get_session, retry_write
from ..model.models import BingoConfig, BingoPlayer, BingoScore, BingoSession

log = get_logger("Plugin:Bingo")

# Plugin root = .../<plugin>/app/logic/service.py → up three levels.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
# Legacy (≤ 1.1) term list on the plugin's local disk; imported once into the DB.
_TERMS_FILE = os.path.join(_ROOT, "terms.txt")

SETTINGS_TOPIC = "bingo:settings_changed"

_CFG_TERMS = "default_terms"
_CFG_SCOREBOARD_IMPORTED = "scoreboard_imported"

_TERMS_CACHE_TTL = 60.0
# Open sessions without any activity for this long drop out of the lobby, and
# closed ones are deleted after the purge window (leader only, at most hourly).
SESSION_IDLE_HOURS = 24
SESSION_PURGE_DAYS = 30
_CLEANUP_INTERVAL = 3600.0
_MARK_ATTEMPTS = 5

DEFAULT_TERMS: List[str] = [
    "Synergieeffekte", "Wir müssen das agil angehen", "Könnt ihr mich hören?",
    "Ich teile mal meinen Bildschirm", "Werfe ich mal in die Runde", "Lass uns das offline besprechen",
    "Das ist ein valider Punkt", "Da bin ich ganz bei dir", "Können wir das kurz parken?",
    "Haben wir noch genug Puffer?", "Win-Win Situation", "Wir müssen die PS auf die Straße bringen",
    "Da müssen wir nochmal tiefer reinbohren", "Ich war auf Mute", "Sorry, ich hatte Verbindungsprobleme",
    "Nehmen wir mal als Action Item mit", "Best Practice", "Low Hanging Fruits",
    "Das steht nicht auf der Agenda", "Quick Win", "KPIs", "Out of the box", "Am Ende des Tages",
    "Ich muss gleich in den nächsten Call", "Lasst uns das skalieren",
]

SARCASTIC_MESSAGES: List[str] = [
    "Knapp vorbei ist auch 2. Platz.",
    "Toll gemacht, du bist der erste Verlierer.",
    "Schön für dich. Der Keks ist aber schon weg.",
    "Immerhin hast du teilgenommen.",
    "Silbermedaille im Bullshit-Bingo. Glückwunsch?",
    "Die Goldmedaille für 'Zuspätkommen' geht an dich.",
]


class BingoUnavailable(RuntimeError):
    """The database is not (yet) available — the API answers 503."""


class _StaleBoard(Exception):
    """A concurrent mark changed the board between our read and our write."""


def check_win(board: List[Dict[str, Any]], size: int) -> bool:
    """Row, column, or either diagonal fully marked."""
    for i in range(size):
        if all(board[i * size + j]["marked"] for j in range(size)):
            return True
    for j in range(size):
        if all(board[i * size + j]["marked"] for i in range(size)):
            return True
    if all(board[i * size + i]["marked"] for i in range(size)):
        return True
    if all(board[i * size + (size - 1 - i)]["marked"] for i in range(size)):
        return True
    return False


def _cells(words: List[str], marks: List[bool]) -> List[Dict[str, Any]]:
    return [{"word": w, "marked": bool(m)} for w, m in zip(words, marks)]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BingoService:
    def __init__(self) -> None:
        # Process-local cache of the Vault-backed toggle (see reload_settings()).
        self.state: Dict[str, Any] = {"scoreboard_enabled": False}
        self._ctx = None
        self._ready = False
        self._terms_cache: Optional[tuple[List[str], float]] = None
        self._last_cleanup = 0.0

    def bind(self, ctx) -> None:
        self._ctx = ctx

    # ── Lifecycle / bootstrap ────────────────────────────────────────────────
    @property
    def is_ready(self) -> bool:
        return self._ready and db_ready()

    def _require_db(self) -> None:
        if not self.is_ready:
            raise BingoUnavailable("Datenbank nicht verfügbar.")

    def bootstrap(self) -> None:
        """Create the tables and import the legacy terms file (blocking, idempotent)."""
        ensure_tables()
        self._ready = True
        self.import_legacy_terms()

    async def abootstrap(self) -> bool:
        try:
            await asyncio.to_thread(self.bootstrap)
            return True
        except Exception as exc:
            log.error(f"Bingo: DB init failed — {exc}")
            return False

    def import_legacy_terms(self) -> bool:
        """One-time import of an operator-edited ``terms.txt`` into the DB."""
        try:
            with open(_TERMS_FILE, "r", encoding="utf-8") as f:
                terms = [line.strip() for line in f if line.strip()]
        except OSError:
            return False
        # The shipped file equals DEFAULT_TERMS — nothing worth persisting.
        if not terms or terms == DEFAULT_TERMS or not self.is_ready:
            return False
        try:
            with _bootstrap_lock(), get_session() as s:
                if s.get(BingoConfig, _CFG_TERMS) is not None:
                    return False
                s.add(BingoConfig(key=_CFG_TERMS, value=json.dumps(terms, ensure_ascii=False)))
                s.commit()
        except IntegrityError:
            return False  # a peer imported it concurrently
        self._terms_cache = None
        log.info(f"Bingo: imported {len(terms)} terms from legacy terms.txt")
        return True

    def import_legacy_scoreboard(self) -> int:
        """One-time import of the ≤ 1.1 Vault scoreboard (JSON) into the DB.

        Serialized by the bootstrap lock and guarded by a marker row, so N
        instances never double-count. Existing DB rows win over Vault values.
        """
        if not self.is_ready or self._ctx is None:
            return 0
        with get_session() as s:
            if s.get(BingoConfig, _CFG_SCOREBOARD_IMPORTED) is not None:
                return 0
        try:
            stored = self._ctx.get_secret("bingo_scoreboard")
            data = json.loads(stored) if stored else {}
            if not isinstance(data, dict):
                data = {}
        except (TypeError, ValueError) as exc:
            log.error(f"Bingo: legacy scoreboard unreadable, skipping import: {exc}")
            data = {}
        imported = 0
        try:
            with _bootstrap_lock(), get_session() as s:
                if s.get(BingoConfig, _CFG_SCOREBOARD_IMPORTED) is not None:
                    return 0
                for player, wins in data.items():
                    name = str(player)[:64]
                    try:
                        count = int(wins)
                    except (TypeError, ValueError):
                        continue
                    if name and count > 0 and s.get(BingoScore, name) is None:
                        s.add(BingoScore(player=name, wins=count))
                        imported += 1
                s.add(BingoConfig(key=_CFG_SCOREBOARD_IMPORTED, value=_utcnow().isoformat()))
                s.commit()
        except IntegrityError:
            return 0  # a peer imported it concurrently
        if imported:
            log.info(f"Bingo: imported {imported} scoreboard entries from Vault")
        return imported

    def load_from_vault(self) -> None:
        """Read the toggle from Vault and run the one-time scoreboard import."""
        self.reload_settings()
        try:
            self.import_legacy_scoreboard()
        except Exception as exc:
            log.error(f"Bingo: scoreboard import failed: {exc}")

    # ── Terms ────────────────────────────────────────────────────────────────
    def default_terms(self) -> List[str]:
        cached = self._terms_cache
        if cached and time.monotonic() - cached[1] < _TERMS_CACHE_TTL:
            return list(cached[0])
        terms: Optional[List[str]] = None
        if self.is_ready:
            try:
                with get_session() as s:
                    row = s.get(BingoConfig, _CFG_TERMS)
                    if row is not None and row.value:
                        terms = [str(t).strip() for t in json.loads(row.value) if str(t).strip()]
            except Exception as exc:
                log.warning(f"Bingo: could not read default terms ({exc!s:.120}) — using built-ins")
        terms = terms or list(DEFAULT_TERMS)
        self._terms_cache = (terms, time.monotonic())
        return list(terms)

    # ── Settings ─────────────────────────────────────────────────────────────
    def get_settings(self) -> Dict[str, Any]:
        return {"scoreboard_enabled": self.state["scoreboard_enabled"]}

    def reload_settings(self) -> None:
        """Re-read ``scoreboard_enabled`` from Vault (blocking).

        Symmetric restore: assign BOTH True and False; tolerate native bools.
        Only a missing secret (or no Vault) keeps the current value.
        """
        if self._ctx is None:
            return
        raw = self._ctx.get_secret("scoreboard_enabled")
        if raw is not None:
            self.state["scoreboard_enabled"] = raw in (True, "True")

    def set_scoreboard_enabled(self, enabled: bool) -> Dict[str, Any]:
        self.state["scoreboard_enabled"] = bool(enabled)
        if self._ctx is not None:
            try:
                self._ctx.set_secret("scoreboard_enabled", str(bool(enabled)))
            except Exception as exc:  # noqa: BLE001 - Vault client raises varied errors; log and continue
                log.error(f"Failed to persist scoreboard_enabled to Vault: {exc}")
        return self.get_settings()

    async def update_scoreboard_enabled(self, enabled: bool) -> Dict[str, Any]:
        """Persist the toggle off-loop, then tell every instance to re-read it."""
        result = await asyncio.to_thread(self.set_scoreboard_enabled, enabled)
        self._announce_settings()
        return result

    def _announce_settings(self) -> None:
        if self._ctx is None:
            return
        payload = {"scoreboard_enabled": self.state["scoreboard_enabled"]}
        try:
            self._ctx.emit(SETTINGS_TOPIC, payload, cluster=True)
        except TypeError:  # core < 1.7: no cluster bus — single instance anyway
            self._ctx.emit(SETTINGS_TOPIC, payload)
        except Exception as exc:
            log.warning(f"Bingo: could not announce settings change: {exc}")

    # ── Scoreboard ───────────────────────────────────────────────────────────
    def scoreboard(self, limit: int = 10) -> List[Dict[str, Any]]:
        if not self.is_ready:
            return []
        with get_session() as s:
            rows = s.execute(
                select(BingoScore.player, BingoScore.wins)
                .where(BingoScore.wins > 0)
                .order_by(BingoScore.wins.desc(), BingoScore.player)
                .limit(limit)
            ).all()
        return [{"player": p, "wins": int(w)} for p, w in rows]

    @staticmethod
    def _increment_score(s, nick: str) -> None:
        """Atomic ``wins = wins + 1`` (insert on first win) inside session *s*."""
        res = s.execute(
            update(BingoScore).where(BingoScore.player == nick).values(wins=BingoScore.wins + 1)
        )
        if not res.rowcount:
            s.add(BingoScore(player=nick, wins=1))
            s.flush()  # a concurrent first insert raises IntegrityError → caller retries

    # ── Sessions ─────────────────────────────────────────────────────────────
    def list_sessions(self) -> List[Dict[str, Any]]:
        self._require_db()
        with get_session() as s:
            sessions = s.execute(
                select(BingoSession.id, BingoSession.name, BingoSession.size)
                .where(BingoSession.status == "open")
                .order_by(BingoSession.created_at, BingoSession.id)
            ).all()
            ids = [row.id for row in sessions]
            players: Dict[str, int] = {}
            winners: Dict[str, int] = {}
            if ids:
                players = dict(s.execute(
                    select(BingoPlayer.session_id, func.count())
                    .where(BingoPlayer.session_id.in_(ids))
                    .group_by(BingoPlayer.session_id)
                ).all())
                winners = dict(s.execute(
                    select(BingoPlayer.session_id, func.count())
                    .where(BingoPlayer.session_id.in_(ids), BingoPlayer.won.is_(True))
                    .group_by(BingoPlayer.session_id)
                ).all())
        return [
            {
                "id": row.id,
                "name": row.name,
                "size": row.size,
                "players": int(players.get(row.id, 0)),
                "winners": int(winners.get(row.id, 0)),
            }
            for row in sessions
        ]

    def count_open_sessions(self) -> int:
        self._require_db()
        with get_session() as s:
            return int(s.scalar(
                select(func.count()).select_from(BingoSession).where(BingoSession.status == "open")
            ) or 0)

    def create_session(
        self, name: str, size: int, terms: List[str], created_by: Optional[str] = None,
    ) -> str:
        name = (name or "").strip()
        size = int(size)
        terms = [t.strip() for t in (terms or []) if t.strip()]
        if not name:
            raise ValueError("Bitte einen Namen vergeben.")
        if size < 3 or size > 5:
            raise ValueError("Feldgröße muss zwischen 3 und 5 liegen.")
        if len(terms) < size * size:
            raise ValueError(f"Du brauchst mindestens {size * size} Begriffe.")
        self._require_db()
        sid = str(uuid4())[:8]

        def _insert() -> None:
            now = _utcnow()
            with get_session() as s:
                s.add(BingoSession(
                    id=sid, name=name[:255], size=size, terms=terms, status="open",
                    created_by=created_by, created_at=now, last_update=now,
                ))
                s.commit()

        retry_write(_insert)
        self.maybe_cleanup()
        return sid

    def join_session(self, sid: str, nick: str) -> None:
        nick = (nick or "").strip()
        if not nick:
            raise ValueError("Nickname fehlt.")
        self._require_db()

        def _join() -> None:
            with get_session() as s:
                sess = s.get(BingoSession, sid)
                if sess is None:
                    raise KeyError(sid)
                if s.get(BingoPlayer, {"session_id": sid, "name": nick}) is not None:
                    return
                n = sess.size * sess.size
                now = _utcnow()
                s.add(BingoPlayer(
                    session_id=sid, name=nick, board=random.sample(list(sess.terms), n),
                    marks=[False] * n, won=False, joined_at=now, updated_at=now, version=0,
                ))
                s.commit()

        try:
            retry_write(_join)
        except IntegrityError:
            pass  # the same nick joined concurrently (other instance / double click)

    def mark_cell(self, sid: str, nick: str, index: int) -> Dict[str, Any]:
        """Toggle a cell; on a fresh bingo, record the win + scoreboard.

        Returns {won, first_winner, sarcastic} so the UI can fire confetti/toast.
        """
        self._require_db()
        result: Dict[str, Any] = {"won": False, "first_winner": False, "sarcastic": None}

        def _toggle() -> str:
            with get_session() as s:
                sess = s.get(BingoSession, sid)
                if sess is None:
                    raise KeyError(sid)
                player = s.get(BingoPlayer, {"session_id": sid, "name": nick})
                if player is None:
                    raise KeyError(nick)
                if player.won:
                    return "already_won"
                size = sess.size
                marks = [bool(m) for m in player.marks]
                if index < 0 or index >= len(marks):
                    raise ValueError("Ungültiger Index.")
                marks[index] = not marks[index]
                won = check_win(_cells(player.board, marks), size)
                now = _utcnow()
                res = s.execute(
                    update(BingoPlayer)
                    .where(
                        BingoPlayer.session_id == sid,
                        BingoPlayer.name == nick,
                        BingoPlayer.version == player.version,
                    )
                    .values(
                        marks=marks, won=won, won_at=now if won else None,
                        updated_at=now, version=player.version + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount != 1:
                    raise _StaleBoard()
                s.commit()
                return "won" if won else "marked"

        outcome = "marked"
        for attempt in range(_MARK_ATTEMPTS):
            try:
                outcome = retry_write(_toggle)
                break
            except _StaleBoard:
                if attempt + 1 == _MARK_ATTEMPTS:
                    raise
                time.sleep(0.02 * (attempt + 1))

        if outcome == "already_won":
            result["won"] = True
            return result
        if outcome != "won":
            return result

        result["won"] = True
        if self._claim_first_win(sid, nick):
            result["first_winner"] = True
        else:
            result["sarcastic"] = random.choice(SARCASTIC_MESSAGES)
        return result

    def _claim_first_win(self, sid: str, nick: str) -> bool:
        """Award the session's first win at most once, cluster-wide."""
        award_score = bool(self.state["scoreboard_enabled"])

        def _claim() -> bool:
            with get_session() as s:
                res = s.execute(
                    update(BingoSession)
                    .where(BingoSession.id == sid, BingoSession.winner.is_(None))
                    .values(winner=nick, last_update=_utcnow())
                    .execution_options(synchronize_session=False)
                )
                first = res.rowcount == 1
                if first and award_score:
                    self._increment_score(s, nick)
                s.commit()
                return first

        try:
            return retry_write(_claim)
        except IntegrityError:
            # A peer inserted this player's first score row concurrently; the
            # rolled-back claim is still unclaimed, so retry once as an UPDATE.
            return retry_write(_claim)

    def session_view(self, sid: str, nick: str) -> Dict[str, Any]:
        self._require_db()
        with get_session() as s:
            sess = s.get(BingoSession, sid)
            if sess is None:
                raise KeyError(sid)
            size = sess.size
            first = sess.winner
            players = s.execute(
                select(BingoPlayer)
                .where(BingoPlayer.session_id == sid)
                .order_by(BingoPlayer.joined_at, BingoPlayer.name)
            ).scalars().all()
            rows = [
                {"name": p.name, "board": _cells(p.board, p.marks), "won": bool(p.won), "won_at": p.won_at}
                for p in players
            ]
            name = sess.name

        # Winner order: the claimed first winner, then everyone else by win time.
        later = sorted(
            (r for r in rows if r["won"] and r["name"] != first),
            key=lambda r: (r["won_at"] or datetime.max, r["name"]),
        )
        winners = ([first] if first else []) + [r["name"] for r in later]

        you = next((r for r in rows if r["name"] == nick), None)
        others: List[Dict[str, Any]] = []
        for r in rows:
            if r["name"] == nick:
                continue
            others.append({
                "nick": r["name"],
                "marks": sum(1 for c in r["board"] if c["marked"]),
                "total": size * size,
                "won": r["won"],
                "place": winners.index(r["name"]) + 1 if r["won"] and r["name"] in winners else None,
                "board": r["board"],
            })
        return {
            "id": sid,
            "name": name,
            "size": size,
            "you": {
                "nick": nick,
                "board": you["board"] if you else None,
                "won": you["won"] if you else False,
                "joined": you is not None,
            },
            "others": others,
            "winners": winners,
            "scoreboard_enabled": self.state["scoreboard_enabled"],
        }

    # ── Housekeeping ─────────────────────────────────────────────────────────
    def maybe_cleanup(self) -> None:
        """Leader-only, throttled session cleanup piggy-backed on writes."""
        if time.monotonic() - self._last_cleanup < _CLEANUP_INTERVAL:
            return
        if not getattr(self._ctx, "is_leader", True):  # single mode / older core: True
            return
        self._last_cleanup = time.monotonic()
        try:
            self.cleanup_sessions()
        except Exception as exc:
            log.warning(f"Bingo: session cleanup failed ({exc!s:.160})")

    def cleanup_sessions(self, now: Optional[datetime] = None) -> Dict[str, int]:
        """Close sessions idle for SESSION_IDLE_HOURS, delete closed ones after
        SESSION_PURGE_DAYS. Activity = session update or any player's mark/join."""
        self._require_db()
        now = now or _utcnow()
        idle_cutoff = now - timedelta(hours=SESSION_IDLE_HOURS)
        purge_cutoff = now - timedelta(days=SESSION_PURGE_DAYS)

        def _inactive(s, status: str, cutoff: datetime) -> List[str]:
            ids = list(s.scalars(
                select(BingoSession.id)
                .where(BingoSession.status == status, BingoSession.last_update < cutoff)
            ))
            if not ids:
                return []
            active = set(s.scalars(
                select(BingoPlayer.session_id)
                .where(BingoPlayer.session_id.in_(ids), BingoPlayer.updated_at >= cutoff)
                .distinct()
            ))
            return [i for i in ids if i not in active]

        def _run() -> Dict[str, int]:
            with get_session() as s:
                idle = _inactive(s, "open", idle_cutoff)
                if idle:
                    s.execute(
                        update(BingoSession).where(BingoSession.id.in_(idle))
                        .values(status="closed").execution_options(synchronize_session=False)
                    )
                stale = _inactive(s, "closed", purge_cutoff)
                if stale:
                    s.query(BingoPlayer).filter(BingoPlayer.session_id.in_(stale)).delete(
                        synchronize_session=False)
                    s.query(BingoSession).filter(BingoSession.id.in_(stale)).delete(
                        synchronize_session=False)
                s.commit()
                return {"closed": len(idle), "purged": len(stale)}

        return retry_write(_run)


bingo_service = BingoService()
