# Meeting Bingo — Dokumentation

## Übersicht

Meeting Bingo ist ein Multiplayer-„Bullshit-Bingo"-Spiel für langatmige Meetings. Spieler treten einem Raum bei, erhalten eine zufällig generierte Bingo-Karte mit Buzzwords und können diese während des Meetings abhaken. Das Plugin dient gleichzeitig als Referenzimplementierung, die zeigt, dass Lyndrix für jeden Anwendungsfall geeignet ist — von ernsthafter Infrastruktur bis hin zu Spielen.

Die gesamte Benutzeroberfläche ist als React-Frontend umgesetzt (`react_ui=True`). Es gibt keine NiceGUI-Seite. Der Spielzustand wird im Arbeitsspeicher gehalten; die Buzzword-Liste wird über Vault persistiert.

---

## Architektur

```
lyndrix-plugin-meeting-bingo/
├── entrypoint.py              # Manifest + Lifecycle-Hooks
├── locales/
│   └── bingo.<locale>.json   # i18n-Übersetzungen (Namespace: bingo)
└── app/
    ├── api.py                 # FastAPI-Router (Räume, Karten, Spielzustand)
    ├── logic/
    │   └── service.py         # BingoService: Raumverwaltung, Karten-Generierung, Spielzustand
    └── ui/
        └── react/             # React-Frontend (einzige UI; Vite-Bundle)
```

**`BingoService`** verwaltet alle aktiven Räume im Arbeitsspeicher. Die Buzzword-Liste wird beim Start aus Vault geladen und kann über die Plugin-Einstellungen (`/bingo/settings`) angepasst werden.

Der React-Client lädt die komplette Spiellogik über die REST-API; im Frontend findet keinerlei Geschäftslogik statt.

---

## API-Referenz

Alle Routen sind unter `/api/plugins/lyndrix.plugin.bingo/` erreichbar und erfordern eine gültige Authentifizierung.

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/rooms` | Liste aller aktiven Räume |
| `POST` | `/rooms` | Neuen Raum erstellen (mit Buzzword-Liste) |
| `DELETE` | `/rooms/{room_id}` | Raum beenden |
| `GET` | `/rooms/{room_id}/card` | Persönliche Bingo-Karte des angemeldeten Nutzers abrufen |
| `POST` | `/rooms/{room_id}/mark` | Buzzword auf der Karte markieren |
| `GET` | `/rooms/{room_id}/state` | Aktuellen Spielzustand abrufen |

---

## Events

Das Plugin abonniert keine Platform-Events und emittiert keine Events über den globalen Event-Bus. Die gesamte Spielkommunikation läuft über die REST-API.

| Richtung | Topic | Beschreibung |
|---|---|---|
| subscribe | `vault:ready_for_data` | Buzzword-Liste aus Vault laden |

---

## Konfiguration & Einstellungen

| Einstellung | Beschreibung | Zugriff |
|---|---|---|
| Buzzword-Liste | Komma-getrennte Liste der Buzzwords für neue Räume | `ctx.get_setting("word_list")` / Settings-UI unter `/bingo/settings` |

**`auto_enable_on_install=False`** — das Plugin benötigt vor dem ersten Einsatz eine konfigurierte Buzzword-Liste. Ohne Liste sind keine neuen Räume möglich.

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

- **Kein SQLAlchemy-Modell:** Der Spielzustand ist kurzlebig und gehört nicht in die Datenbank. Nur die Buzzword-Liste wird über Vault dauerhaft gespeichert.
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

Die Service-Schicht (`app/logic/service.py`) ist vollständig ohne laufenden Core testbar. Für Lifecycle-Tests kann `ModuleContext` gemockt werden.
