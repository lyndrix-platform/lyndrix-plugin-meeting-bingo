# Meeting Bingo — Documentation

## Overview

Meeting Bingo is a multiplayer "bullshit bingo" game for long-winded meetings. Players join a room, receive a randomly generated bingo card with buzzwords, and can tick them off during the meeting. At the same time, the plugin serves as a reference implementation that shows that Lyndrix is suitable for every use case — from serious infrastructure to games.

The entire user interface is implemented as a React frontend (`react_ui=True`). There is no NiceGUI page. Since 1.2.0, the entire game state (rooms, players, cards, leaderboard, default terms) lives in the shared database — the plugin therefore also runs in cluster mode (multiple core instances behind a load balancer). Only the `scoreboard_enabled` toggle lives in Vault.

---

## Architecture

```
lyndrix-plugin-meeting-bingo/
├── entrypoint.py              # Manifest + lifecycle hooks
├── locales/
│   └── bingo.<locale>.json   # i18n translations (namespace: bingo)
├── tests/                     # pytest (SQLite in-memory)
└── app/
    ├── api.py                 # FastAPI router (lobby, rooms, marking, settings)
    ├── model/
    │   ├── models.py          # Tables bingo_sessions, bingo_players, bingo_scores, bingo_config
    │   └── database.py        # DDL under bootstrap lock, READ-COMMITTED sessions, retry
    ├── logic/
    │   └── service.py         # BingoService: room management, card generation, game rules
    └── ui/
        └── react/             # React frontend (only UI; Vite bundle)
```

**`BingoService`** reads and writes all rooms via the database, so that every instance sees the same lobby and the same cards.

| Table | Content |
|---|---|
| `bingo_sessions` | Room: `id`, `name`, `size`, `terms` (JSON), `status` (`open`/`closed`), `created_by`, `created_at`, `last_update`, `winner` (first winner) |
| `bingo_players` | Players per room, PK (`session_id`, `name`): `owner` (authenticated user, since 1.3.0, nullable), `board` (JSON), `marks` (JSON), `won`, `won_at`, `joined_at`, `version` |
| `bingo_scores` | Leaderboard: `player` (PK), `wins` |
| `bingo_config` | Default terms and markers for one-time imports |

Cluster rules:

- Tables are created idempotently under the core lock `lyndrix_bootstrap`; missing nullable columns of older versions (e.g. `bingo_players.owner`) are added via `ALTER TABLE` — during setup (if the DB is already connected) and on every `db:connected`.
- Write accesses are short READ-COMMITTED transactions with retry on MariaDB 1020/1205/1213. A mark applies only to the card version that was read (optimistic locking).
- The first winner is assigned via `UPDATE … WHERE winner IS NULL` — two instances can never assign it twice. The leaderboard counter increases atomically (`wins = wins + 1`).
- Rooms without activity for 24 h are closed (disappear from the lobby) and deleted after 30 days — by an hourly job under the lease `bingo:cleanup` (exactly one instance, independent of requests; since 1.3.0 no longer triggered by `create_session`).
- One-time imports: the old Vault leaderboard (`bingo_scoreboard`) and a modified `terms.txt` are imported into the DB once. Without stored terms, the built-in list `DEFAULT_TERMS` applies.

The React client loads the complete game logic via the REST API; there is no business logic at all in the frontend.

---

## API Reference

All routes are reachable under `/api/plugins/lyndrix.plugin.bingo/` and require valid authentication.

| Method | Path | Description |
|---|---|---|
| `GET` | `/lobby` | Open rooms, leaderboard, default terms |
| `GET` | `/scoreboard` | Leaderboard |
| `POST` | `/sessions` | Create a new room (`name`, `size`, `terms`); `422` when term limits are exceeded, `429` when the quota is exhausted |
| `POST` | `/sessions/{sid}/join` | Join a room (`nick` = display name only; the player is bound to the authenticated user, nick already taken → `400`, full lobby → `429`) |
| `GET` | `/sessions/{sid}?nick=` | Game state from the perspective of the authenticated user (`nick` optional) |
| `POST` | `/sessions/{sid}/mark` | Toggle a field (`index`); always marks the own card, a passed `nick` is ignored |
| `GET`/`PUT` | `/prefs` | Toggle `scoreboard_enabled` |

As long as the database is unavailable, the game routes respond with `503` (`Retry-After: 5`).

Since 1.3.0, every player is bound to the authenticated user (new column `bingo_players.owner`): one player per user and room. Players from older versions are taken over by the first authenticated join/mark that specifies their nick. The leaderboard counts the authenticated username (existing nick entries are preserved).

Size/quota violations: `422` (terms), `429` (quotas, with `Retry-After`). Static upper limits: 1000 terms, 500 characters per term.

---

## Events

Game communication runs via the REST API. The bus is only used to synchronize settings between instances.

| Direction | Topic | Description |
|---|---|---|
| subscribe | `vault:ready_for_data` | Load `scoreboard_enabled`, one-time import of the old leaderboard |
| subscribe | `db:connected` | Create tables (idempotent) |
| Lease | `bingo:cleanup` | Hourly cleanup of inactive rooms on exactly one instance (`ctx.on_lease`; older cores without leases: directly) |
| emit + subscribe | `bingo:settings_changed` | After `PUT /prefs` cluster-wide (`cluster=True`); instances with `_origin` re-read the toggle |
| subscribe | `plugin:settings_changed` | Core reports saved plugin settings → re-read the toggle |

---

## Configuration & Settings

| Setting | Description | Access |
|---|---|---|
| Scoreboard active | Counts first wins per player | `PUT /prefs` (`scoreboard_enabled`, Vault) / settings UI at `/bingo/settings` |
| Max. terms per room (`max_terms`) | Category *Limits*, default 200; above that `422` | Core settings dialog |
| Max. characters per term (`max_term_length`) | Default 80; above that `422` | Core settings dialog |
| Max. open rooms per user (`max_sessions_per_user`) | Default 20; above that `429` until a room closes (24 h inactivity) | Core settings dialog |
| Max. players per room (`max_players_per_session`) | Default 50; above that `429` | Core settings dialog |
| Default terms | Suggestion list for new rooms | Table `bingo_config` (one-time import from `terms.txt`), otherwise `DEFAULT_TERMS` |

---

## Internationalization

The plugin registers the i18n namespace `bingo`. Translation files under `locales/bingo.<locale>.json` are automatically included in the Lyndrix catalog when loaded. The React client obtains them via `GET /api/i18n/{locale}?ns=bingo`.

---

## React Routes

| Path | Description |
|---|---|
| `/bingo` | Main view: room list, game board |
| `/bingo/settings` | Settings (buzzword list, room options) |

---

## Design Decisions

- **Game state in the database (from 1.2.0):** Behind a load balancer, every request lands on an arbitrary instance; in-memory state would make rooms "disappear". That is why rooms, players and the leaderboard are kept in the shared DB.
- **React-only UI:** Meeting Bingo demonstrates the pure React plugin architecture. There is no NiceGUI page — the fallback to NiceGUI (in `entrypoint.py`) does not exist.
- **Reference plugin:** This plugin is suitable as a template for new plugins with a React-only frontend and Vault-backed configuration.

---

## Development & Testing

```bash
# From the plugin directory (lyndrix-plugin-meeting-bingo/)
pip install -r requirements-dev.txt

# Run tests
pytest

# Type checking
mypy .

# Linter
ruff check .

# Check formatter
black --check .
```

The service layer (`app/logic/service.py`) is testable without a running core: the tests replace `db_instance` with a SQLite in-memory engine and simulate two instances with two `BingoService` objects. Run in the dev container (see `lyndrix-dev/CLAUDE.md`, `PYTHONPATH=/app`).
