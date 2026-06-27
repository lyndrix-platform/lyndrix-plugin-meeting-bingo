# Lyndrix Meeting Bingo Plugin

**Version:** 0.1.3  
**Autor:** Lyndrix

Ein Multiplayer Bullshit-Bingo Plugin, das nahtlos in die Lyndrix-Plattform integriert ist. Perfekt, um langatmige Meetings mit etwas Gamification aufzulockern.

## 📋 Features

- **Multiplayer Lobby**: Erstelle Bingo-Sessions mit anpassbarer Feldgröße (3x3 bis 5x5).
- **Echtzeit-Synchronisation**: Sieh live, wie deine Kollegen ihre Felder markieren (und wer kurz vor dem Sieg steht).
- **Wall of Shame**: Ein optionales Scoreboard, das die Gewinner persistent im Lyndrix Vault speichert.
- **Sarkastische Kommentare**: Das System kommentiert deine Leistung (oder deren Fehlen).
- **Integrierte Begriffe**: Kommt mit einer kuratierten Liste an Buzzwords (`terms.txt`), die pro Session angepasst werden kann.

## 🚀 Installation

Da es sich um ein Plugin für `lyndrix-core` handelt, muss es im Plugin-Verzeichnis der Hauptanwendung installiert werden.

1. Navigiere in das Plugin-Verzeichnis deiner `lyndrix-core` Installation:
   ```bash
   cd /pfad/zu/lyndrix-core/plugins
   ```

2. Klone dieses Repository:
   ```bash
   git clone https://github.com/lyndrix-platform/lyndrix-plugin-meeting-bingo.git lyndrix.plugin.bingo
   ```
   *Hinweis: Der Zielordnername `lyndrix.plugin.bingo` wird empfohlen, damit die ID im Manifest sauber matcht.*

3. Starte die Lyndrix-Anwendung neu. Das Plugin wird automatisch geladen und ist unter der Route `/bingo` erreichbar.

## ⚙️ Konfiguration

Das Plugin nutzt die interne `ctx` API von Lyndrix für Einstellungen und Secrets.

### Scoreboard (Wall of Shame)
Standardmäßig ist das dauerhafte Speichern von Gewinnern deaktiviert (aus "ethischen" Gründen).
Um es zu aktivieren:
1. Öffne das Plugin in der UI.
2. Scrolle zu den Einstellungen.
3. Aktiviere den Switch **"Scoreboard aktivieren"**.
4. Die Daten werden sicher im Vault unter dem Key `bingo_scoreboard` abgelegt.

## 🛠 Entwicklung & Struktur

- `entrypoint.py`: Dünne Wiring-Schicht (Manifest + Lifecycle), keine Geschäftslogik.
- `app/logic/service.py`: Spielzustand und -regeln (Single Source of Truth).
- `app/api.py`: REST-Router (`build_plugin_router`), gemountet unter `/api/plugins/lyndrix.plugin.bingo/`.
- `app/ui/react/`: React-Frontend (`react_ui=True`), gebaut nach `app/ui/static/ui_bundle.js`.
- `terms.txt`: Standardliste der Buzzwords (wird neu erstellt, falls gelöscht).

### Build (Frontend)
```bash
npm install
npm run build   # -> app/ui/static/ui_bundle.js
```

### Abhängigkeiten
Das Plugin nutzt ausschließlich die stabile `core.api`-Oberfläche von `lyndrix-core`
(Manifest, Router-Registrierung, Vault-Zugriff via `ctx`). Das Frontend ist eine
lokal gebündelte React-App ohne externe CDN-Laufzeitabhängigkeiten.

## 📝 Lizenz
Internes Tool. Nutzung auf eigene Gefahr während offizieller Meetings.