"""REST API for Meeting Bingo.

A single router mounted by core at ``/api/plugins/lyndrix.plugin.bingo/`` via
``ctx.register_routes()``. The registry enforces authentication. Reads use
``api:read``; every state-mutating route (create/join/mark, settings) uses
``api:write`` so the permission model matches the HTTP verb (least privilege).

The React bundle (app/ui/react) polls ``/lobby`` and ``/sessions/{sid}`` for the
real-time view and POSTs joins/marks. All endpoints delegate to the shared
``BingoService`` so logic lives in one place. Every service call touches the
database and/or Vault, so all of them are offloaded with ``asyncio.to_thread``
to keep the event loop responsive. While the database is unavailable the game
routes answer 503.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from core.api import ApiIdentity, require_permission

from .logic.service import BingoService, BingoUnavailable, InvalidInput, QuotaExceeded

# Plugin-scoped permission ids. Gating routes on the fully-qualified
# plugin:<id>:api:* (rather than bare api:read/api:write) makes the manifest's
# scoped viewer/operator roles actually unlock routes, while global api:read/
# api:write holders keep working via the core's one-directional fallback.
PLUGIN_ID = "lyndrix.plugin.bingo"
PERM_READ = f"plugin:{PLUGIN_ID}:api:read"
PERM_WRITE = f"plugin:{PLUGIN_ID}:api:write"


# Hard ceilings that reject absurd bodies before any work; the operator-tunable
# limits (service.limits, settings max_terms/max_term_length/...) apply below them.
_HARD_MAX_TERMS = 1000
_HARD_MAX_TERM_LENGTH = 500


class CreateSessionPayload(BaseModel):
    name: str = Field(max_length=255)
    size: int = 3
    terms: List[str] = Field(default_factory=list, max_length=_HARD_MAX_TERMS)

    @field_validator("terms")
    @classmethod
    def _term_length(cls, terms: List[str]) -> List[str]:
        if any(len(t) > _HARD_MAX_TERM_LENGTH for t in terms):
            raise ValueError(f"a term may have at most {_HARD_MAX_TERM_LENGTH} characters")
        return terms


class JoinPayload(BaseModel):
    """``nick`` is only the display name; the player is bound to the caller."""

    nick: str = Field(min_length=1, max_length=40)


class MarkPayload(BaseModel):
    # Accepted for older clients but ignored: the board marked is always the
    # caller's own (MB-01).
    nick: Optional[str] = Field(default=None, max_length=40)
    index: int


class SettingsPayload(BaseModel):
    scoreboard_enabled: bool


def _limit_error(exc: ValueError) -> Optional[HTTPException]:
    """422 for input over a size limit, 429 for an exhausted quota, else None."""
    if isinstance(exc, InvalidInput):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, QuotaExceeded):
        return HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "60"})
    return None


def _user(identity: ApiIdentity) -> str:
    """The authenticated user all game actions are attributed to (MB-01)."""
    return getattr(identity, "username", None) or "system"


def _unavailable(exc: BingoUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "5"})


def build_plugin_router(service: BingoService) -> APIRouter:
    router = APIRouter(tags=["Meeting Bingo"])

    # ── Lobby / scoreboard ───────────────────────────────────────────────────
    def _lobby() -> dict:
        return {
            "sessions": service.list_sessions(),
            "scoreboard_enabled": service.state["scoreboard_enabled"],
            "scoreboard": service.scoreboard(),
            "default_terms": service.default_terms(),
        }

    @router.get("/lobby")
    async def lobby(_identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        try:
            return await asyncio.to_thread(_lobby)
        except BingoUnavailable as exc:
            raise _unavailable(exc) from exc

    @router.get("/scoreboard")
    async def scoreboard(_identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        scores = await asyncio.to_thread(service.scoreboard)
        return {"enabled": service.state["scoreboard_enabled"], "scores": scores}

    # ── Sessions ─────────────────────────────────────────────────────────────
    @router.post("/sessions")
    async def create_session(
        payload: CreateSessionPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        try:
            sid = await asyncio.to_thread(
                service.create_session, payload.name, payload.size, payload.terms,
                _user(identity),
            )
        except BingoUnavailable as exc:
            raise _unavailable(exc) from exc
        except ValueError as exc:
            raise (_limit_error(exc) or HTTPException(status_code=400, detail=str(exc))) from exc
        return {"session_id": sid}

    @router.post("/sessions/{sid}/join")
    async def join(
        sid: str,
        payload: JoinPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        user = _user(identity)

        def _join() -> dict:
            name = service.join_session(sid, payload.nick, owner=user)
            return service.session_view(sid, name, owner=user)

        try:
            return await asyncio.to_thread(_join)
        except BingoUnavailable as exc:
            raise _unavailable(exc) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session nicht gefunden.") from exc
        except ValueError as exc:
            raise (_limit_error(exc) or HTTPException(status_code=400, detail=str(exc))) from exc

    @router.get("/sessions/{sid}")
    async def get_session(
        sid: str,
        nick: str = "",
        identity: ApiIdentity = Depends(require_permission(PERM_READ)),
    ):
        try:
            return await asyncio.to_thread(service.session_view, sid, nick, _user(identity))
        except BingoUnavailable as exc:
            raise _unavailable(exc) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session nicht gefunden.") from exc

    @router.post("/sessions/{sid}/mark")
    async def mark(
        sid: str,
        payload: MarkPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        user = _user(identity)

        def _mark() -> dict:
            result = service.mark_cell(sid, payload.nick or "", payload.index, owner=user)
            return {"result": result, "session": service.session_view(sid, payload.nick or "", user)}

        try:
            return await asyncio.to_thread(_mark)
        except BingoUnavailable as exc:
            raise _unavailable(exc) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session/Spieler nicht gefunden.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Settings ─────────────────────────────────────────────────────────────
    # NB: must NOT be "/settings" — core reserves /api/plugins/{id}/settings for
    # its schema-driven settings system, which would shadow this router's route.
    @router.get("/prefs")
    async def get_settings(_identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        return service.get_settings()

    @router.put("/prefs")
    async def put_settings(
        payload: SettingsPayload,
        _identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        # Persists to Vault off-loop, then announces the change cluster-wide so
        # every instance re-reads it (bingo:settings_changed).
        return await service.update_scoreboard_enabled(payload.scoreboard_enabled)

    return router
