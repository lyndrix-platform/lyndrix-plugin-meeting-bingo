"""Cluster readiness (1.2.0): sessions, players, scoreboard and terms live in the
shared DB, so two service instances (= two core pods) see the same game; the
first win is awarded once; legacy Vault/disk data is imported once.
Runs against in-memory SQLite patched into the core's db_instance."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.logic import service as service_mod
from app.logic.service import DEFAULT_TERMS, BingoService, BingoUnavailable
from app.model import database
from app.model.models import BingoPlayer, BingoScore, BingoSession

TERMS = [f"term-{i}" for i in range(30)]


class _Ctx:
    def __init__(self, secrets=None, *, leader=True):
        self.log = logging.getLogger("bingo-test")
        self.secrets = secrets if secrets is not None else {}
        self.is_leader = leader
        self.events: list[tuple[str, dict, bool]] = []

    def get_secret(self, key):
        return self.secrets.get(key)

    def set_secret(self, key, value):
        self.secrets[key] = value
        return True

    def emit(self, topic, payload, cluster=False):
        self.events.append((topic, payload, cluster))


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    taken: list[str] = []

    @contextmanager
    def _lock(name):
        taken.append(name)
        yield True

    fake = SimpleNamespace(engine=engine, SessionLocal=sessionmaker(bind=engine),
                           is_connected=True, advisory_lock=_lock, taken=taken)
    monkeypatch.setattr(database, "db_instance", fake)
    return fake


def _pod(secrets=None, **kw) -> BingoService:
    svc = BingoService()
    svc.bind(_Ctx(secrets, **kw))
    svc.bootstrap()
    return svc


def _win_line(svc: BingoService, sid: str, nick: str) -> dict:
    """Mark the first row of *nick*'s 3x3 board; returns the last mark result."""
    result = {}
    for idx in range(3):
        result = svc.mark_cell(sid, nick, idx)
    return result


def test_bootstrap_under_lock_and_idempotent(db):
    _pod()
    _pod()
    assert db.taken.count("lyndrix_bootstrap") >= 2


def test_session_created_on_one_pod_is_playable_on_another(db):
    a, b = _pod(), _pod()
    sid = a.create_session("Daily", 3, TERMS, created_by="alice")
    assert [s["id"] for s in b.list_sessions()] == [sid]
    b.join_session(sid, "bob")
    a.join_session(sid, "carol")
    a.join_session(sid, "carol")  # idempotent
    lobby = a.list_sessions()[0]
    assert lobby == {"id": sid, "name": "Daily", "size": 3, "players": 2, "winners": 0}

    view_b = b.session_view(sid, "bob")
    view_a = a.session_view(sid, "bob")
    assert view_a["you"]["board"] == view_b["you"]["board"]  # same board on every pod
    assert view_a["you"]["joined"] is True and len(view_a["you"]["board"]) == 9
    assert [o["nick"] for o in view_a["others"]] == ["carol"]

    a.mark_cell(sid, "bob", 4)
    assert b.session_view(sid, "bob")["you"]["board"][4]["marked"] is True
    b.mark_cell(sid, "bob", 4)  # toggle back on the other pod
    assert a.session_view(sid, "bob")["you"]["board"][4]["marked"] is False


def test_unknown_session_and_player_raise_keyerror(db):
    svc = _pod()
    with pytest.raises(KeyError):
        svc.join_session("nope", "bob")
    sid = svc.create_session("X", 3, TERMS)
    with pytest.raises(KeyError):
        svc.mark_cell(sid, "ghost", 0)
    svc.join_session(sid, "bob")
    with pytest.raises(ValueError):
        svc.mark_cell(sid, "bob", 99)


def test_first_winner_awarded_once_across_pods(db):
    a, b = _pod({"scoreboard_enabled": "True"}), _pod({"scoreboard_enabled": "True"})
    a.reload_settings()
    b.reload_settings()
    sid = a.create_session("Daily", 3, TERMS)
    a.join_session(sid, "bob")
    b.join_session(sid, "carol")

    first = _win_line(a, sid, "bob")
    second = _win_line(b, sid, "carol")
    assert first["won"] and first["first_winner"] and first["sarcastic"] is None
    assert second["won"] and not second["first_winner"] and second["sarcastic"]
    assert a.mark_cell(sid, "bob", 5) == {"won": True, "first_winner": False, "sarcastic": None}

    view = b.session_view(sid, "dave")
    assert view["winners"] == ["bob", "carol"]
    assert {o["nick"]: o["place"] for o in view["others"]} == {"bob": 1, "carol": 2}
    assert view["you"]["joined"] is False
    assert a.list_sessions()[0]["winners"] == 2
    assert b.scoreboard() == [{"player": "bob", "wins": 1}]


def test_score_increment_is_atomic_and_accumulates(db):
    svc = _pod({"scoreboard_enabled": "True"})
    svc.reload_settings()
    for _ in range(3):
        sid = svc.create_session("Round", 3, TERMS)
        svc.join_session(sid, "bob")
        assert _win_line(svc, sid, "bob")["first_winner"]
    assert svc.scoreboard() == [{"player": "bob", "wins": 3}]


def test_scoreboard_disabled_awards_nothing(db):
    svc = _pod()
    sid = svc.create_session("Daily", 3, TERMS)
    svc.join_session(sid, "bob")
    assert _win_line(svc, sid, "bob")["first_winner"]
    assert svc.scoreboard() == []


def test_stale_board_write_is_retried(db, monkeypatch):
    """A concurrent mark bumped the version between our read and our write."""
    svc = _pod()
    sid = svc.create_session("Daily", 3, TERMS)
    svc.join_session(sid, "bob")
    real = service_mod.get_session
    calls = {"n": 0}

    @contextmanager
    def _racing():
        calls["n"] += 1
        if calls["n"] == 1:  # a "peer" marks cell 0 right before our first write
            with real() as s:
                s.execute(update(BingoPlayer).values(marks=[True] + [False] * 8, version=BingoPlayer.version + 1))
                s.commit()
        with real() as s:
            yield s

    monkeypatch.setattr(service_mod, "get_session", _racing)
    svc.mark_cell(sid, "bob", 1)
    monkeypatch.setattr(service_mod, "get_session", real)
    marks = [c["marked"] for c in svc.session_view(sid, "bob")["you"]["board"]]
    assert marks[:2] == [True, True]  # neither mark lost


def test_snapshot_conflict_1020_is_retried(db, monkeypatch):
    svc = _pod()
    sid = svc.create_session("Daily", 3, TERMS)
    svc.join_session(sid, "bob")
    real = service_mod.get_session
    calls = {"n": 0}

    @contextmanager
    def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("(1020, 'Record has changed since last read')")
        with real() as s:
            yield s

    monkeypatch.setattr(service_mod, "get_session", _flaky)
    svc.mark_cell(sid, "bob", 0)
    monkeypatch.setattr(service_mod, "get_session", real)
    assert svc.session_view(sid, "bob")["you"]["board"][0]["marked"] is True


def test_legacy_vault_scoreboard_imported_once(db):
    secrets = {"bingo_scoreboard": json.dumps({"bob": 4, "carol": 1})}
    a, b = _pod(secrets), _pod(secrets)
    assert a.import_legacy_scoreboard() == 2
    assert b.import_legacy_scoreboard() == 0  # marker row: no double count
    assert a.scoreboard() == [{"player": "bob", "wins": 4}, {"player": "carol", "wins": 1}]


def test_terms_from_db_with_default_fallback_and_legacy_file(db, tmp_path, monkeypatch):
    svc = _pod()
    assert svc.default_terms() == DEFAULT_TERMS
    custom = tmp_path / "terms.txt"
    custom.write_text("Alpha\nBeta\n\nGamma\n", encoding="utf-8")
    monkeypatch.setattr(service_mod, "_TERMS_FILE", str(custom))
    assert svc.import_legacy_terms() is True
    assert svc.import_legacy_terms() is False  # one-time
    assert _pod().default_terms() == ["Alpha", "Beta", "Gamma"]


def test_settings_toggle_announced_cluster_wide(db):
    svc = _pod()
    result = asyncio.run(svc.update_scoreboard_enabled(True))
    assert result == {"scoreboard_enabled": True}
    assert svc._ctx.secrets["scoreboard_enabled"] == "True"
    assert svc._ctx.events == [("bingo:settings_changed", {"scoreboard_enabled": True}, True)]

    peer = BingoService()
    peer.bind(_Ctx(svc._ctx.secrets))
    peer.reload_settings()
    assert peer.get_settings() == {"scoreboard_enabled": True}


def test_idle_sessions_closed_then_purged(db):
    svc = _pod()
    old = svc.create_session("Old", 3, TERMS)
    fresh = svc.create_session("Fresh", 3, TERMS)
    svc.join_session(old, "bob")
    long_ago = datetime.utcnow() - timedelta(days=2)
    with db.SessionLocal() as s:
        s.execute(update(BingoSession).where(BingoSession.id == old).values(last_update=long_ago))
        s.execute(update(BingoPlayer).where(BingoPlayer.session_id == old).values(updated_at=long_ago))
        s.commit()
    assert svc.cleanup_sessions() == {"closed": 1, "purged": 0}
    assert [s["id"] for s in svc.list_sessions()] == [fresh]
    assert svc.count_open_sessions() == 1
    assert svc.session_view(old, "bob")["you"]["joined"] is True  # still viewable

    assert svc.cleanup_sessions(now=datetime.utcnow() + timedelta(days=40))["purged"] == 2
    with db.SessionLocal() as s:
        assert s.scalars(select(BingoPlayer)).all() == []


def test_cleanup_is_leader_only(db):
    standby = _pod(leader=False)
    standby.maybe_cleanup()
    assert standby._last_cleanup == 0.0


def test_without_db_game_routes_are_unavailable(monkeypatch):
    monkeypatch.setattr(database, "db_instance",
                        SimpleNamespace(engine=None, SessionLocal=None, is_connected=False))
    svc = BingoService()
    svc.bind(_Ctx())
    with pytest.raises(BingoUnavailable):
        svc.list_sessions()
    assert svc.scoreboard() == []
    assert svc.default_terms() == DEFAULT_TERMS


def test_score_rows_keyed_by_player(db):
    svc = _pod({"scoreboard_enabled": "True"})
    svc.reload_settings()
    with db.SessionLocal() as s:
        s.add(BingoScore(player="bob", wins=2))
        s.commit()
    sid = svc.create_session("Daily", 3, TERMS)
    svc.join_session(sid, "bob")
    _win_line(svc, sid, "bob")
    assert svc.scoreboard() == [{"player": "bob", "wins": 3}]
