"""REST API for Meeting Bingo.

A single router mounted by core at ``/api/plugins/lyndrix.plugin.bingo/`` via
``ctx.register_routes()``. The registry enforces authentication; gameplay uses
``api:read`` (any authenticated user may play), settings use ``api:write``.

The React bundle (src/ui) polls ``/lobby`` and ``/sessions/{sid}`` for the
real-time view — same cadence as the NiceGUI 1 s timers — and POSTs joins/marks.
All endpoints delegate to the shared ``BingoService`` so logic lives in one place.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api import ApiIdentity, require_permission

from .controller.service import BingoService


class CreateSessionPayload(BaseModel):
    name: str
    size: int = 3
    terms: List[str] = Field(default_factory=list)


class JoinPayload(BaseModel):
    nick: str


class MarkPayload(BaseModel):
    nick: str
    index: int


class SettingsPayload(BaseModel):
    scoreboard_enabled: bool


def build_plugin_router(service: BingoService) -> APIRouter:
    router = APIRouter(tags=["Meeting Bingo"])

    # ── Lobby / scoreboard ───────────────────────────────────────────────────
    @router.get("/lobby")
    async def lobby(_identity: ApiIdentity = Depends(require_permission("api:read"))):
        return {
            "sessions": service.list_sessions(),
            "scoreboard_enabled": service.state["scoreboard_enabled"],
            "scoreboard": service.scoreboard(),
            "default_terms": service.default_terms(),
        }

    @router.get("/scoreboard")
    async def scoreboard(_identity: ApiIdentity = Depends(require_permission("api:read"))):
        return {"enabled": service.state["scoreboard_enabled"], "scores": service.scoreboard()}

    # ── Sessions ─────────────────────────────────────────────────────────────
    @router.post("/sessions")
    async def create_session(
        payload: CreateSessionPayload,
        _identity: ApiIdentity = Depends(require_permission("api:read")),
    ):
        try:
            sid = service.create_session(payload.name, payload.size, payload.terms)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session_id": sid}

    @router.post("/sessions/{sid}/join")
    async def join(
        sid: str,
        payload: JoinPayload,
        _identity: ApiIdentity = Depends(require_permission("api:read")),
    ):
        try:
            service.join_session(sid, payload.nick)
            return service.session_view(sid, payload.nick)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session nicht gefunden.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/{sid}")
    async def get_session(
        sid: str,
        nick: str,
        _identity: ApiIdentity = Depends(require_permission("api:read")),
    ):
        try:
            return service.session_view(sid, nick)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session nicht gefunden.") from exc

    @router.post("/sessions/{sid}/mark")
    async def mark(
        sid: str,
        payload: MarkPayload,
        _identity: ApiIdentity = Depends(require_permission("api:read")),
    ):
        try:
            result = service.mark_cell(sid, payload.nick, payload.index)
            return {"result": result, "session": service.session_view(sid, payload.nick)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session/Spieler nicht gefunden.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Settings ─────────────────────────────────────────────────────────────
    # NB: must NOT be "/settings" — core reserves /api/plugins/{id}/settings for
    # its schema-driven settings system, which would shadow this router's route.
    @router.get("/prefs")
    async def get_settings(_identity: ApiIdentity = Depends(require_permission("api:read"))):
        return service.get_settings()

    @router.put("/prefs")
    async def put_settings(
        payload: SettingsPayload,
        _identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        return service.set_scoreboard_enabled(payload.scoreboard_enabled)

    return router
