# Meeting Bingo — Dokumentation

## Übersicht

Meeting Bingo ist ein Multiplayer-„Bullshit-Bingo"-Spiel für langatmige Meetings. Spieler treten einem Raum bei, erhalten eine zufällig generierte Bingo-Karte mit Buzzwords und können diese während des Meetings abhaken. Das Plugin dient gleichzeitig als Referenzimplementierung, die zeigt, dass Lyndrix für jeden Anwendungsfall geeignet ist — von ernsthafter Infrastruktur bis hin zu Spielen.

Die gesamte Benutzeroberfläche ist als React-Frontend umgesetzt (`react_ui=True`). Es gibt keine NiceGUI-Seite. Seit 1.2.0 liegt der gesamte Spielzustand (Räume, Spieler, Karten, Bestenliste, Standard-Begriffe) in der gemeinsamen Datenbank — das Plugin läuft damit auch im Cluster-Betrieb (mehrere Core-Instanzen hinter einem Load-Balancer). Nur der Schalter `scoreboard_enabled` liegt in Vault.

---

## Architektur

```
lyndrix-plugin-meeting-bingo/
├── entrypoint.py              # Manifest + Lifecycle-Hooks
├── locales/
│   └── bingo.<locale>.json   # i18n-Übersetzungen (Namespace: bingo)
├── tests/                     # pytest (SQLite in-memory)
└── app/
    ├── api.py                 # FastAPI-Router (Lobby, Räume, Markieren, Einstellungen)
    ├── model/
    │   ├── models.py          # Tabellen bingo_sessions, bingo_players, bingo_scores, bingo_config
    │   └── database.py        # DDL unter Bootstrap-Lock, READ-COMMITTED-Sessions, Retry
    ├── logic/
    │   └── service.py         # BingoService: Raumverwaltung, Karten-Generierung, Spielregeln
    └── ui/
        └── react/             # React-Frontend (einzige UI; Vite-Bundle)
```

**`BingoService`** liest und schreibt alle Räume über die Datenbank, sodass jede Instanz dieselbe Lobby und dieselben Karten sieht.

| Tabelle | Inhalt |
|---|---|
| `bingo_sessions` | Raum: `id`, `name`, `size`, `terms` (JSON), `status` (`open`/`closed`), `created_by`, `created_at`, `last_update`, `winner` (erster Gewinner) |
| `bingo_players` | Spieler je Raum, PK (`session_id`, `name`): `board` (JSON), `marks` (JSON), `won`, `won_at`, `joined_at`, `version` |
| `bingo_scores` | Bestenliste: `player` (PK), `wins` |
| `bingo_config` | Standard-Begriffe und Marker für Einmal-Importe |

Cluster-Regeln:

- Tabellen werden idempotent unter dem Core-Lock `lyndrix_bootstrap` angelegt — beim Setup (falls die DB schon verbunden ist) und bei jedem `db:connected`.
- Schreibzugriffe sind kurze READ-COMMITTED-Transaktionen mit Retry bei MariaDB 1020/1205/1213. Ein Markieren gilt nur für die gelesene Kartenversion (optimistisches Locking).
- Der erste Gewinner wird per `UPDATE … WHERE winner IS NULL` vergeben — zwei Instanzen können ihn nie doppelt vergeben. Der Bestenlisten-Zähler steigt atomar (`wins = wins + 1`).
- Räume ohne Aktivität seit 24 h werden geschlossen (verschwinden aus der Lobby) und nach 30 Tagen gelöscht — nur durch die Leader-Instanz, höchstens stündlich.
- Einmal-Importe: die alte Vault-Bestenliste (`bingo_scoreboard`) und eine geänderte `terms.txt` werden einmalig in die DB übernommen. Ohne gespeicherte Begriffe gilt die eingebaute Liste `DEFAULT_TERMS`.

Der React-Client lädt die komplette Spiellogik über die REST-API; im Frontend findet keinerlei Geschäftslogik statt.

---

## API-Referenz

Alle Routen sind unter `/api/plugins/lyndrix.plugin.bingo/` erreichbar und erfordern eine gültige Authentifizierung.

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/lobby` | Offene Räume, Bestenliste, Standard-Begriffe |
| `GET` | `/scoreboard` | Bestenliste |
| `POST` | `/sessions` | Neuen Raum erstellen (`name`, `size`, `terms`) |
| `POST` | `/sessions/{sid}/join` | Raum beitreten (`nick`) |
| `GET` | `/sessions/{sid}?nick=` | Spielzustand aus Sicht eines Spielers |
| `POST` | `/sessions/{sid}/mark` | Feld umschalten (`nick`, `index`) |
| `GET`/`PUT` | `/prefs` | Schalter `scoreboard_enabled` |

Solange die Datenbank nicht verfügbar ist, antworten die Spiel-Routen mit `503` (`Retry-After: 5`).

---

## Events

Die Spielkommunikation läuft über die REST-API. Der Bus dient nur dem Abgleich der Einstellungen zwischen Instanzen.

| Richtung | Topic | Beschreibung |
|---|---|---|
| subscribe | `vault:ready_for_data` | `scoreboard_enabled` laden, Einmal-Import der alten Bestenliste |
| subscribe | `db:connected` | Tabellen anlegen (idempotent) |
| emit + subscribe | `bingo:settings_changed` | Nach `PUT /prefs` clusterweit (`cluster=True`); Instanzen mit `_origin` lesen den Schalter neu |
| subscribe | `plugin:settings_changed` | Core meldet gespeicherte Plugin-Einstellungen → Schalter neu lesen |

---

## Konfiguration & Einstellungen

| Einstellung | Beschreibung | Zugriff |
|---|---|---|
| Bestenliste aktiv | Zählt erste Siege je Spieler | `PUT /prefs` (`scoreboard_enabled`, Vault) / Settings-UI unter `/bingo/settings` |
| Standard-Begriffe | Vorschlagsliste für neue Räume | Tabelle `bingo_config` (Einmal-Import aus `terms.txt`), sonst `DEFAULT_TERMS` |

---

## Internationaliserung

Das Plugin registriert den i18n-Namespace `bingo`. Übersetzungsdateien unter `locales/bingo.<locale>.json` werden automatisch beim Laden in den Lyndrix-Katalog aufgenommen. Der React-Client bezieht sie über `GET /api/i18n/{locale}?ns=bingo`.

---

## React-Routen

| Pfad | Beschreibung |
|---|---|
| `/bingo` | Hauptansicht: Raumliste, Spielfeld |
| `/bingo/settings` | Einstellungen (Buzzword-Liste, Raumoptionen) |

---

## Design-Entscheidungen

- **Spielzustand in der Datenbank (ab 1.2.0):** Hinter einem Load-Balancer landet jede Anfrage auf einer beliebigen Instanz; ein Zustand im Arbeitsspeicher würde Räume „verschwinden" lassen. Deshalb liegen Räume, Spieler und Bestenliste in der gemeinsamen DB.
- **React-only UI:** Meeting Bingo demonstriert die reine React-Plugin-Architektur. Es gibt keine NiceGUI-Seite — der Fallback auf NiceGUI (in `entrypoint.py`) existiert nicht.
- **Referenz-Plugin:** Dieses Plugin eignet sich als Vorlage für neue Plugins mit React-only-Frontend und Vault-gestützter Konfiguration.

---

## Entwicklung & Tests

```bash
# Aus dem Plugin-Verzeichnis (lyndrix-plugin-meeting-bingo/)
pip install -r requirements-dev.txt

# Tests ausführen
pytest

# Typprüfung
mypy .

# Linter
ruff check .

# Formatter prüfen
black --check .
```

Die Service-Schicht (`app/logic/service.py`) ist ohne laufenden Core testbar: die Tests ersetzen `db_instance` durch eine SQLite-In-Memory-Engine und simulieren zwei Instanzen mit zwei `BingoService`-Objekten. Im Dev-Container ausführen (siehe `lyndrix-dev/CLAUDE.md`, `PYTHONPATH=/app`).
