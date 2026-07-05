"""Meeting Bingo plugin — thin wiring layer (manifest + lifecycle only).

All game state and rules live in ``app/logic/service.py``; the HTTP router in
``app/api.py`` exposes them and the React bundle (``app/ui/react``) is the sole
front-end (``react_ui=True``). This module must stay logic-free wiring.
"""
import asyncio
import time

from core.api import ModuleManifest, PluginHealthStatus

from .app.logic.service import bingo_service
from .app.api import build_plugin_router

# ==========================================
# 1. MANIFEST
# ==========================================
manifest = ModuleManifest(
    id="lyndrix.plugin.bingo",
    name="Meeting Bingo",
    version="1.0.0",
    description="Multiplayer Bullshit-Bingo für langatmige Meetings.",
    author="Lyndrix",
    icon="grid_on",
    type="PLUGIN",
    min_core_version="1.0.0",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-meeting-bingo",
    # React-only plugin: the React bundle is the sole front-end (react_routes
    # below drive the React sidebar). Deliberately NO ui_route — that field is
    # the *NiceGUI* sidebar target, and declaring it made the NiceGUI shell list
    # a "Meeting Bingo" link to a page that was never registered (hard 404).
    react_ui=True,
    # i18next-shaped namespace served to the React UI; core auto-registers
    # locales/bingo.<locale>.json and adds "bingo" to the client allowlist.
    i18n_namespace="bingo",
    react_routes=[
        {
            "path": "/bingo",
            "label": "Meeting Bingo",
            "icon": "grid_on",
            "sidebar_visible": True,
        },
        {
            "path": "/bingo/settings",
            "label": "Meeting Bingo Einstellungen",
            "icon": "settings",
            "sidebar_visible": False,
        },
    ],
    settings_ui_route="/bingo/settings",
    permissions={"subscribe": ["vault:ready_for_data"], "emit": []},
    # Identity & Permissions 2.0 role bundles. Permissions are fully qualified
    # (plugin:lyndrix.plugin.bingo:api:*) rather than the bare global
    # api:read/api:write, so granting a role only ever scopes access to this
    # plugin.
    roles=[
        {"id": "viewer", "label": "Meeting Bingo Viewer",
         "permissions": ["plugin:lyndrix.plugin.bingo:api:read"],
         "auto_map_groups": [],
         "description": "Read-only access to Meeting Bingo boards and games."},
        {"id": "operator", "label": "Meeting Bingo Operator",
         "permissions": ["plugin:lyndrix.plugin.bingo:api:read", "plugin:lyndrix.plugin.bingo:api:write"],
         "auto_map_groups": [],
         "description": "Play and manage Meeting Bingo games."},
        {"id": "admin", "label": "Meeting Bingo Administrator",
         "permissions": ["plugin:lyndrix.plugin.bingo:api:read", "plugin:lyndrix.plugin.bingo:api:write"],
         "auto_map_groups": ["INT_ADMIN"],
         "description": "Full control over the Meeting Bingo plugin."},
    ],
)


# ==========================================
# 2. SETUP / LIFECYCLE
# ==========================================
def setup(ctx):
    ctx.log.info("STARTUP: Loading Meeting Bingo Plugin...")

    # Bind the service to ctx (Vault access) and mount the React bundle's API.
    # Registry mounts it at /api/plugins/lyndrix.plugin.bingo/ and enforces auth.
    bingo_service.bind(ctx)
    ctx.register_routes(build_plugin_router(bingo_service))

    # Seed the default terms file (no-op if it already exists).
    bingo_service.ensure_terms_file()

    @ctx.subscribe("vault:ready_for_data")
    async def load_data_from_vault(payload=None):
        ctx.log.info("LOAD: Vault ready. Fetching Bingo data...")
        # load_from_vault performs synchronous hvac round-trips — offload it so
        # the boot event loop is never blocked.
        await asyncio.to_thread(bingo_service.load_from_vault)


# ==========================================
# 3. HEALTH — functional liveness probe
# ==========================================
async def health(ctx) -> PluginHealthStatus:
    """Functional health probe.

    Meeting Bingo holds no DB — its runtime is the in-memory game engine plus
    the word catalog it deals boards from. So a meaningful probe checks exactly
    that: the service is bound to a context, its game-state structure is intact,
    and a non-empty term list is loadable (from disk, with a built-in fallback).
    An engine that can't deal a board is broken even though ``setup()`` ran.
    The disk read is offloaded to keep the event loop free.
    """
    start = time.perf_counter()

    if getattr(bingo_service, "_ctx", None) is None:
        return PluginHealthStatus(status="error", details={"reason": "service_not_bound"})

    state = getattr(bingo_service, "state", None)
    required_keys = {"sessions", "scoreboard_enabled", "scoreboard"}
    if not isinstance(state, dict) or not required_keys.issubset(state.keys()):
        return PluginHealthStatus(
            status="error",
            details={"reason": "state_corrupted", "state_keys": sorted(state.keys()) if isinstance(state, dict) else None},
        )

    try:
        terms = await asyncio.to_thread(bingo_service.default_terms)
    except Exception as exc:
        return PluginHealthStatus(
            status="error",
            details={"reason": "terms_unavailable", "error": str(exc)},
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )

    latency = round((time.perf_counter() - start) * 1000, 1)
    sessions = state.get("sessions") or {}
    details = {
        "terms_available": len(terms),
        "active_sessions": len(sessions) if hasattr(sessions, "__len__") else 0,
        "scoreboard_enabled": bool(state.get("scoreboard_enabled")),
    }
    # A standard bingo board needs 24 distinct terms (5x5 minus the free centre).
    if len(terms) < 24:
        return PluginHealthStatus(
            status="degraded",
            details={**details, "reason": "insufficient_terms", "minimum_required": 24},
            latency_ms=latency,
        )
    return PluginHealthStatus(status="ok", details=details, latency_ms=latency)
