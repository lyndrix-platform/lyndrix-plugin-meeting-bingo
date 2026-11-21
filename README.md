# Lyndrix Meeting Bingo Plugin

**Version:** 1.3.0  
**Author:** Lyndrix

A multiplayer bullshit-bingo plugin that integrates seamlessly into the Lyndrix platform. Perfect for livening up long-winded meetings with a bit of gamification.

## 📋 Features

- **Multiplayer Lobby**: Create bingo sessions with an adjustable board size (3x3 to 5x5).
- **Real-time Sync**: See live how your colleagues mark their fields (and who is close to winning).
- **Wall of Shame**: An optional scoreboard that persistently stores the winners in the Lyndrix Vault.
- **Sarcastic Comments**: The system comments on your performance (or lack thereof).
- **Built-in Terms**: Comes with a curated list of buzzwords (`terms.txt`) that can be adjusted per session.

## 🚀 Installation

Since this is a plugin for `lyndrix-core`, it must be installed in the plugin directory of the main application.

1. Navigate to the plugin directory of your `lyndrix-core` installation:
   ```bash
   cd /path/to/lyndrix-core/plugins
   ```

2. Clone this repository:
   ```bash
   git clone https://github.com/lyndrix-platform/lyndrix-plugin-meeting-bingo.git lyndrix.plugin.bingo
   ```
   *Note: The target folder name `lyndrix.plugin.bingo` is recommended so that the ID in the manifest matches cleanly.*

3. Restart the Lyndrix application. The plugin is loaded automatically and is reachable at the route `/bingo`.

## ⚙️ Configuration

The plugin uses Lyndrix's internal `ctx` API for settings and secrets.

### Scoreboard (Wall of Shame)
By default, persistently storing winners is disabled (for "ethical" reasons).
To enable it:
1. Open the plugin in the UI.
2. Scroll to the settings.
3. Enable the switch **"Enable scoreboard"**.
4. The data is stored securely in the Vault under the key `bingo_scoreboard`.

## 🛠 Development & Structure

- `entrypoint.py`: Thin wiring layer (manifest + lifecycle), no business logic.
- `app/logic/service.py`: Game state and rules (single source of truth).
- `app/api.py`: REST router (`build_plugin_router`), mounted under `/api/plugins/lyndrix.plugin.bingo/`.
- `app/ui/react/`: React frontend (`react_ui=True`), built to `app/ui/static/ui_bundle.js`.
- `terms.txt`: Default list of buzzwords (recreated if deleted).

### Build (Frontend)
```bash
npm install
npm run build   # -> app/ui/static/ui_bundle.js
```

### Dependencies
The plugin uses exclusively the stable `core.api` surface of `lyndrix-core`
(manifest, router registration, Vault access via `ctx`). The frontend is a
locally bundled React app without external CDN runtime dependencies.

## 📝 License
Internal tool. Use at your own risk during official meetings.
