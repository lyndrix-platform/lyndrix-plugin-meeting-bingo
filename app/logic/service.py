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
# closed ones are deleted after the purge window (leased hourly job, see start_cleanup).
SESSION_IDLE_HOURS = 24
SESSION_PURGE_DAYS = 30
_CLEANUP_INTERVAL = 3600.0
_CLEANUP_FIRST_DELAY = 30.0
_MARK_ATTEMPTS = 5

# Input limits (MB-02). Operator settings (manifest settings_schema) override
# these defaults; they exist so any ordinary user cannot bloat the DB or flood
# the lobby that every client polls.
DEFAULT_LIMITS: Dict[str, int] = {
    "max_terms": 200,
    "max_term_length": 80,
    "max_sessions_per_user": 20,
    "max_players_per_session": 50,
}

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


class InvalidInput(ValueError):
    """Input over a configured size limit (HTTP 422)."""


class QuotaExceeded(ValueError):
    """A per-user / per-session quota is used up (HTTP 429)."""


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
        self.limits: Dict[str, int] = dict(DEFAULT_LIMITS)
        self._cleanup_task: Optional[asyncio.Task] = None

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
        self.reload_limits()
        raw = self._ctx.get_secret("scoreboard_enabled")
        if raw is not None:
            self.state["scoreboard_enabled"] = raw in (True, "True")

    def reload_limits(self) -> None:
        """Re-read the size limits from the operator settings (older cores
        without ``ctx.get_setting`` keep the defaults)."""
        get_setting = getattr(self._ctx, "get_setting", None)
        if not callable(get_setting):
            return
        for key, default in DEFAULT_LIMITS.items():
            try:
                value = int(get_setting(key, default))
            except (TypeError, ValueError):
                value = default
            self.limits[key] = value if value >= 1 else default

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
        max_terms, max_len = self.limits["max_terms"], self.limits["max_term_length"]
        if len(terms) > max_terms:
            raise InvalidInput(f"Höchstens {max_terms} Begriffe pro Session erlaubt.")
        if any(len(t) > max_len for t in terms):
            raise InvalidInput(f"Ein Begriff darf höchstens {max_len} Zeichen lang sein.")
        self._require_db()
        sid = str(uuid4())[:8]
        max_sessions = self.limits["max_sessions_per_user"]

        def _insert() -> None:
            now = _utcnow()
            with get_session() as s:
                if created_by:
                    open_own = int(s.scalar(
                        select(func.count()).select_from(BingoSession)
                        .where(BingoSession.status == "open", BingoSession.created_by == created_by)
                    ) or 0)
                    if open_own >= max_sessions:
                        raise QuotaExceeded(
                            f"Du hast bereits {open_own} offene Sessions (Maximum {max_sessions})."
                        )
                s.add(BingoSession(
                    id=sid, name=name[:255], size=size, terms=terms, status="open",
                    created_by=created_by, created_at=now, last_update=now,
                ))
                s.commit()

        retry_write(_insert)
        return sid

    def join_session(self, sid: str, nick: str, owner: Optional[str] = None) -> str:
        """Join (idempotent) and return the player's name in the session.

        ``owner`` is the authenticated user (MB-01): a user has at most one
        player per session, ``nick`` is only its display name for the first
        join and is ignored afterwards. Without ``owner`` (internal/legacy
        callers) the nickname alone identifies the player.
        """
        nick = (nick or "").strip()
        if not nick:
            raise ValueError("Nickname fehlt.")
        self._require_db()
        max_players = self.limits["max_players_per_session"]

        def _join() -> str:
            with get_session() as s:
                sess = s.get(BingoSession, sid)
                if sess is None:
                    raise KeyError(sid)
                if owner is not None:
                    mine = s.scalars(
                        select(BingoPlayer).where(BingoPlayer.session_id == sid, BingoPlayer.owner == owner)
                        .order_by(BingoPlayer.joined_at, BingoPlayer.name)
                    ).first()
                    if mine is not None:
                        return mine.name
                same = s.get(BingoPlayer, {"session_id": sid, "name": nick})
                if same is not None:
                    if owner is None or same.owner == owner:
                        return nick
                    if same.owner is None:
                        # Player stored before owners existed: its nickname
                        # holder claims it (conditional, so only one user wins).
                        claimed = s.execute(
                            update(BingoPlayer)
                            .where(BingoPlayer.session_id == sid, BingoPlayer.name == nick,
                                   BingoPlayer.owner.is_(None))
                            .values(owner=owner).execution_options(synchronize_session=False)
                        ).rowcount
                        s.commit()
                        if claimed:
                            return nick
                    raise ValueError("Dieser Nickname ist in der Session bereits vergeben.")
                count = int(s.scalar(
                    select(func.count()).select_from(BingoPlayer).where(BingoPlayer.session_id == sid)
                ) or 0)
                if count >= max_players:
                    raise QuotaExceeded(f"Die Session ist voll (Maximum {max_players} Spieler).")
                n = sess.size * sess.size
                now = _utcnow()
                s.add(BingoPlayer(
                    session_id=sid, name=nick, owner=owner, board=random.sample(list(sess.terms), n),
                    marks=[False] * n, won=False, joined_at=now, updated_at=now, version=0,
                ))
                s.commit()
                return nick

        try:
            return retry_write(_join)
        except IntegrityError:
            # The same nickname joined concurrently (other instance / double
            # click): re-run, which now sees the row and decides by its owner.
            return retry_write(_join)

    def _player_name(self, s, sid: str, nick: str, owner: Optional[str]) -> str:
        """The name of the player *owner* controls in session *sid* (MB-01).

        With an owner the nickname given by the client is irrelevant: the player
        is the one stored for that user. A player from before owners existed
        (``owner IS NULL``) is claimed by the first authenticated user who
        presents its nickname. Raises ``KeyError`` when the user has no player.
        """
        if owner is None:
            return nick
        mine = s.scalars(
            select(BingoPlayer.name).where(BingoPlayer.session_id == sid, BingoPlayer.owner == owner)
            .order_by(BingoPlayer.joined_at, BingoPlayer.name)
        ).first()
        if mine is not None:
            return mine
        legacy = s.get(BingoPlayer, {"session_id": sid, "name": nick})
        if legacy is not None and legacy.owner is None:
            s.execute(
                update(BingoPlayer)
                .where(BingoPlayer.session_id == sid, BingoPlayer.name == nick, BingoPlayer.owner.is_(None))
                .values(owner=owner).execution_options(synchronize_session=False)
            )
            s.commit()
            again = s.scalars(
                select(BingoPlayer.name).where(BingoPlayer.session_id == sid, BingoPlayer.owner == owner)
            ).first()
            if again is not None:
                return again
        raise KeyError(nick)

    def mark_cell(
        self, sid: str, nick: str, index: int, owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Toggle a cell; on a fresh bingo, record the win + scoreboard.

        With ``owner`` (the authenticated user) the marked board is always that
        user's own, whatever ``nick`` says (MB-01). Returns {won, first_winner,
        sarcastic} so the UI can fire confetti/toast.
        """
        self._require_db()
        result: Dict[str, Any] = {"won": False, "first_winner": False, "sarcastic": None}
        names: Dict[str, str] = {}

        def _toggle() -> str:
            with get_session() as s:
                sess = s.get(BingoSession, sid)
                if sess is None:
                    raise KeyError(sid)
                nick = names["player"] = self._player_name(s, sid, names["requested"], owner)
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

        names["requested"] = nick
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
        if self._claim_first_win(sid, names["player"], owner):
            result["first_winner"] = True
        else:
            result["sarcastic"] = random.choice(SARCASTIC_MESSAGES)
        return result

    def _claim_first_win(self, sid: str, nick: str, owner: Optional[str] = None) -> bool:
        """Award the session's first win at most once, cluster-wide. The
        scoreboard counts per authenticated user (``owner``), not per nickname."""
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
                    self._increment_score(s, (owner or nick)[:64])
                s.commit()
                return first

        try:
            return retry_write(_claim)
        except IntegrityError:
            # A peer inserted this player's first score row concurrently; the
            # rolled-back claim is still unclaimed, so retry once as an UPDATE.
            return retry_write(_claim)

    def session_view(self, sid: str, nick: str, owner: Optional[str] = None) -> Dict[str, Any]:
        """The board view for one user. With ``owner`` "you" is the player stored
        for that user; a player from before owners existed stays readable by its
        nickname. Owners are never part of the view."""
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
                {"name": p.name, "board": _cells(p.board, p.marks), "won": bool(p.won), "won_at": p.won_at,
                 "owner": p.owner}
                for p in players
            ]
            name = sess.name

        # Winner order: the claimed first winner, then everyone else by win time.
        later = sorted(
            (r for r in rows if r["won"] and r["name"] != first),
            key=lambda r: (r["won_at"] or datetime.max, r["name"]),
        )
        winners = ([first] if first else []) + [r["name"] for r in later]

        if owner is None:
            you = next((r for r in rows if r["name"] == nick), None)
        else:
            you = next((r for r in rows if r["owner"] == owner), None) or next(
                (r for r in rows if r["name"] == nick and r["owner"] is None), None)
        others: List[Dict[str, Any]] = []
        for r in rows:
            if you is not None and r["name"] == you["name"]:
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
                "nick": you["name"] if you else nick,
                "board": you["board"] if you else None,
                "won": you["won"] if you else False,
                "joined": you is not None,
            },
            "others": others,
            "winners": winners,
            "scoreboard_enabled": self.state["scoreboard_enabled"],
        }

    # ── Housekeeping ─────────────────────────────────────────────────────────
    async def start_cleanup(self) -> None:
        """``on_lease("bingo:cleanup")`` acquired (or single instance): sweep
        sessions periodically, independent of any request (MB-03)."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        create_task = getattr(self._ctx, "create_task", None) or asyncio.get_running_loop().create_task
        self._cleanup_task = create_task(self._cleanup_loop(), name="bingo:cleanup")

    async def stop_cleanup(self) -> None:
        """Lease lost / plugin unloaded: stop sweeping on this instance."""
        task, self._cleanup_task = self._cleanup_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - we only wait for it to end
                pass

    async def _cleanup_loop(self) -> None:
        delay = _CLEANUP_FIRST_DELAY
        while True:
            await asyncio.sleep(delay)
            delay = _CLEANUP_INTERVAL
            if not self.is_ready:
                continue
            try:
                done = await asyncio.to_thread(self.cleanup_sessions)
                if done["closed"] or done["purged"]:
                    log.info(f"Bingo: cleanup closed {done['closed']} and purged {done['purged']} session(s)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one failed sweep never ends the job
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
