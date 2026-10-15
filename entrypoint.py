"""Meeting Bingo plugin — thin wiring layer (manifest + lifecycle only).

All game state and rules live in ``app/logic/service.py``; the HTTP router in
``app/api.py`` exposes them and the React bundle (``app/ui/react``) is the sole
front-end (``react_ui=True``). This module must stay logic-free wiring.

Cluster-safe since 1.2.0: sessions, players, scoreboard and default terms live
in the shared database; the Vault-backed ``scoreboard_enabled`` toggle is
re-read on every instance via ``bingo:settings_changed`` (cluster bus).
"""
import asyncio
import time

from core.api import ModuleManifest, PluginHealthStatus, db_instance

from .app.logic.service import SETTINGS_TOPIC, bingo_service
from .app.api import build_plugin_router

# ==========================================
# 1. MANIFEST
# ==========================================
manifest = ModuleManifest(
    id="lyndrix.plugin.bingo",
    name="Meeting Bingo",
    version="1.2.0",
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
    permissions={
        "subscribe": ["vault:ready_for_data", "db:connected", "plugin:settings_changed", SETTINGS_TOPIC],
        "emit": [SETTINGS_TOPIC],
    },
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
def _spawn(ctx, coro) -> None:
    """Start background work via ctx.create_task (older cores: the running loop)."""
    create_task = getattr(ctx, "create_task", None)
    if callable(create_task):
        create_task(coro, name="bingo:bootstrap")
    else:
        asyncio.get_running_loop().create_task(coro)


def setup(ctx):
    ctx.log.info("STARTUP: Loading Meeting Bingo Plugin...")

    # Bind the service to ctx (Vault access) and mount the React bundle's API.
    # Registry mounts it at /api/plugins/lyndrix.plugin.bingo/ and enforces auth.
    bingo_service.bind(ctx)
    ctx.register_routes(build_plugin_router(bingo_service))

    # db:connected / vault:ready_for_data usually fired BEFORE setup() (they
    # trigger plugin activation), so bootstrap right away in the background:
    # tables under the core's bootstrap lock, one-time legacy imports, toggle.
    async def _initial_bootstrap():
        if db_instance.is_connected:
            await bingo_service.abootstrap()
        await asyncio.to_thread(bingo_service.load_from_vault)

    _spawn(ctx, _initial_bootstrap())

    @ctx.subscribe("db:connected")
    async def _on_db_connected(payload=None):
        if await bingo_service.abootstrap():
            await asyncio.to_thread(bingo_service.load_from_vault)

    @ctx.subscribe("vault:ready_for_data")
    async def load_data_from_vault(payload=None):
        ctx.log.info("LOAD: Vault ready. Fetching Bingo data...")
        # Synchronous hvac/DB round-trips — offload so the loop is never blocked.
        await asyncio.to_thread(bingo_service.load_from_vault)

    # A peer instance changed the toggle (our own emit has no _origin).
    @ctx.subscribe(SETTINGS_TOPIC)
    async def _on_peer_settings(payload=None):
        if isinstance(payload, dict) and payload.get("_origin"):
            await asyncio.to_thread(bingo_service.reload_settings)

    # Core >= 1.5.1 (bridged to every instance): this plugin's settings were saved.
    @ctx.subscribe("plugin:settings_changed")
    async def _on_settings_saved(payload=None):
        if isinstance(payload, dict) and payload.get("plugin_id") == manifest.id:
            await asyncio.to_thread(bingo_service.reload_settings)


# ==========================================
# 3. HEALTH — functional liveness probe
# ==========================================
async def health(ctx) -> PluginHealthStatus:
    """Functional health probe.

    The game state lives in the shared database, so the probe checks that the
    service is bound, its tables are bootstrapped, the open sessions can be
    counted (one cheap COUNT — cluster-wide, not just this instance) and a
    term list is available to deal boards from (DB, built-in fallback).
    Blocking reads are offloaded to keep the event loop free.
    """
    start = time.perf_counter()

    if getattr(bingo_service, "_ctx", None) is None:
        return PluginHealthStatus(status="error", details={"reason": "service_not_bound"})

    if not bingo_service.is_ready:
        return PluginHealthStatus(
            status="degraded",
            details={"reason": "database_not_ready"},
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )

    try:
        terms = await asyncio.to_thread(bingo_service.default_terms)
        active_sessions = await asyncio.to_thread(bingo_service.count_open_sessions)
    except Exception as exc:
        return PluginHealthStatus(
            status="error",
            details={"reason": "query_failed", "error": str(exc)},
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )

    latency = round((time.perf_counter() - start) * 1000, 1)
    details = {
        "terms_available": len(terms),
        "active_sessions": active_sessions,
        "scoreboard_enabled": bool(bingo_service.state.get("scoreboard_enabled")),
    }
    # A standard bingo board needs 24 distinct terms (5x5 minus the free centre).
    if len(terms) < 24:
        return PluginHealthStatus(
            status="degraded",
            details={**details, "reason": "insufficient_terms", "minimum_required": 24},
            latency_ms=latency,
        )
    return PluginHealthStatus(status="ok", details=details, latency_ms=latency)
