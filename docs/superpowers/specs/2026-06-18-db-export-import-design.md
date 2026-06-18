# DB-Export / -Import (Merge) — Design

**Datum:** 2026-06-18
**Komponente:** `wifi-button-builder/wifi_button_builder.py` (DB-Panel)

## Ziel

Ein Techniker konfiguriert Buttons auf seinem Laptop (lokale `buttons.db`, kein
git-Sync). Er exportiert seine Geräte in eine Datei und schickt sie. Der
Empfänger importiert sie in die geteilte, git-synchronisierte `labels.db` —
mit Vorschau und selektivem Merge, kein blindes Überschreiben.

## Format

Versionierte JSON-Datei `*.wbdb.json` (verlustfrei, inkl. `.ino` und
`config_json`, anders als der bestehende CSV-Export, der diese verliert):

```json
{
  "format": "wifi-button-db",
  "version": 1,
  "exported_at": "2026-06-18T08:30:00",
  "devices": [ { "mac": "...", "...": "...alle Spalten inkl. config_json + ino..." } ]
}
```

Der bestehende CSV-Export bleibt unverändert (Tabellen/Mensch).

## Export

- Neuer Button `⬇ DB` neben `⬇ CSV` im DB-Panel.
- Exportiert die **aktuell gefilterte** Auswahl (alle, wenn Suchfeld leer) —
  gleiches Verhalten wie CSV.
- Speicherdialog, Default `wifi-buttons_YYYYMMDD.wbdb.json`.

## Import

1. Datei wählen → JSON parsen, Umschlag validieren (falsches Format →
   Fehlerdialog; Datensatz ohne MAC → übersprungen + Hinweis).
2. Diff gegen die eigene DB je MAC: **NEU / ÄNDERT / IDENTISCH** (Vergleich
   über die inhaltlichen Felder, ohne Zeitstempel/`flash_count`).
3. Vorschau-Dialog (Treeview): Status · MAC · Gerät · Kunde/Standort, mit
   Häkchen. Vorausgewählt: NEU + ÄNDERT; IDENTISCH abgewählt.
   Buttons „Importieren (n)" / „Abbrechen".
4. Bestätigte Einträge per Upsert schreiben. Bei vorhandener MAC: Techniker-
   Werte überschreiben, aber `first_seen` und der höhere `flash_count` bleiben
   erhalten (Historie geht nicht verloren).
5. Bei aktiver git-DB: `wb_git_push("import: N Geräte")`. UI refresh.

## GUI-freie Funktionen (testbar ohne Tk)

- `wb_export_db(query="") -> dict` — Umschlag bauen.
- `wb_full_row(mac) -> dict | None` — voller Datensatz für Diff/Vergleich.
- `wb_import_records(records, selected_macs) -> (added, updated)` — Merge-Upsert.
- `wb_diff_record(record) -> "new" | "changed" | "same"` — Klassifizierung.

## Tests

Projekt hat noch keine Tests. Gezielte Tests für die reinen Datenfunktionen
gegen eine Temp-DB (`DB_PATH` gepatcht):

- Round-Trip: Export → Import in leere DB ergibt identische Datensätze.
- Diff-Klassifizierung: new / changed / same korrekt.
- Konflikt-Merge: `first_seen` und höherer `flash_count` bleiben erhalten.
- Selektion: nur ausgewählte MACs werden geschrieben.

## Fehlerfälle

- Kaputtes/fremdes JSON → Dialog, kein Schreiben.
- Datensatz ohne `mac` → überspringen + Hinweis.
- Leere Auswahl → nichts tun.
