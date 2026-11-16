"""MB-01 identity binding, MB-02 limits, MB-03 leased cleanup."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.api import PERM_READ, PERM_WRITE, build_plugin_router
from app.logic.service import InvalidInput, QuotaExceeded
from app.model.models import BingoPlayer, BingoSession
from core.api import ApiIdentity

from .test_cluster import TERMS, _pod, db  # noqa: F401  (fixture)


def _identity(name: str) -> ApiIdentity:
    return ApiIdentity(method="session", username=name, permissions=frozenset({PERM_READ, PERM_WRITE}))


@pytest.fixture()
def api(db, monkeypatch):
    from core.api import security

    svc = _pod({"scoreboard_enabled": "True"})
    svc.reload_settings()
    current = {"identity": _identity("alice")}
    monkeypatch.setattr(security, "require_api_auth", lambda request: current["identity"])
    app = FastAPI()
    app.include_router(build_plugin_router(svc))
    http = TestClient(app)
    http.as_ = lambda name: current.__setitem__("identity", _identity(name)) or http  # type: ignore[attr-defined]
    http.svc = svc  # type: ignore[attr-defined]
    return http


def _create(http, **kw):
    body = {"name": "Daily", "size": 3, "terms": TERMS, **kw}
    return http.post("/sessions", json=body)


# ── MB-01 ──────────────────────────────────────────────────────────────────────

def test_marks_always_hit_the_callers_own_board(api):
    sid = _create(api).json()["session_id"]
    api.as_("alice").post(f"/sessions/{sid}/join", json={"nick": "Ali"})
    api.as_("bob").post(f"/sessions/{sid}/join", json={"nick": "Bobby"})

    # Bob claims to be "Ali": his own board is marked, Alice's stays untouched.
    res = api.as_("bob").post(f"/sessions/{sid}/mark", json={"nick": "Ali", "index": 0})
    assert res.status_code == 200
    assert res.json()["session"]["you"]["nick"] == "Bobby"
    alice = api.as_("alice").get(f"/sessions/{sid}", params={"nick": "Bobby"}).json()
    assert alice["you"]["nick"] == "Ali"
    assert not any(c["marked"] for c in alice["you"]["board"])
    assert [o["nick"] for o in alice["others"]] == ["Bobby"]
    assert "owner" not in str(alice)

    # No player of her own → 404, never somebody else's board.
    assert api.as_("mallory").post(f"/sessions/{sid}/mark", json={"nick": "Ali", "index": 0}).status_code == 404


def test_join_binds_user_and_rejects_taken_nick(api):
    sid = _create(api).json()["session_id"]
    first = api.as_("alice").post(f"/sessions/{sid}/join", json={"nick": "Ali"}).json()
    again = api.as_("alice").post(f"/sessions/{sid}/join", json={"nick": "Other"}).json()
    assert again["you"]["nick"] == "Ali" and again["you"]["board"] == first["you"]["board"]
    assert api.as_("bob").post(f"/sessions/{sid}/join", json={"nick": "Ali"}).status_code == 400


def test_scoreboard_counts_the_authenticated_user_not_the_nick(api):
    sid = _create(api).json()["session_id"]
    api.as_("bob").post(f"/sessions/{sid}/join", json={"nick": "alice"})  # display name of someone else
    for i in range(3):
        api.as_("bob").post(f"/sessions/{sid}/mark", json={"index": i})
    assert api.svc.scoreboard() == [{"player": "bob", "wins": 1}]


def test_legacy_players_stay_readable_and_are_claimed_once(db):
    svc = _pod()
    sid = svc.create_session("Old", 3, TERMS)
    svc.join_session(sid, "bob")  # stored without owner (pre-1.3 data)
    assert svc.session_view(sid, "bob", owner="carol")["you"]["joined"] is True  # readable
    svc.mark_cell(sid, "bob", 0, owner="carol")  # first authenticated mark claims it
    with db.SessionLocal() as s:
        assert s.scalars(select(BingoPlayer.owner)).all() == ["carol"]
    with pytest.raises(KeyError):
        svc.mark_cell(sid, "bob", 1, owner="mallory")  # claimed: no longer takeover-able


def test_missing_owner_column_is_added_to_an_old_table(db):
    from sqlalchemy import inspect, text

    from app.model import database

    _pod()
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE bingo_players DROP COLUMN owner"))
    database.ensure_tables()
    assert "owner" in {c["name"] for c in inspect(db.engine).get_columns("bingo_players")}


# ── MB-02 ──────────────────────────────────────────────────────────────────────

def test_term_limits_are_422(api):
    api.svc.limits.update(max_terms=30, max_term_length=10)
    assert _create(api, terms=[f"t{i}" for i in range(31)]).status_code == 422
    assert _create(api, terms=TERMS[:8] + ["x" * 11]).status_code == 422
    assert _create(api, terms=[f"t{i}" for i in range(30)]).status_code == 200
    # absurd bodies are rejected by the static ceiling before any work
    assert _create(api, terms=["a"] * 1001).status_code == 422
    assert _create(api, terms=["a" * 501] * 9).status_code == 422


def test_sessions_per_user_cap_is_429(api):
    api.svc.limits["max_sessions_per_user"] = 2
    assert _create(api).status_code == 200 and _create(api).status_code == 200
    third = _create(api)
    assert third.status_code == 429
    assert _create(api.as_("bob")).status_code == 200  # per user, not global


def test_players_per_session_cap_is_429(api):
    api.svc.limits["max_players_per_session"] = 2
    sid = _create(api).json()["session_id"]
    for user in ("a", "b"):
        assert api.as_(user).post(f"/sessions/{sid}/join", json={"nick": user}).status_code == 200
    assert api.as_("c").post(f"/sessions/{sid}/join", json={"nick": "c"}).status_code == 429
    # an existing player re-joining is not a new seat
    assert api.as_("a").post(f"/sessions/{sid}/join", json={"nick": "a"}).status_code == 200


def test_limits_come_from_settings(db):
    svc = _pod()
    svc._ctx.get_setting = lambda key, default=None: {"max_terms": "25", "max_sessions_per_user": "abc",
                                                      "max_term_length": "0"}.get(key, default)
    svc.reload_limits()
    assert svc.limits["max_terms"] == 25
    assert svc.limits["max_sessions_per_user"] == 20  # invalid → default
    assert svc.limits["max_term_length"] == 80        # < 1 → default
    with pytest.raises(InvalidInput):
        svc.create_session("x", 3, [f"t{i}" for i in range(26)])
    assert issubclass(QuotaExceeded, ValueError)


# ── MB-03 ──────────────────────────────────────────────────────────────────────

def test_cleanup_runs_without_any_create(db, monkeypatch):
    from app.logic import service as service_mod

    svc = _pod()
    old = svc.create_session("Old", 3, TERMS)
    long_ago = datetime.utcnow() - timedelta(days=2)
    with db.SessionLocal() as s:
        s.execute(update(BingoSession).where(BingoSession.id == old).values(last_update=long_ago))
        s.commit()
    monkeypatch.setattr(service_mod, "_CLEANUP_FIRST_DELAY", 0.0)
    monkeypatch.setattr(service_mod, "_CLEANUP_INTERVAL", 3600.0)

    async def _run():
        await svc.start_cleanup()
        for _ in range(50):
            await asyncio.sleep(0.02)
            if svc.count_open_sessions() == 0:
                break
        await svc.stop_cleanup()

    asyncio.run(_run())
    assert svc.count_open_sessions() == 0
    assert svc._cleanup_task is None


def test_cleanup_stops_when_the_lease_is_lost(db):
    svc = _pod()

    async def _run():
        await svc.start_cleanup()
        task = svc._cleanup_task
        await svc.start_cleanup()  # idempotent
        assert svc._cleanup_task is task
        await svc.stop_cleanup()
        return task

    task = asyncio.run(_run())
    assert task.cancelled() or task.done()
