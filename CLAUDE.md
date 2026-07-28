# CLAUDE.md

Projektkontext für Claude Code. Kurz halten, nur Nicht-Offensichtliches.

## Was das ist

Zwei zusammengehörige Teile für batteriebetriebene WiFi-Taster auf **Adafruit
Feather ESP32-C6**:

- **`wifi-button-builder/`** — Python-/tkinter-GUI (Techniker-Tool). Konfiguriert
  Taster und pflegt eine geteilte Geräte-DB. Einstiegspunkt:
  `wifi_button_builder.py`.
- **`firmware/wifi-button-base/`** — generisches **Base-Image** (Arduino `.ino`).
  Wird **einmal** geflasht; danach kommt die Config per USB-Serial ins NVS — kein
  Recompile pro Gerät.

## Base-Image: die .ino ändern reicht NICHT

Geflasht wird vom **mars-provisioner** (`ptouch_gui.py` → `flash_wifi_base`), und
der kompiliert nichts — er schiebt die **committeten `.bin`-Artefakte** aus
`firmware/wifi-button-base/` per esptool auf den Chip. Eine Quelltextänderung
erreicht ein Board also erst nach:

1. `python3 firmware/export-base-image.py` (baut + aktualisiert `VERSION`),
2. Artefakte **committen und pushen** — der Provisioner macht vorher
   `git pull --rebase --autostash`, uncommittete `.bin`-Änderungen laufen dort in
   einen Konflikt,
3. neu flashen.

Der FQBN ist fix: `esp32:esp32:adafruit_feather_esp32c6:CDCOnBoot=cdc`. **Ohne
`CDCOnBoot=cdc` landet `Serial` auf UART0** und der USB-Config-Modus ist tot.
`FW_VERSION` in der `.ino` bei Protokolländerungen hochzählen — `VERSION`
übernimmt sie automatisch.

## Arbeitsweise / Konventionen

- **Python venv**: immer `wifi-button-builder/.venv` benutzen, **nie** global
  `pip`. Tool starten: `wifi-button-builder/.venv/bin/python wifi_button_builder.py`.
- **Tests**: `cd wifi-button-builder && .venv/bin/python -m pytest tests/ -q`.
- **Sprache**: UI-Texte, Commit-Messages und Kommentare auf **Deutsch** (siehe
  bestehende Commits/Code).
- Vor dem Commit `python -m py_compile wifi_button_builder.py` als schneller
  Syntax-Check.
- **Pre-Push-Hook**: `.githooks/pre-push` läuft `py_compile` + `pytest` vor jedem
  Push (spiegelt den CI-`test`-Job). Pro Klon einmalig aktivieren:
  `git config core.hooksPath .githooks`.

## Release / Build

- Windows-`.exe` entsteht **nur** über GitHub Actions
  (`.github/workflows/build-builder.yml`) beim Push eines **`v*`-Tags** → hängt
  die ZIP an ein GitHub-Release.
- **macOS** läuft direkt über die `.py` (kein `.app`-Build).
- Neues Release: `git tag vX.Y.Z && git push origin vX.Y.Z`. Tag muss auf den
  Commit zeigen, der gebaut werden soll.

## Geteilte Geräte-DB (wichtig)

- Builder und das separate **ptouch**-Tool teilen sich **eine** SQLite-DB,
  `labels.db`, MAC-keyed, über das ptouch-Repo git-synchronisiert.
- Liegt das ptouch-Repo daneben (`../ptouch/labels.db`), wird es benutzt und
  Änderungen werden auto-committed/-gepusht; sonst Fallback auf lokale
  `~/.wifi_button_builder/buttons.db` ohne Git-Sync.
- DB-Schreibvorgänge im Builder lösen `wb_git_push(...)` aus (fire-and-forget).

## Serial-Config-Protokoll (Base-Image ↔ Builder)

Zeilenweise über 115200 Baud. Befehle: `MAC?`, `VER?`, `CFG?` (Dump + `END`),
`SET <key> <val>`, `SAVE`, `CLEAR`, `RUN` (Config-Modus verlassen / Test-Sendung).
USB angesteckt ⇒ Config-Modus; Batterie-Wake (GPIO/Timer) ⇒ Normalbetrieb.
Das WLAN-Passwort gibt das Base-Image bei `CFG?` bewusst **nicht** aus.

- `<val>` darf **leer** sein (`SET pass ` löscht das Passwort → offenes WLAN).
  Jedes `SET` quittiert mit `OK` bzw. `ERR set`; der Builder wertet das aus und
  bricht vor dem `SAVE` ab, wenn ein Wert abgelehnt wurde.
- **DTR muss gesetzt sein** (`_open_config_port`): Arduinos `Serial`-bool spiegelt
  auf USB-CDC die DTR-Leitung, und das Base-Image verlässt bei `!Serial` sofort den
  Config-Modus. Ohne DTR hält das Board USB für abgezogen und schläft ein.
- **Ein Reset ist darüber trotzdem nicht möglich** — der ESP32-C6 hängt am
  USB-Serial-JTAG, dafür bräuchte es die esptool-Sequenz. Ein Board, das den
  Config-Modus verlassen hat (`CONFIG_IDLE_MS`, 10 min ohne Kommando → Deep Sleep),
  holt nur die **RESET-Taste** zurück.
- Antworten immer per Deadline-Schleife lesen (`_read_until`), nie mit einem
  einzelnen `read()` nach festem `sleep` — `read(in_waiting or 1)` liefert sonst
  ein einzelnes Byte und die Antwort gilt fälschlich als ausgeblieben.

## Aktions-URL (festes Schema)

Jede Taster-Aktion hat immer die Form
`http://<host>/cgi-bin/index.cgi?webif-pass=<pass>&spotrequest=<request>`,
Methode **GET**, genau **eine** Aktion pro Taster. Im Builder werden nur Host,
WebIF-Pass und Spot-Request editiert; gespeichert wird die volle URL.
