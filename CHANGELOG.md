# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-10-15
### Changed
- Cluster readiness: game sessions and players moved from process memory into the
  shared database (`bingo_sessions`, `bingo_players`), so a session created on one
  core instance is visible and playable on every other one. Joins and marks are
  short READ COMMITTED transactions retried on MariaDB 1020/1205/1213; a mark
  applies only to the board version it read (optimistic lock); the first winner is
  claimed with `UPDATE … WHERE winner IS NULL`, so two instances can never both
  award it. Tables are created idempotently under the core's `lyndrix_bootstrap`
  advisory lock, at setup when the DB is already connected and on `db:connected`.
- Scoreboard moved from a whole-dict Vault write into `bingo_scores` with an atomic
  `wins = wins + 1`; the legacy Vault value `bingo_scoreboard` is imported once.
- Default terms are read from `bingo_config` (fallback `DEFAULT_TERMS`); an
  operator-edited `terms.txt` is imported once. The plugin no longer writes to its
  local disk.
- `PUT /prefs` announces `bingo:settings_changed` with `cluster=True`; peers (and
  `plugin:settings_changed` for this plugin) re-read `scoreboard_enabled` from Vault.
- `health()` counts open sessions from the DB (cluster-wide) and reports
  `degraded` / `database_not_ready` until the tables exist.
- All API routes offload service calls with `asyncio.to_thread`; game routes answer
  503 (`Retry-After: 5`) while the database is unavailable.

### Added
- Leader-only session housekeeping (at most hourly, piggy-backed on session
  creation): sessions idle for 24 h are closed (hidden from the lobby, still
  viewable), closed sessions are deleted after 30 days.
- `created_by` (API identity) is stored with each session.
- Test suite (`tests/`, SQLite in-memory, two service instances as two pods).

### Removed
- `BingoService.ensure_terms_file()` and the in-memory `sessions` / `scoreboard`
  state.
