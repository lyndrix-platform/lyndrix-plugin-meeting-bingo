"""Meeting Bingo plugin — thin wiring layer (manifest + lifecycle only).

All game state and rules live in ``app/logic/service.py``; the HTTP router in
``app/api.py`` exposes them and the React bundle (``app/ui/react``) is the sole
front-end (``react_ui=True``). This module must stay logic-free wiring.
"""
import asyncio

from core.api import ModuleManifest

from .app.logic.service import bingo_service
from .app.api import build_plugin_router

# ==========================================
# 1. MANIFEST
# ==========================================
manifest = ModuleManifest(
    id="lyndrix.plugin.bingo",
    name="Meeting Bingo",
    version="0.2.0",
    description="Multiplayer Bullshit-Bingo für langatmige Meetings.",
    author="Lyndrix",
    icon="grid_on",
    type="PLUGIN",
    min_core_version="1.0.0",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-meeting-bingo",
    ui_route="/bingo",
    react_ui=True,
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
