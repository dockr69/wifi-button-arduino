#!/usr/bin/env python3
"""
ESP32-C6 WiFi Button Builder
Generates Arduino IDE .ino sketches for battery-powered WiFi buttons.
Hardcoded for Adafruit Feather ESP32-C6.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import csv
import json
import os
import re
import ipaddress
import sqlite3
import subprocess
import sys
import threading
import time
import queue
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, quote

# ESP32-C6 (USB-Serial-JTAG) puts the base MAC into the USB descriptor
# serial number, so we can read it without any firmware running.
MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")

# Windows: stop child processes (git) from flashing a console window — the GUI
# is windowed. CREATE_NO_WINDOW exists only on Windows; 0 elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "device_name": "wifi-button",
    # Installation site — ESP32s are deployed per location. Drives DB sorting.
    "customer": "",
    "location": "",
    "wifi_ssid": "",
    "wifi_password": "",
    # ip_mode: "static" | "dhcp_cache"
    #   static     — never use DHCP (fastest, requires fixed IP at router)
    #   dhcp_cache — DHCP once, cache lease in RTC RAM, reuse on wake
    "ip_mode": "static",
    "static_ip": "192.168.2.123",
    "gateway": "192.168.2.1",
    "subnet": "255.255.255.0",
    "dns": "8.8.8.8",
    "wifi_timeout_s": 10,
    "http_timeout_s": 3,
    # Anti-spam cooldown: ignore presses within N seconds of last send.
    # 0 disables. Survives deep sleep via RTC timekeeping.
    "cooldown_s": 30,
    "wifi_tx_power": "20dBm",
    "wifi_power_save": False,
    # Send the whole batch repeat_count times, with repeat_interval_s between sends.
    # repeat_count=1 disables repeats (single send per press).
    "repeat_count": 1,
    "repeat_interval_s": 60,
    "buttons": [
        {
            "name": "Button 1",
            "url": "http://192.168.2.175/cgi-bin/index.cgi?webif-pass=1&spotrequest=test1.mp3",
            "method": "GET",
        }
    ],
}

TX_POWER_MAP = {
    "20dBm": "WIFI_POWER_19_5dBm",
    "17dBm": "WIFI_POWER_17dBm",
    "15dBm": "WIFI_POWER_15dBm",
    "13dBm": "WIFI_POWER_13dBm",
    "11dBm": "WIFI_POWER_11dBm",
    "8.5dBm": "WIFI_POWER_8_5dBm",
    "7dBm": "WIFI_POWER_7dBm",
    "5dBm": "WIFI_POWER_5dBm",
    "2dBm": "WIFI_POWER_2dBm",
}

# Hardcoded wakeup pin: Adafruit Feather ESP32-C6 "A5 / IO2" — RTC GPIO, button to GND.
WAKEUP_GPIO = 2

# Hardcoded maintenance: hold the button 5s → stay awake 60s for reflashing.
# Long stay-awake is critical so the user has time to push a new sketch via USB
# before the chip drops back into deep sleep.
MAINTENANCE_HOLD_MS = 5000
MAINTENANCE_MS = 60000

# ── Shared device DB (ptouch/labels.db) ────────────────────────────────────────
#
# The ptouch label tool keeps a git-synced SQLite DB (labels.db) keyed by the
# device MAC. We reuse that exact file so a flashed button's full config (IP,
# WiFi, HTTP actions …) lives next to its printed-label record and PTouch can
# show everything. The ptouch repo is private, so storing the WiFi password in
# plaintext is acceptable. If the ptouch repo isn't checked out beside us, we
# fall back to a local buttons.db (no git sync).

SCRIPT_DIR = Path(__file__).resolve().parent
PTOUCH_DIR = Path(os.environ.get(
    "WIFI_BUTTON_PTOUCH_DIR",
    str(SCRIPT_DIR.parent.parent / "ptouch"),
))
if PTOUCH_DIR.is_dir():
    DB_PATH = PTOUCH_DIR / "labels.db"
    DB_GIT_DIR: Path | None = PTOUCH_DIR
else:
    # Stable per-user location so the fallback DB survives across runs. With a
    # packaged (PyInstaller onefile) build SCRIPT_DIR is a temp extraction dir,
    # so a buttons.db next to it would be ephemeral — keep it in the home dir.
    APP_DATA_DIR = Path.home() / ".wifi_button_builder"
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = APP_DATA_DIR / "buttons.db"
    DB_GIT_DIR = None

WB_SCHEMA = """
CREATE TABLE IF NOT EXISTS wifi_buttons (
    mac           TEXT PRIMARY KEY,
    customer      TEXT,
    location      TEXT,
    device_name   TEXT,
    ip_mode       TEXT,
    ip            TEXT,
    gateway       TEXT,
    subnet        TEXT,
    dns           TEXT,
    wifi_ssid     TEXT,
    wifi_password TEXT,
    buttons       TEXT,
    config_json   TEXT,
    ino           TEXT,
    first_seen    TEXT,
    last_flashed  TEXT,
    flash_count   INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);
"""


def wb_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(WB_SCHEMA)
    # Migration for DBs created before a column existed.
    have = [r[1] for r in con.execute("PRAGMA table_info(wifi_buttons)").fetchall()]
    for col in ("ino", "customer", "location"):
        if col not in have:
            con.execute(f"ALTER TABLE wifi_buttons ADD COLUMN {col} TEXT")
    con.commit()
    return con


def wb_register(mac: str, cfg: dict, ino: str = "") -> None:
    """Insert or update a device's full config (+ generated .ino), keyed by MAC."""
    mac = mac.upper()
    now = datetime.now().isoformat(timespec="seconds")
    ip = cfg.get("static_ip", "") if cfg.get("ip_mode") == "static" else "DHCP"
    buttons = json.dumps(cfg.get("buttons", []), ensure_ascii=False)
    config_json = json.dumps(cfg, ensure_ascii=False)
    con = wb_db()
    con.execute(
        """
        INSERT INTO wifi_buttons
            (mac, customer, location, device_name, ip_mode, ip, gateway,
             subnet, dns, wifi_ssid, wifi_password, buttons, config_json, ino,
             first_seen, last_flashed, flash_count, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'')
        ON CONFLICT(mac) DO UPDATE SET
            customer      = excluded.customer,
            location      = excluded.location,
            device_name   = excluded.device_name,
            ip_mode       = excluded.ip_mode,
            ip            = excluded.ip,
            gateway       = excluded.gateway,
            subnet        = excluded.subnet,
            dns           = excluded.dns,
            wifi_ssid     = excluded.wifi_ssid,
            wifi_password = excluded.wifi_password,
            buttons       = excluded.buttons,
            config_json   = excluded.config_json,
            ino           = excluded.ino,
            last_flashed  = excluded.last_flashed,
            flash_count   = wifi_buttons.flash_count + 1
        """,
        (mac, cfg.get("customer", ""), cfg.get("location", ""),
         cfg.get("device_name", ""), cfg.get("ip_mode", ""), ip,
         cfg.get("gateway", ""), cfg.get("subnet", ""), cfg.get("dns", ""),
         cfg.get("wifi_ssid", ""), cfg.get("wifi_password", ""),
         buttons, config_json, ino, now, now),
    )
    con.commit()
    con.close()


def wb_update_from_read(mac: str, read_cfg: dict) -> str:
    """Nur die per CFG? ausgelesenen Felder (WLAN + Buttons) in den DB-Eintrag
    übernehmen. Metadaten (Kunde/Standort/Gerätename), Passwort und flash_count
    bleiben unangetastet — das Base-Image gibt sie nicht aus. Gibt 'updated' für
    einen vorhandenen bzw. 'created' für einen neu angelegten Eintrag zurück."""
    mac = mac.upper()
    existing = wb_get_config(mac) or {}
    merged = {**existing, **read_cfg}
    ip = merged.get("static_ip", "") if merged.get("ip_mode") == "static" else "DHCP"
    buttons = json.dumps(merged.get("buttons", []), ensure_ascii=False)
    config_json = json.dumps(merged, ensure_ascii=False)
    con = wb_db()
    found = con.execute("SELECT 1 FROM wifi_buttons WHERE mac=?", (mac,)).fetchone()
    if found:
        con.execute(
            """UPDATE wifi_buttons SET
                   ip_mode=?, ip=?, gateway=?, subnet=?, dns=?, wifi_ssid=?,
                   buttons=?, config_json=?
               WHERE mac=?""",
            (merged.get("ip_mode", ""), ip, merged.get("gateway", ""),
             merged.get("subnet", ""), merged.get("dns", ""),
             merged.get("wifi_ssid", ""), buttons, config_json, mac),
        )
        result = "updated"
    else:
        now = datetime.now().isoformat(timespec="seconds")
        con.execute(
            """INSERT INTO wifi_buttons
                   (mac, customer, location, device_name, ip_mode, ip, gateway,
                    subnet, dns, wifi_ssid, wifi_password, buttons, config_json,
                    ino, first_seen, last_flashed, flash_count, notes)
               VALUES (?,'','','',?,?,?,?,?,?,'',?,?,'',?,NULL,0,'')""",
            (mac, merged.get("ip_mode", ""), ip, merged.get("gateway", ""),
             merged.get("subnet", ""), merged.get("dns", ""),
             merged.get("wifi_ssid", ""), buttons, config_json, now),
        )
        result = "created"
    con.commit()
    con.close()
    return result


def _wb_rows(where: str = "", params: tuple = ()) -> list[tuple]:
    con = wb_db()
    rows = con.execute(
        "SELECT COALESCE(customer,''), COALESCE(location,''), mac, "
        "device_name, ip, wifi_ssid, buttons, "
        "COALESCE(last_flashed,'—'), flash_count, COALESCE(notes,'') "
        f"FROM wifi_buttons {where} "
        "ORDER BY customer COLLATE NOCASE, location COLLATE NOCASE, "
        "device_name COLLATE NOCASE",
        params,
    ).fetchall()
    con.close()
    return rows


def wb_all() -> list[tuple]:
    return _wb_rows()


def wb_search(query: str) -> list[tuple]:
    like = f"%{query}%"
    return _wb_rows(
        "WHERE customer LIKE ? OR location LIKE ? OR mac LIKE ? "
        "OR device_name LIKE ? OR ip LIKE ? OR wifi_ssid LIKE ? "
        "OR buttons LIKE ? OR COALESCE(notes,'') LIKE ?",
        (like, like, like, like, like, like, like, like),
    )


WB_EXPORT_COLS = ("customer", "location", "mac", "device_name", "ip_mode",
                  "ip", "gateway", "subnet", "dns", "wifi_ssid",
                  "wifi_password", "buttons", "first_seen", "last_flashed",
                  "flash_count", "notes", "config_json")


def wb_export_rows(query: str = "") -> list[tuple]:
    """Full per-device rows for CSV export (everything that's configurable)."""
    select = (
        "SELECT COALESCE(customer,''), COALESCE(location,''), mac, "
        "COALESCE(device_name,''), COALESCE(ip_mode,''), COALESCE(ip,''), "
        "COALESCE(gateway,''), COALESCE(subnet,''), COALESCE(dns,''), "
        "COALESCE(wifi_ssid,''), COALESCE(wifi_password,''), "
        "COALESCE(buttons,''), COALESCE(first_seen,''), "
        "COALESCE(last_flashed,''), COALESCE(flash_count,0), "
        "COALESCE(notes,''), COALESCE(config_json,'') FROM wifi_buttons "
    )
    order = (" ORDER BY customer COLLATE NOCASE, location COLLATE NOCASE, "
             "device_name COLLATE NOCASE")
    con = wb_db()
    if query:
        like = f"%{query}%"
        rows = con.execute(
            select +
            "WHERE customer LIKE ? OR location LIKE ? OR mac LIKE ? "
            "OR device_name LIKE ? OR ip LIKE ? OR wifi_ssid LIKE ? "
            "OR buttons LIKE ? OR COALESCE(notes,'') LIKE ?" + order,
            (like, like, like, like, like, like, like, like),
        ).fetchall()
    else:
        rows = con.execute(select + order).fetchall()
    con.close()
    return rows


# ── DB export / import (verlustfreies JSON, MAC-keyed Merge) ────────────────────

# Volle Spaltenliste der Tabelle (Reihenfolge = INSERT-Reihenfolge unten).
WB_ALL_COLS = ("mac", "customer", "location", "device_name", "ip_mode", "ip",
               "gateway", "subnet", "dns", "wifi_ssid", "wifi_password",
               "buttons", "config_json", "ino", "first_seen", "last_flashed",
               "flash_count", "notes")
# Inhaltliche Felder für den Diff (ohne Zeitstempel/Zähler).
WB_CONTENT_COLS = ("customer", "location", "device_name", "ip_mode", "ip",
                   "gateway", "subnet", "dns", "wifi_ssid", "wifi_password",
                   "buttons", "config_json", "ino", "notes")
WB_EXPORT_FORMAT = "wifi-button-db"
WB_EXPORT_VERSION = 1


def wb_full_row(mac: str) -> dict | None:
    """Full per-device record as a dict, keyed by MAC (None if unknown)."""
    con = wb_db()
    row = con.execute(
        f"SELECT {', '.join(WB_ALL_COLS)} FROM wifi_buttons WHERE mac=?",
        (mac.upper(),),
    ).fetchone()
    con.close()
    return dict(zip(WB_ALL_COLS, row)) if row else None


def wb_export_db(query: str = "") -> dict:
    """Lossless export bundle of the (optionally filtered) devices."""
    rows = wb_search(query) if query else wb_all()
    devices = [wb_full_row(r[2]) for r in rows]  # r[2] == mac
    return {
        "format": WB_EXPORT_FORMAT,
        "version": WB_EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "devices": [d for d in devices if d],
    }


def _import_text(record: dict, col: str) -> str:
    """Read a column from an import record, coercing JSON fields to strings."""
    val = record.get(col, "")
    if col in ("buttons", "config_json") and not isinstance(val, str):
        return json.dumps(val, ensure_ascii=False)
    return "" if val is None else str(val)


def wb_diff_record(record: dict) -> str:
    """Classify an import record against the current DB: new|changed|same."""
    mac = (record.get("mac") or "").upper()
    existing = wb_full_row(mac) if mac else None
    if existing is None:
        return "new"
    for col in WB_CONTENT_COLS:
        if _import_text(record, col) != ("" if existing[col] is None
                                         else str(existing[col])):
            return "changed"
    return "same"


def wb_import_records(records: list[dict],
                      selected_macs: set[str]) -> tuple[int, int]:
    """Merge-upsert selected records. Returns (added, updated).

    On conflict the incoming content overwrites, but first_seen and the higher
    flash_count are kept so local history survives. Records without a MAC are
    skipped."""
    selected = {m.upper() for m in selected_macs}
    added = updated = 0
    con = wb_db()
    now = datetime.now().isoformat(timespec="seconds")
    for rec in records:
        mac = (rec.get("mac") or "").upper()
        if not mac or mac not in selected:
            continue
        exists = con.execute(
            "SELECT 1 FROM wifi_buttons WHERE mac=?", (mac,)).fetchone()
        vals = {c: _import_text(rec, c) for c in WB_CONTENT_COLS}
        first_seen = rec.get("first_seen") or now
        last_flashed = rec.get("last_flashed") or now
        flash_count = int(rec.get("flash_count") or 0)
        con.execute(
            f"""
            INSERT INTO wifi_buttons
                ({', '.join(WB_ALL_COLS)})
            VALUES ({', '.join('?' for _ in WB_ALL_COLS)})
            ON CONFLICT(mac) DO UPDATE SET
                {', '.join(f'{c}=excluded.{c}' for c in WB_CONTENT_COLS)},
                last_flashed = MAX(COALESCE(wifi_buttons.last_flashed,''),
                                   COALESCE(excluded.last_flashed,'')),
                flash_count  = MAX(wifi_buttons.flash_count, excluded.flash_count)
            """,
            (mac, vals["customer"], vals["location"], vals["device_name"],
             vals["ip_mode"], vals["ip"], vals["gateway"], vals["subnet"],
             vals["dns"], vals["wifi_ssid"], vals["wifi_password"],
             vals["buttons"], vals["config_json"], vals["ino"],
             first_seen, last_flashed, flash_count, vals["notes"]),
        )
        if exists:
            updated += 1
        else:
            added += 1
    con.commit()
    con.close()
    return added, updated


def wb_get_config(mac: str) -> dict | None:
    con = wb_db()
    row = con.execute(
        "SELECT config_json FROM wifi_buttons WHERE mac=?", (mac,)
    ).fetchone()
    con.close()
    return json.loads(row[0]) if row and row[0] else None


def wb_get_ino(mac: str) -> str:
    con = wb_db()
    row = con.execute(
        "SELECT ino FROM wifi_buttons WHERE mac=?", (mac,)
    ).fetchone()
    con.close()
    return (row[0] or "") if row else ""


# Columns offered as builder dropdowns, most-recently-used first.
_DISTINCT_OK = {"customer", "location", "ip", "gateway", "subnet", "dns",
                "wifi_ssid", "device_name"}


def wb_distinct(column: str) -> list[str]:
    """Distinct non-empty values for a column, newest-first (for dropdowns)."""
    if column not in _DISTINCT_OK:
        return []
    con = wb_db()
    rows = con.execute(
        f"SELECT {column}, MAX(last_flashed) m FROM wifi_buttons "
        f"WHERE {column} IS NOT NULL AND {column} != '' "
        f"GROUP BY {column} ORDER BY m DESC"
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def wb_macs() -> list[str]:
    con = wb_db()
    rows = con.execute(
        "SELECT mac FROM wifi_buttons ORDER BY last_flashed DESC"
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def _devices_table_exists(con) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='devices'"
    ).fetchone() is not None


def wb_mac_labels() -> dict[str, str]:
    """mac → 'Kunde / Standort · Tasterort' for every MAC in the shared DB.

    Kunde/Standort come from the builder (wifi_buttons); the Tasterort (the
    label text assigned in the ptouch tool, e.g. 'Eisenwaren Info') comes from
    devices.freetext. Either part may be missing."""
    con = wb_db()
    kunde_ort: dict[str, str] = {}
    for mac, cust, loc in con.execute(
            "SELECT mac, COALESCE(customer,''), COALESCE(location,'') FROM wifi_buttons"):
        kunde_ort[mac] = " / ".join(x for x in (cust, loc) if x)
    tasterort: dict[str, str] = {}
    if _devices_table_exists(con):
        for mac, free in con.execute(
                "SELECT mac, COALESCE(freetext,'') FROM devices"):
            if free:
                tasterort[mac] = free
    con.close()
    labels: dict[str, str] = {}
    for mac in set(kunde_ort) | set(tasterort):
        parts = [p for p in (kunde_ort.get(mac, ""), tasterort.get(mac, "")) if p]
        labels[mac] = " · ".join(parts)
    return labels


def wb_get_meta(mac: str) -> tuple[str, str]:
    """(customer, location) for a MAC from the builder's records.

    Note: the ptouch freetext is the *Tasterort* (button position), NOT the
    city, so it is deliberately not used to fill the Standort field here."""
    con = wb_db()
    row = con.execute(
        "SELECT COALESCE(customer,''), COALESCE(location,'') "
        "FROM wifi_buttons WHERE mac=?", (mac,)).fetchone()
    con.close()
    return row if row else ("", "")


def suggest_device_name(mac: str) -> str:
    """Device-Name suggestion = exactly the label shown next to the MAC in the
    dropdown (Kunde / Standort · Tasterort, as assigned in PTouch). Pre-fills the
    editable Device Name when a MAC is picked instead of carrying over the
    previous button's name."""
    return wb_mac_labels().get(mac, "").strip()


def wb_delete(mac: str) -> None:
    con = wb_db()
    con.execute("DELETE FROM wifi_buttons WHERE mac=?", (mac,))
    con.commit()
    con.close()


def wb_set_notes(mac: str, notes: str) -> None:
    con = wb_db()
    con.execute("UPDATE wifi_buttons SET notes=? WHERE mac=?", (notes, mac))
    con.commit()
    con.close()


def wb_git_pull() -> None:
    """Pull the latest DB from the ptouch remote (silent, non-fatal)."""
    if DB_GIT_DIR is None:
        return
    try:
        subprocess.run(
            ["git", "-C", str(DB_GIT_DIR), "pull", "--rebase",
             "--autostash", "--quiet"],
            check=False, timeout=15, capture_output=True,
            creationflags=_NO_WINDOW)
    except Exception:
        pass


def wb_git_push(message: str) -> None:
    """Commit + push the DB (async, fire-and-forget) via the ptouch repo."""
    if DB_GIT_DIR is None:
        return

    def _run():
        try:
            r = subprocess.run(
                ["git", "-C", str(DB_GIT_DIR), "add", str(DB_PATH)],
                capture_output=True, timeout=10, creationflags=_NO_WINDOW)
            if r.returncode != 0:
                return
            r = subprocess.run(
                ["git", "-C", str(DB_GIT_DIR), "diff", "--cached", "--quiet"],
                capture_output=True, creationflags=_NO_WINDOW)
            if r.returncode == 0:
                return  # nothing staged
            subprocess.run(
                ["git", "-C", str(DB_GIT_DIR), "commit", "-m", message],
                capture_output=True, timeout=10, creationflags=_NO_WINDOW)
            subprocess.run(
                ["git", "-C", str(DB_GIT_DIR), "pull", "--rebase",
                 "--autostash", "--quiet"],
                capture_output=True, timeout=15, creationflags=_NO_WINDOW)
            subprocess.run(
                ["git", "-C", str(DB_GIT_DIR), "push", "--quiet"],
                capture_output=True, timeout=20, creationflags=_NO_WINDOW)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


# ── Serielle Geräteerkennung (pyserial) ───────────────────────────────────────
#
# Der Builder kompiliert/flasht NICHT selbst: Die Techniker bekommen Taster, auf
# denen das generische Base-Image bereits liegt. Config wird nur per USB-Serial
# geschrieben (send_config_serial) bzw. gelesen (read_config_serial). Zum
# Auflisten der Boards reicht deshalb pyserial — kein arduino-cli, kein
# ESP32-Core-Download, kein Bootstrap.


def _detected_ports() -> list[dict]:
    """Angeschlossene ESP32-C6-Boards via pyserial. Behält die frühere Form der
    arduino-cli-Ausgabe (address + serialNumber), damit die Aufrufer unverändert
    bleiben. Auf dem ESP32-C6 (USB-Serial-JTAG) legt das ROM die Basis-MAC in die
    USB-Seriennummer — wir listen nur Ports mit einer MAC darin (die echten
    Taster, ohne Bluetooth-/Debug-Ports)."""
    from serial.tools import list_ports
    out = []
    for p in list_ports.comports():
        sn = p.serial_number or ""
        if p.device and MAC_RE.search(sn):
            out.append({"port": {"address": p.device,
                                 "properties": {"serialNumber": sn}}})
    return out


def list_serial_ports() -> list[str]:
    """Return detected serial port addresses."""
    result = []
    for entry in _detected_ports():
        addr = entry.get("port", {}).get("address")
        if addr:
            result.append(addr)
    return result


def connected_macs() -> list[str]:
    """MACs of all currently connected boards, in one board-list pass."""
    out = []
    for entry in _detected_ports():
        sn = (entry.get("port", {}).get("properties") or {}).get("serialNumber", "")
        m = MAC_RE.search(sn or "")
        if m:
            out.append(m.group(1).upper())
    return out


def mac_from_port(port: str) -> str | None:
    """Read the base MAC from the USB serial number of the given port.

    On the ESP32-C6 (USB-Serial-JTAG) the ROM sets the USB descriptor serial
    number to the base MAC — works without any firmware running."""
    for entry in _detected_ports():
        p = entry.get("port", {})
        if p.get("address") == port:
            sn = (p.get("properties") or {}).get("serialNumber", "")
            m = MAC_RE.search(sn or "")
            if m:
                return m.group(1).upper()
    return None


def _parse_button_url(url: str, method: str):
    """(host, port, path, method) aus einer Button-URL — wie in generate_ino."""
    m = re.match(r'https?://([^/:]+)(?::(\d+))?(/.*)?$', url or "")
    if m:
        return m.group(1), int(m.group(2) or 80), m.group(3) or "/", method
    return "", 80, (url or "/"), method


def _config_set_pairs(cfg: dict):
    """cfg-Dict -> Liste (key, value) für das Base-Image-NVS (SET-Befehle)."""
    pairs = [
        ("ssid", cfg.get("wifi_ssid", "")),
        ("pass", cfg.get("wifi_password", "")),
        ("ipmode", cfg.get("ip_mode", "static")),
        ("ip", cfg.get("static_ip", "")),
        ("gw", cfg.get("gateway", "")),
        ("sn", cfg.get("subnet", "")),
        ("dns", cfg.get("dns", "")),
        ("wifitmo", int(cfg.get("wifi_timeout_s", 10)) * 1000),
        ("httptmo", int(cfg.get("http_timeout_s", 3)) * 1000),
        ("repcnt", int(cfg.get("repeat_count", 1))),
        ("repint", int(cfg.get("repeat_interval_s", 60)) * 1000),
        ("cooldn", int(cfg.get("cooldown_s", 0)) * 1000000),
        ("txpow", cfg.get("wifi_tx_power", "20dBm")),
        ("psave", 1 if cfg.get("wifi_power_save") else 0),
    ]
    btns = cfg.get("buttons", [])
    pairs.append(("btncnt", len(btns)))
    for i, b in enumerate(btns):
        host, port, path, meth = _parse_button_url(b.get("url", ""), b.get("method", "GET"))
        pairs += [(f"b{i}host", host), (f"b{i}port", port),
                  (f"b{i}path", path), (f"b{i}meth", meth)]
    return pairs


def send_config_serial(port: str, cfg: dict, log_cb) -> bool:
    """Config per USB-Serial ins Base-Image-NVS schreiben (SET/SAVE) — kein
    Recompile/Flash. Setzt voraus, dass das generische Base-Image geflasht ist
    (ptouch) und das Board im Config-Modus lauscht (frisch geflasht / Reset)."""
    import serial as _serial
    try:
        with _serial.Serial(port, 115200, timeout=2) as ser:
            # Best-effort Reset über DTR -> Board bootet in den Config-Modus
            try:
                ser.dtr = False; time.sleep(0.1); ser.dtr = True
            except Exception:
                pass
            time.sleep(1.8)
            ser.reset_input_buffer()
            ser.write(b"VER?\n"); time.sleep(0.4)
            resp = ser.read(ser.in_waiting or 1).decode(errors="ignore")
            if "VER wbtn" not in resp:
                log_cb("✗ Keine Antwort vom Base-Image (VER?).")
                log_cb("  Base-Image geflasht? (ptouch) Board kurz RESET/neu anstecken.")
                return False
            log_cb(f"✓ Base-Image erkannt: {resp.strip()}")
            for k, v in _config_set_pairs(cfg):
                ser.write(f"SET {k} {v}\n".encode()); time.sleep(0.04)
                ser.read(ser.in_waiting or 0)
            ser.write(b"SAVE\n"); time.sleep(0.6)
            resp = ser.read(ser.in_waiting or 1).decode(errors="ignore")
            if "OK saved" in resp:
                log_cb("✓ Config ins NVS gespeichert. Button einsatzbereit.")
                return True
            log_cb(f"⚠ Unerwartete SAVE-Antwort: {resp.strip()}")
            return False
    except Exception as e:
        log_cb(f"✗ Serial-Fehler: {e}")
        return False


def _parse_dump(text: str) -> dict | None:
    """CFG?-Dump des Base-Images in ein (Teil-)Config-Dict übersetzen.
    Gibt None zurück, wenn nichts Verwertbares enthalten ist. Das WLAN-Passwort
    wird vom Base-Image bewusst NICHT ausgegeben und fehlt daher hier."""
    kv: dict[str, str] = {}
    btns: dict[int, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line == "END":
            break
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        m = re.fullmatch(r"b(\d+)", key)
        if m:
            btns[int(m.group(1))] = val
        else:
            kv[key] = val
    if not kv and not btns:
        return None

    def _int(key, default, scale=1):
        try:
            return max(1, int(kv[key]) // scale) if scale != 1 else int(kv[key])
        except (KeyError, ValueError):
            return default

    cfg: dict = {}
    if "ssid" in kv:    cfg["wifi_ssid"] = kv["ssid"]
    if "ipmode" in kv:  cfg["ip_mode"] = kv["ipmode"]
    if "ip" in kv:      cfg["static_ip"] = kv["ip"]
    if "gw" in kv:      cfg["gateway"] = kv["gw"]
    if "sn" in kv:      cfg["subnet"] = kv["sn"]
    if "dns" in kv:     cfg["dns"] = kv["dns"]
    if "wifitmo" in kv: cfg["wifi_timeout_s"] = _int("wifitmo", 10, 1000)
    if "httptmo" in kv: cfg["http_timeout_s"] = _int("httptmo", 3, 1000)
    if "repcnt" in kv:  cfg["repeat_count"] = _int("repcnt", 1)
    if "repint" in kv:  cfg["repeat_interval_s"] = _int("repint", 60, 1000)
    if "txpow" in kv:   cfg["wifi_tx_power"] = kv["txpow"]
    if "psave" in kv:   cfg["wifi_power_save"] = kv["psave"] not in ("0", "", "false")

    buttons = []
    for i in sorted(btns):
        parts = btns[i].split(" ", 3)
        host = parts[0] if len(parts) > 0 else ""
        try:
            bport = int(parts[1]) if len(parts) > 1 else 80
        except ValueError:
            bport = 80
        meth = parts[2] if len(parts) > 2 else "GET"
        path = parts[3] if len(parts) > 3 else "/"
        if not path.startswith("/"):
            path = "/" + path
        url = f"http://{host}" + (f":{bport}" if bport != 80 else "") + path
        buttons.append({"name": f"Button {i + 1}", "url": url, "method": meth})
    cfg["buttons"] = buttons
    return cfg


def stream_serial_log(port: str, log_cb, stop_event: threading.Event,
                      send_run: bool = False) -> None:
    """Open the serial port and stream all output until stop_event is set.

    If send_run=True, sends the RUN command first so a test button action fires
    and its Serial.printf() output is visible in real time."""
    import serial as _serial
    try:
        with _serial.Serial(port, 115200, timeout=0.1) as ser:
            log_cb(f"[Verbunden · {port} · 115200 Baud]")
            if send_run:
                time.sleep(0.2)
                ser.reset_input_buffer()
                ser.write(b"RUN\n")
                log_cb("[RUN gesendet — Test-Sendung läuft …]")
            buf = b""
            while not stop_event.is_set():
                chunk = ser.read(ser.in_waiting or 1)
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        log_cb(line.decode(errors="replace").rstrip("\r"))
                else:
                    time.sleep(0.02)
            log_cb("[Gestoppt]")
    except Exception as e:
        log_cb(f"[Verbindung getrennt: {e}]")


def read_config_serial(port: str, log_cb) -> dict | None:
    """Aktuelle Config per USB-Serial aus dem Base-Image-NVS auslesen (CFG?).
    Setzt — wie send_config_serial — das geflashte Base-Image im Config-Modus
    voraus. Gibt ein (Teil-)Config-Dict zurück oder None bei Fehler."""
    import serial as _serial
    try:
        with _serial.Serial(port, 115200, timeout=2) as ser:
            # Best-effort Reset über DTR -> Board bootet in den Config-Modus
            try:
                ser.dtr = False; time.sleep(0.1); ser.dtr = True
            except Exception:
                pass
            time.sleep(1.8)
            ser.reset_input_buffer()
            ser.write(b"VER?\n"); time.sleep(0.4)
            resp = ser.read(ser.in_waiting or 1).decode(errors="ignore")
            if "VER wbtn" not in resp:
                log_cb("✗ Keine Antwort vom Base-Image (VER?).")
                log_cb("  Base-Image geflasht? (ptouch) Board kurz RESET/neu anstecken.")
                return None
            log_cb(f"✓ Base-Image erkannt: {resp.strip()}")
            ser.reset_input_buffer()
            ser.write(b"CFG?\n")
            buf = ""
            deadline = time.time() + 4
            while time.time() < deadline:
                chunk = ser.read(ser.in_waiting or 1).decode(errors="ignore")
                if chunk:
                    buf += chunk
                    if "END" in buf:
                        break
                else:
                    time.sleep(0.05)
            cfg = _parse_dump(buf)
            if cfg is None:
                log_cb("✗ Keine gültige CFG?-Antwort empfangen.")
                return None
            log_cb(f"✓ Config ausgelesen ({len(cfg.get('buttons', []))} Button(s)).")
            return cfg
    except Exception as e:
        log_cb(f"✗ Serial-Fehler: {e}")
        return None


# ── Aktions-URL (festes Schema) ───────────────────────────────────────────────
#
# Die Taster-Aktion hat immer dieselbe Form:
#   http://<host>/cgi-bin/index.cgi?webif-pass=<pass>&spotrequest=<request>
# Im Builder werden nur Host (IP), webif-pass und spotrequest editiert; Pfad und
# Methode (GET) sind fix. Gespeichert wird weiterhin die volle URL — bei
# Bestandsgeräten werden die drei Felder daraus zurückgelesen.
ACTION_PATH = "/cgi-bin/index.cgi"
ACTION_PASS_PARAM = "webif-pass"
ACTION_REQUEST_PARAM = "spotrequest"


def parse_action_url(url: str) -> tuple[str, str, str]:
    """(host, webif-pass, spotrequest) aus einer Aktions-URL herauslösen.
    Tolerant: was fehlt (oder eine abweichende Bestands-URL), wird ''."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return "", "", ""
    host = parts.netloc
    if not host and parts.path:
        # URL ohne Schema (z. B. blanke IP) -> ersten Pfadteil als Host nehmen.
        host = parts.path.lstrip("/").split("/", 1)[0]
    qs = dict(parse_qsl(parts.query, keep_blank_values=True))
    return host, qs.get(ACTION_PASS_PARAM, ""), qs.get(ACTION_REQUEST_PARAM, "")


def compose_action_url(host: str, webif_pass: str, request: str) -> str:
    """Volle Aktions-URL aus den drei Feldern bauen (immer GET, fester Pfad)."""
    host = (host or "").strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.strip("/")
    return (f"http://{host}{ACTION_PATH}"
            f"?{ACTION_PASS_PARAM}={quote(webif_pass.strip(), safe='')}"
            f"&{ACTION_REQUEST_PARAM}={quote(request.strip(), safe='')}")


# ── Code Generator ────────────────────────────────────────────────────────────


def generate_ino(cfg: dict) -> str:
    """Generate Arduino .ino sketch from config dict (ESP32-C6 only)."""
    lines = []

    def L(text=""):
        lines.append(text)

    # Parse URLs into host/port/path for fire-and-forget
    parsed_urls = []
    for btn in cfg["buttons"]:
        url = btn["url"]
        m = re.match(r'https?://([^/:]+)(?::(\d+))?(/.*)$', url)
        if m:
            parsed_urls.append({
                "host": m.group(1),
                "port": int(m.group(2)) if m.group(2) else 80,
                "path": m.group(3),
                "method": btn.get("method", "GET"),
            })
        else:
            parsed_urls.append({"host": "", "port": 80, "path": url, "method": btn.get("method", "GET")})

    L('#include <WiFi.h>')
    L('#include <esp_sleep.h>')
    L('#include <driver/gpio.h>')
    L('#include <sys/time.h>')
    L()

    L('// ---- Konfiguration ----')
    L(f'const char* WIFI_SSID     = "{cfg["wifi_ssid"]}";')
    L(f'const char* WIFI_PASSWORD = "{cfg["wifi_password"]}";')
    L()

    ip_mode = cfg.get("ip_mode", "static")
    if ip_mode == "static":
        for name, key in [("STATIC_IP", "static_ip"), ("GATEWAY", "gateway"),
                          ("SUBNET", "subnet"), ("DNS", "dns")]:
            octets = cfg[key].split(".")
            L(f'const IPAddress {name}({", ".join(octets)});')
        L()

    for i, pu in enumerate(parsed_urls):
        L(f'const char* HTTP_HOST_{i} = "{pu["host"]}";')
        L(f'const int   HTTP_PORT_{i} = {pu["port"]};')
        L(f'const char* HTTP_PATH_{i} = "{pu["path"]}";')
    L()

    L(f'const gpio_num_t WAKEUP_PIN = GPIO_NUM_{WAKEUP_GPIO};')
    L()

    L(f'const unsigned long WIFI_TIMEOUT_MS     = {cfg["wifi_timeout_s"] * 1000};')
    L(f'const unsigned long HTTP_TIMEOUT_MS     = {cfg["http_timeout_s"] * 1000};')
    L(f'const unsigned long MAINTENANCE_MS      = {MAINTENANCE_MS};   // hold {MAINTENANCE_HOLD_MS // 1000}s → stay awake {MAINTENANCE_MS // 1000}s')
    L(f'const unsigned long MAINTENANCE_HOLD_MS = {MAINTENANCE_HOLD_MS};')
    L(f'const int           REPEAT_COUNT       = {cfg.get("repeat_count", 1)};')
    L(f'const unsigned long REPEAT_INTERVAL_MS = {cfg.get("repeat_interval_s", 60) * 1000};')
    L(f'const uint64_t      COOLDOWN_US        = {cfg.get("cooldown_s", 0)}ULL * 1000000ULL;  // 0 = disabled')
    L()

    L('// WiFi cache survives deep sleep')
    L('RTC_DATA_ATTR int savedChannel = 0;')
    L('RTC_DATA_ATTR uint8_t savedBSSID[6] = {0};')
    L('RTC_DATA_ATTR bool hasCachedWiFi = false;')
    L('// Repeat sequence counter (incremented across timer-wake cycles)')
    L('RTC_DATA_ATTR int repeatIndex = 0;')
    L('// Wall-clock (gettimeofday) of last sequence start — survives deep sleep via RTC')
    L('RTC_DATA_ATTR uint64_t lastSendUs = 0;')
    if ip_mode == "dhcp_cache":
        L('// Cached DHCP lease — first wake learns it, every wake reuses it')
        L('RTC_DATA_ATTR uint32_t cachedIP  = 0;')
        L('RTC_DATA_ATTR uint32_t cachedGW  = 0;')
        L('RTC_DATA_ATTR uint32_t cachedSN  = 0;')
        L('RTC_DATA_ATTR uint32_t cachedDNS = 0;')
        L('RTC_DATA_ATTR bool hasCachedIP = false;')
    L()

    L('void sendHttpRequest(const char* host, int port, const char* path, const char* method);')
    L('void enterDeepSleep();')
    L('void enterTimerSleep(uint64_t us);')
    L()

    # ── setup()
    L('// ---- Setup (runs once on every wake) ----')
    L('void setup() {')
    L('  // STEMMA QT / NeoPixel power off + hold through deep sleep')
    L('  gpio_set_direction(GPIO_NUM_20, GPIO_MODE_OUTPUT);')
    L('  gpio_set_level(GPIO_NUM_20, 0);')
    L('  gpio_hold_en(GPIO_NUM_20);')
    L()
    L('  Serial.begin(115200);')
    L('  delay(50);')
    L()
    L('  esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();')
    L('  if (cause == ESP_SLEEP_WAKEUP_GPIO) {')
    L('    if (repeatIndex > 0) {')
    L('      Serial.printf("GPIO wake mid-sequence (after %d sends) - CANCEL\\n", repeatIndex);')
    L('      repeatIndex = 0;')
    L('      enterDeepSleep();  // does not return')
    L('    }')
    L('    Serial.println("GPIO wakeup - button pressed");')
    L()
    L('    // Long-press maintenance: hold the button past the threshold to stay awake.')
    L('    // Exits the moment the button is released — short press adds no delay.')
    L('    gpio_pullup_en(WAKEUP_PIN);')
    L('    unsigned long pressStart = millis();')
    L('    while (gpio_get_level(WAKEUP_PIN) == 0 && millis() - pressStart < MAINTENANCE_HOLD_MS) {')
    L('      delay(20);')
    L('    }')
    L('    if (gpio_get_level(WAKEUP_PIN) == 0) {')
    L('      Serial.printf("Long press (>%lu ms) - MAINTENANCE MODE, staying awake %lu ms\\n",')
    L('        MAINTENANCE_HOLD_MS, MAINTENANCE_MS);')
    L('      Serial.println("Release button now. USB is live — flash a new sketch.");')
    L('      // Wait for release (max 30s, then proceed anyway)')
    L('      unsigned long relStart = millis();')
    L('      while (gpio_get_level(WAKEUP_PIN) == 0 && millis() - relStart < 30000) { delay(50); }')
    L('      delay(MAINTENANCE_MS);')
    L('      enterDeepSleep();  // does not return')
    L('    }')
    L()
    L('    // Cooldown: ignore short presses within COOLDOWN_US of last send.')
    L('    // Checked after maintenance so long-press reflashing always works.')
    L('    if (COOLDOWN_US > 0 && lastSendUs > 0) {')
    L('      struct timeval tv;')
    L('      gettimeofday(&tv, NULL);')
    L('      uint64_t nowUs = (uint64_t)tv.tv_sec * 1000000ULL + tv.tv_usec;')
    L('      if (nowUs > lastSendUs && (nowUs - lastSendUs) < COOLDOWN_US) {')
    L('        uint64_t remainMs = (COOLDOWN_US - (nowUs - lastSendUs)) / 1000;')
    L('        Serial.printf("Cooldown active - %llu ms remaining, ignoring press\\n", remainMs);')
    L('        enterDeepSleep();  // does not return')
    L('      }')
    L('    }')
    L('  } else if (cause == ESP_SLEEP_WAKEUP_TIMER) {')
    L('    Serial.printf("Timer wakeup - repeat %d/%d\\n", repeatIndex + 1, REPEAT_COUNT);')
    L('  } else {')
    L('    Serial.printf("Other wakeup cause: %d\\n", cause);')
    L('    repeatIndex = 0;')
    L('  }')
    L()

    L('  WiFi.persistent(false);')
    if ip_mode == "static":
        L('  WiFi.config(STATIC_IP, GATEWAY, SUBNET, DNS);')
    elif ip_mode == "dhcp_cache":
        L('  // Reuse cached DHCP lease (skips DHCP-DISCOVER, ~1s saved)')
        L('  if (hasCachedIP) {')
        L('    WiFi.config(IPAddress(cachedIP), IPAddress(cachedGW),')
        L('                IPAddress(cachedSN), IPAddress(cachedDNS));')
        L('  }')

    tx = TX_POWER_MAP.get(cfg["wifi_tx_power"], "WIFI_POWER_19_5dBm")
    L(f'  WiFi.setTxPower({tx});')
    L(f'  WiFi.setSleep({"true" if cfg["wifi_power_save"] else "false"});')
    L('  WiFi.mode(WIFI_STA);')
    L('  WiFi.setMinSecurity(WIFI_AUTH_WPA2_PSK);')
    L('  WiFi.setScanMethod(WIFI_FAST_SCAN);')
    L('  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);')
    L()
    L('  // Use cached BSSID + channel if available (skips scan)')
    L('  if (hasCachedWiFi && savedChannel > 0) {')
    L('    Serial.printf("Fast connect: CH %d, BSSID %02X:%02X:%02X:%02X:%02X:%02X\\n",')
    L('      savedChannel, savedBSSID[0], savedBSSID[1], savedBSSID[2],')
    L('      savedBSSID[3], savedBSSID[4], savedBSSID[5]);')
    L('    WiFi.begin(WIFI_SSID, WIFI_PASSWORD, savedChannel, savedBSSID);')
    L('  } else {')
    L('    Serial.println("No cache, full scan");')
    L('    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);')
    L('  }')
    L()
    L('  Serial.println("Connecting WiFi...");')
    L()
    L('  unsigned long start = millis();')
    L('  bool cacheRetried = false;')
    L('  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {')
    L('    // Cached connect failed? Retry once with full scan')
    L('    if (!cacheRetried && hasCachedWiFi && millis() - start > 4000) {')
    L('      Serial.println("Cache miss - fallback to full scan");')
    L('      cacheRetried = true;')
    L('      hasCachedWiFi = false;')
    L('      WiFi.disconnect();')
    L('      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);')
    L('    }')
    L('    delay(100);')
    L('    if ((millis() - start) % 1000 < 100) {')
    L('      Serial.printf("  WiFi status: %d (%lu ms)\\n", WiFi.status(), millis() - start);')
    L('    }')
    L('  }')
    L()
    L('  if (WiFi.status() == WL_CONNECTED) {')
    L('    savedChannel = WiFi.channel();')
    L('    memcpy(savedBSSID, WiFi.BSSID(), 6);')
    L('    hasCachedWiFi = true;')
    if ip_mode == "dhcp_cache":
        L('    if (!hasCachedIP) {')
        L('      cachedIP  = (uint32_t)WiFi.localIP();')
        L('      cachedGW  = (uint32_t)WiFi.gatewayIP();')
        L('      cachedSN  = (uint32_t)WiFi.subnetMask();')
        L('      cachedDNS = (uint32_t)WiFi.dnsIP();')
        L('      hasCachedIP = true;')
        L('      Serial.println("DHCP lease cached for next wake");')
        L('    }')
    L()
    L('    Serial.printf("WiFi OK (%lu ms), IP: %s, CH: %d\\n",')
    L('      millis() - start, WiFi.localIP().toString().c_str(), savedChannel);')
    L()
    L('    // Stamp cooldown timer at first send of a sequence (not on repeats)')
    L('    if (repeatIndex == 0) {')
    L('      struct timeval tv;')
    L('      gettimeofday(&tv, NULL);')
    L('      lastSendUs = (uint64_t)tv.tv_sec * 1000000ULL + tv.tv_usec;')
    L('    }')

    for i, pu in enumerate(parsed_urls):
        L(f'    sendHttpRequest(HTTP_HOST_{i}, HTTP_PORT_{i}, HTTP_PATH_{i}, "{pu["method"]}");')

    L()
    L('    repeatIndex++;')
    L('    if (repeatIndex < REPEAT_COUNT) {')
    L('      Serial.printf("Will repeat in %lu ms (%d/%d done)\\n",')
    L('        REPEAT_INTERVAL_MS, repeatIndex, REPEAT_COUNT);')
    L('      enterTimerSleep((uint64_t)REPEAT_INTERVAL_MS * 1000ULL);')
    L('    } else {')
    L('      repeatIndex = 0;')
    L('      enterDeepSleep();')
    L('    }')
    L('  } else {')
    L('    Serial.printf("WiFi FAILED after %lu ms, status: %d\\n", millis() - start, WiFi.status());')
    L('    hasCachedWiFi = false;')
    L('    savedChannel = 0;')
    L('    repeatIndex = 0;  // abort repeat sequence on failure')
    if ip_mode == "dhcp_cache":
        L('    hasCachedIP = false;  // Invalidate lease — next wake re-DHCPs')
    L('    Serial.println("Maintenance window");')
    L('    delay(MAINTENANCE_MS);')
    L('    enterDeepSleep();')
    L('  }')
    L('}')
    L()

    L('void loop() {')
    L('}')
    L()

    # ── HTTP Fire-and-Forget
    L('// ---- HTTP Fire-and-Forget ----')
    L('void sendHttpRequest(const char* host, int port, const char* path, const char* method) {')
    L('  Serial.printf("HTTP %s %s%s\\n", method, host, path);')
    L('  unsigned long httpStart = millis();')
    L()
    L('  WiFiClient client;')
    L('  if (client.connect(host, port, HTTP_TIMEOUT_MS)) {')
    L('    client.printf("%s %s HTTP/1.0\\r\\n"')
    L('                  "Host: %s\\r\\n"')
    L('                  "Connection: close\\r\\n\\r\\n",')
    L('                  method, path, host);')
    L('    client.flush();')
    L('    delay(100);')
    L('    client.stop();')
    L('    Serial.printf("HTTP sent (%lu ms)\\n", millis() - httpStart);')
    L('  } else {')
    L('    Serial.printf("HTTP connect failed (%lu ms)\\n", millis() - httpStart);')
    L('  }')
    L('}')
    L()

    # ── Deep Sleep (C6 specific)
    L('// ---- Deep Sleep (ESP32-C6) ----')
    L('void enterDeepSleep() {')
    L('  Serial.printf("Sleeping after %lu ms total uptime\\n", millis());')
    L()
    L('  WiFi.disconnect(true);')
    L('  WiFi.mode(WIFI_OFF);')
    L()
    L('  // Wait for button release so level-triggered wakeup does not fire immediately')
    L('  unsigned long relStart = millis();')
    L('  while (gpio_get_level(WAKEUP_PIN) == 0 && millis() - relStart < 5000) { delay(20); }')
    L('  Serial.flush();')
    L()
    L('  gpio_pullup_en(WAKEUP_PIN);')
    L('  gpio_pulldown_dis(WAKEUP_PIN);')
    L()
    L('  esp_deep_sleep_enable_gpio_wakeup(1ULL << WAKEUP_PIN, ESP_GPIO_WAKEUP_GPIO_LOW);')
    L()
    L('  esp_deep_sleep_start();')
    L('}')
    L()

    # ── Timer Sleep (between repeats; GPIO wake armed for cancel)
    L('// ---- Timer Sleep (between repeats, GPIO press cancels) ----')
    L('void enterTimerSleep(uint64_t us) {')
    L('  Serial.printf("Timer sleep %llu us, GPIO armed for cancel\\n", us);')
    L()
    L('  WiFi.disconnect(true);')
    L('  WiFi.mode(WIFI_OFF);')
    L()
    L('  // Wait for button release so GPIO wake does not fire immediately')
    L('  unsigned long releaseStart = millis();')
    L('  while (gpio_get_level(WAKEUP_PIN) == 0 && millis() - releaseStart < 5000) {')
    L('    delay(20);')
    L('  }')
    L('  Serial.flush();')
    L()
    L('  gpio_pullup_en(WAKEUP_PIN);')
    L('  gpio_pulldown_dis(WAKEUP_PIN);')
    L()
    L('  esp_deep_sleep_enable_gpio_wakeup(1ULL << WAKEUP_PIN, ESP_GPIO_WAKEUP_GPIO_LOW);')
    L('  esp_sleep_enable_timer_wakeup(us);')
    L()
    L('  esp_deep_sleep_start();')
    L('}')

    return "\n".join(lines)


# ── GUI ───────────────────────────────────────────────────────────────────────


class ButtonEditor(ttk.Frame):
    # Es gibt immer genau einen Button — daher flach (kein eigener Rahmen, kein
    # Entfernen/Hinzufügen) und ein Feld pro Zeile über die volle Breite, damit
    # nichts am Kartenrand abgeschnitten wird.
    def __init__(self, parent, index, data):
        super().__init__(parent)
        self.index = index
        host, webif_pass, request = parse_action_url(data.get("url", ""))
        self.columnconfigure(1, weight=1)

        rows = [
            ("Name:",         "name_var",    data.get("name", "Button 1")),
            ("Host (IP):",    "host_var",    host),
            ("WebIF-Pass:",   "pass_var",    webif_pass),
            ("Spot-Request:", "request_var", request),
        ]
        for r, (label, attr, value) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=value)
            setattr(self, attr, var)
            ttk.Entry(self, textvariable=var).grid(
                row=r, column=1, sticky="ew", padx=(4, 0), pady=2)

        ttk.Label(self, text="Methode GET · Pfad /cgi-bin/index.cgi",
                  style="Muted.TLabel").grid(row=len(rows), column=1, sticky="w",
                                             padx=(4, 0), pady=(2, 0))

    def get_data(self) -> dict:
        return {
            "name": self.name_var.get(),
            "url": compose_action_url(self.host_var.get(), self.pass_var.get(),
                                      self.request_var.get()),
            "method": "GET",
        }


class WifiButtonBuilder(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ESP32-C6 WiFi Button Builder")
        self.geometry("1340x880")
        self.minsize(1080, 660)

        self.config_data = dict(DEFAULT_CONFIG)
        self.button_editors: list[ButtonEditor] = []
        self.status_queue: queue.Queue = queue.Queue()
        # Connected boards, kept fresh by a background poller (see _device_poller).
        self._connected_macs: list[str] = []
        self._scan_snapshot = None

        self._build_ui()
        self._load_last_config()
        self._db_refresh()
        self._refresh_db_dropdowns(prefill=True)
        self.after(100, self._drain_status_queue)
        # Keep the shared DB in sync: pull on a loop and refresh the panel
        # whenever the file actually changes (no manual reload needed).
        threading.Thread(target=self._db_poller, daemon=True).start()
        # Continuously detect (un)plugged boards so the MAC dropdown stays live.
        threading.Thread(target=self._device_poller, daemon=True).start()

    @staticmethod
    def _db_signature():
        """Cheap fingerprint of the DB file to detect changes after a pull."""
        try:
            st = DB_PATH.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _db_poller(self):
        """Background loop: pull the shared DB and refresh the UI on change."""
        last_sig = None  # force one refresh on first pass
        while True:
            try:
                wb_git_pull()
                sig = self._db_signature()
                if sig != last_sig:
                    last_sig = sig
                    # Refresh on the main thread (see _drain_status_queue).
                    self.status_queue.put(("db_changed", None))
            except Exception:
                pass
            time.sleep(5)

    def _init_style(self):
        # Flat, modern light theme. clam is the only built-in ttk theme that
        # lets us recolor borders/fields away from the 90s 3D bevels.
        C = {
            "bg":      "#FFFFFF", "ink":    "#14181F", "muted":  "#6B7280",
            "border":  "#E5E7EB", "hover":  "#F3F4F6", "press":  "#E5E7EB",
            "accent":  "#0D9488", "accent2": "#0F766E", "sel":    "#CCFBF1",
            "stripe":  "#FAFBFC",
        }
        self.C = C
        import tkinter.font as tkfont
        fams = set(tkfont.families())

        def fam(cands):
            return next((c for c in cands if c in fams), cands[-1])

        ui = fam(["Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter",
                  "Cantarell", "DejaVu Sans", "TkDefaultFont"])
        mono = fam(["SF Mono", "Cascadia Code", "JetBrains Mono", "Consolas",
                    "Menlo", "DejaVu Sans Mono", "TkFixedFont"])
        self.F = {
            "ui": (ui, 11), "ui_bold": (ui, 11, "bold"), "small": (ui, 10),
            "small_bold": (ui, 10, "bold"), "head": (ui, 17, "bold"),
            "mono": (mono, 11),
        }
        self.configure(background=C["bg"])
        self.option_add("*Font", self.F["ui"])
        self.option_add("*TCombobox*Listbox.background", C["bg"])
        self.option_add("*TCombobox*Listbox.foreground", C["ink"])
        self.option_add("*TCombobox*Listbox.selectBackground", C["sel"])
        self.option_add("*TCombobox*Listbox.selectForeground", C["ink"])

        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        flat = dict(bordercolor=C["border"], lightcolor=C["border"],
                    darkcolor=C["border"])
        s.configure(".", background=C["bg"], foreground=C["ink"],
                    font=self.F["ui"], focuscolor=C["accent"])
        s.configure("TFrame", background=C["bg"])
        s.configure("TLabel", background=C["bg"], foreground=C["ink"])
        s.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"],
                    font=self.F["small"])
        s.configure("Status.TLabel", background=C["bg"], foreground=C["muted"],
                    font=self.F["small"])
        s.configure("Header.TLabel", background=C["bg"], foreground=C["ink"],
                    font=self.F["head"])
        s.configure("TLabelframe", background=C["bg"], relief="solid",
                    borderwidth=1, padding=12, **flat)
        s.configure("TLabelframe.Label", background=C["bg"],
                    foreground=C["accent"], font=self.F["small_bold"])
        s.configure("TButton", background=C["bg"], foreground=C["ink"],
                    borderwidth=1, relief="flat", padding=(10, 5),
                    font=self.F["ui"], **flat)
        s.map("TButton",
              background=[("pressed", C["press"]), ("active", C["hover"])],
              bordercolor=[("active", C["accent"])])
        s.configure("Accent.TButton", background=C["accent"], foreground="#FFFFFF",
                    padding=(12, 5), bordercolor=C["accent"],
                    lightcolor=C["accent"], darkcolor=C["accent"])
        s.map("Accent.TButton",
              background=[("pressed", C["accent2"]), ("active", C["accent2"])],
              foreground=[("disabled", "#C7CBD1")])
        s.configure("TEntry", fieldbackground=C["bg"], foreground=C["ink"],
                    borderwidth=1, padding=3, **flat)
        s.map("TEntry", bordercolor=[("focus", C["accent"])])
        s.configure("TCombobox", fieldbackground=C["bg"], background=C["bg"],
                    foreground=C["ink"], arrowcolor=C["muted"], borderwidth=1,
                    padding=4, **flat)
        s.map("TCombobox", bordercolor=[("focus", C["accent"])],
              fieldbackground=[("readonly", C["bg"])])
        s.configure("TCheckbutton", background=C["bg"], foreground=C["ink"])
        s.map("TCheckbutton", background=[("active", C["bg"])])
        s.configure("TRadiobutton", background=C["bg"], foreground=C["ink"])
        s.map("TRadiobutton", background=[("active", C["bg"])])
        s.configure("TSeparator", background=C["border"])
        s.configure("TPanedwindow", background=C["bg"])
        s.configure("Treeview", background=C["bg"], fieldbackground=C["bg"],
                    foreground=C["ink"], rowheight=28, borderwidth=0,
                    font=self.F["ui"])
        s.configure("Treeview.Heading", background=C["bg"], foreground=C["muted"],
                    relief="flat", borderwidth=0, padding=(8, 8),
                    font=self.F["small_bold"])
        s.map("Treeview.Heading", background=[("active", C["hover"])])
        s.map("Treeview", background=[("selected", C["sel"])],
              foreground=[("selected", C["ink"])])
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"{orient}.TScrollbar", background=C["hover"],
                        troughcolor=C["bg"], arrowcolor=C["muted"],
                        borderwidth=0, **flat)

    def _show_view(self, key: str):
        """Switch between the 'editor' and 'db' pages (segmented toggle)."""
        self.view_var.set(key)
        self.editor_view.pack_forget()
        self.db_view.pack_forget()
        view = self.editor_view if key == "editor" else self.db_view
        view.pack(fill="both", expand=True)
        for k, btn in self._nav_buttons.items():
            btn.configure(style="Accent.TButton" if k == key else "TButton")

    def _update_conn_indicator(self, macs):
        if macs:
            extra = f"   +{len(macs) - 1} weitere" if len(macs) > 1 else ""
            self.conn_label.configure(foreground=self.C["accent"])
            self.conn_var.set(f"●  Verbunden  ·  {macs[0]}{extra}")
        else:
            self.conn_label.configure(foreground=self.C["muted"])
            self.conn_var.set("○  Kein Taster verbunden")

    def _build_ui(self):
        self._init_style()

        # Header bar — app identity + live "button connected" indicator.
        header = ttk.Frame(self, padding=(16, 12))
        header.pack(side="top", fill="x")
        ttk.Label(header, text="WiFi-Button Builder",
                  style="Header.TLabel").pack(side="left")
        self.conn_var = tk.StringVar(value="○  Kein Taster verbunden")
        self.conn_label = ttk.Label(header, textvariable=self.conn_var,
                                    style="Muted.TLabel", font=self.F["ui_bold"])
        self.conn_label.pack(side="right")
        ttk.Separator(self, orient="horizontal").pack(side="top", fill="x")

        # Segmented page switcher: Konfiguration | Datenbank.
        nav = ttk.Frame(self, padding=(16, 8))
        nav.pack(side="top", fill="x")
        self.view_var = tk.StringVar(value="editor")
        self._nav_buttons = {}
        for key, label in (("editor", "⚙  Konfiguration"), ("db", "🗄  Datenbank")):
            b = ttk.Button(nav, text=label, command=lambda k=key: self._show_view(k))
            b.pack(side="left", padx=(0, 6))
            self._nav_buttons[key] = b
        ttk.Separator(self, orient="horizontal").pack(side="top", fill="x")

        # Statusbar at the very bottom (packed bottom-first so it sticks).
        self.status_var = tk.StringVar(value="Bereit.")
        ttk.Label(self, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w", padding=(16, 6)).pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")

        # Body holds two stacked pages; _show_view() shows one at a time.
        body = ttk.Frame(self)
        body.pack(side="top", fill="both", expand=True)
        self.editor_view = ttk.Frame(body, padding=(12, 12))
        self.db_view = ttk.Frame(body, padding=(12, 12))

        # Editor page: scrollable config form.
        canvas = tk.Canvas(self.editor_view, highlightthickness=0,
                           background=self.C["bg"])
        scrollbar = ttk.Scrollbar(self.editor_view, orient="vertical",
                                  command=canvas.yview)
        self.scroll_frame = ttk.Frame(canvas)

        self.scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        # Stretch the inner frame to the canvas width so cards fill the page.
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            if self.view_var.get() == "editor":
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", lambda e: self.view_var.get() == "editor" and canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: self.view_var.get() == "editor" and canvas.yview_scroll(1, "units"))

        # Aligned dashboard grid. Gerät spans full width; below it two equal,
        # flush card pairs — WiFi ↔ HTTP Aktionen and Timing&Wiederholung ↔
        # Taster. sticky="nsew" + uniform columns make paired cards share the
        # row's height, so their borders line up instead of ending ragged.
        grid = ttk.Frame(self.scroll_frame)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1, uniform="c")
        grid.columnconfigure(1, weight=1, uniform="c")

        dev = ttk.Frame(grid)
        dev.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._build_device_section(dev)

        wifi = ttk.Frame(grid)
        wifi.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=(0, 8))
        self._build_wifi_section(wifi)
        http = ttk.Frame(grid)
        http.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=(0, 8))
        self._build_buttons_section(http)

        beh = ttk.Frame(grid)
        beh.grid(row=2, column=0, sticky="nsew", padx=(0, 5))
        self._build_timing_section(beh)
        ser = ttk.Frame(grid)
        ser.grid(row=2, column=1, sticky="nsew", padx=(5, 0))
        self._build_flash_section(ser)

        self._build_actions(self.scroll_frame)

        # Database page.
        self._build_db_panel(self.db_view)

        self._show_view("editor")

    def _build_device_section(self, parent):
        f = ttk.LabelFrame(parent, text="Gerät (Adafruit Feather ESP32-C6)", padding=8)
        f.pack(fill="both", expand=True)

        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)

        ttk.Label(f, text="Device Name:").grid(row=0, column=0, sticky="w", pady=2)
        self.device_name_var = tk.StringVar(value=self.config_data["device_name"])
        ttk.Entry(f, textvariable=self.device_name_var).grid(
            row=0, column=1, sticky="ew", padx=(4, 16), pady=2)

        # MAC: pick a connected board or an existing DB device. Choosing a
        # known MAC loads that device's full config into the editor.
        ttk.Label(f, text="MAC:").grid(row=0, column=2, sticky="w", pady=2)
        self.mac_var = tk.StringVar()
        self.mac_combo = ttk.Combobox(f, textvariable=self.mac_var)
        self.mac_combo.grid(row=0, column=3, sticky="ew", padx=(4, 0), pady=2)
        self.mac_combo.bind("<<ComboboxSelected>>", self._on_mac_selected)

        ttk.Label(f, text="Kunde:").grid(row=1, column=0, sticky="w", pady=2)
        self.customer_var = tk.StringVar(value=self.config_data.get("customer", ""))
        self.customer_combo = ttk.Combobox(f, textvariable=self.customer_var)
        self.customer_combo.grid(row=1, column=1, sticky="ew", padx=(4, 16), pady=2)

        ttk.Label(f, text="Standort:").grid(row=1, column=2, sticky="w", pady=2)
        self.location_var = tk.StringVar(value=self.config_data.get("location", ""))
        self.location_combo = ttk.Combobox(f, textvariable=self.location_var)
        self.location_combo.grid(row=1, column=3, sticky="ew", padx=(4, 0), pady=2)

    def _build_wifi_section(self, parent):
        f = ttk.LabelFrame(parent, text="WiFi", padding=8)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)

        ttk.Label(f, text="SSID:").grid(row=0, column=0, sticky="w", pady=2)
        self.ssid_var = tk.StringVar(value=self.config_data["wifi_ssid"])
        ttk.Entry(f, textvariable=self.ssid_var).grid(
            row=0, column=1, sticky="ew", padx=(4, 16), pady=2)
        ttk.Label(f, text="Passwort:").grid(row=0, column=2, sticky="w", pady=2)
        self.pw_var = tk.StringVar(value=self.config_data["wifi_password"])
        ttk.Entry(f, textvariable=self.pw_var).grid(
            row=0, column=3, sticky="ew", padx=(4, 0), pady=2)

        self.ip_mode_var = tk.StringVar(value=self.config_data.get("ip_mode", "static"))
        mode_frame = ttk.Frame(f)
        mode_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 2))
        ttk.Label(mode_frame, text="IP-Modus:").pack(side="left")
        for label, val in [("Statisch", "static"), ("DHCP + IP-Cache", "dhcp_cache")]:
            ttk.Radiobutton(mode_frame, text=label, value=val,
                            variable=self.ip_mode_var,
                            command=self._on_ip_mode_change).pack(side="left", padx=(8, 0))

        # IP fields are editable comboboxes pre-filled with previously used
        # values from the DB (Gateway/Subnet/DNS rarely change per site).
        labels = [("IP:", "static_ip"), ("Gateway:", "gateway"),
                  ("Subnet:", "subnet"), ("DNS:", "dns")]
        self.ip_vars = {}
        self.ip_entries = {}
        for i, (label, key) in enumerate(labels):
            r = 2 + (i // 2)
            c = (i % 2) * 2
            ttk.Label(f, text=label).grid(row=r, column=c, sticky="w", pady=2)
            var = tk.StringVar(value=self.config_data[key])
            self.ip_vars[key] = var
            cb = ttk.Combobox(f, textvariable=var)
            cb.grid(row=r, column=c + 1, sticky="ew",
                    padx=(4, 16) if c == 0 else (4, 0), pady=2)
            self.ip_entries[key] = cb

        ttk.Label(f, text="TX Power:").grid(row=4, column=0, sticky="w", pady=2)
        self.tx_power_var = tk.StringVar(value=self.config_data["wifi_tx_power"])
        ttk.Combobox(f, textvariable=self.tx_power_var, values=list(TX_POWER_MAP.keys()),
                     width=10, state="readonly").grid(
            row=4, column=1, sticky="w", padx=(4, 16), pady=2)
        self.power_save_var = tk.BooleanVar(value=self.config_data["wifi_power_save"])
        ttk.Checkbutton(f, text="Power Save", variable=self.power_save_var).grid(
            row=4, column=2, columnspan=2, sticky="w", pady=2)

        self._on_ip_mode_change()

    def _on_ip_mode_change(self):
        # Manual IP fields are only relevant in "static" mode
        state = "normal" if self.ip_mode_var.get() == "static" else "disabled"
        for ent in self.ip_entries.values():
            ent.configure(state=state)

    def _build_timing_section(self, parent):
        f = ttk.LabelFrame(parent, text="Timing & Wiederholung", padding=8)
        f.pack(fill="both", expand=True)

        self.timing_vars = {
            "wifi_timeout_s": tk.IntVar(value=self.config_data["wifi_timeout_s"]),
            "http_timeout_s": tk.IntVar(value=self.config_data["http_timeout_s"]),
            "cooldown_s":     tk.IntVar(value=self.config_data["cooldown_s"]),
        }
        self.repeat_count_var = tk.IntVar(value=self.config_data.get("repeat_count", 1))
        self.repeat_interval_var = tk.IntVar(value=self.config_data.get("repeat_interval_s", 60))
        f.columnconfigure(4, weight=1)  # trailing spacer → hint spans full width

        def spin(r, c, label, lo, hi, var, width=6):
            ttk.Label(f, text=label).grid(row=r, column=c, sticky="w", pady=2)
            ttk.Spinbox(f, from_=lo, to=hi, textvariable=var, width=width).grid(
                row=r, column=c + 1, sticky="w", padx=(4, 18), pady=2)

        spin(0, 0, "WiFi Timeout (s):", 1, 120, self.timing_vars["wifi_timeout_s"])
        spin(0, 2, "HTTP Timeout (s):", 1, 120, self.timing_vars["http_timeout_s"])
        spin(1, 0, "Cooldown (s):", 0, 3600, self.timing_vars["cooldown_s"])
        spin(1, 2, "Anzahl Sends:", 1, 20, self.repeat_count_var)
        spin(2, 0, "Intervall (s):", 1, 3600, self.repeat_interval_var, width=8)

        ttk.Label(f, text="Cooldown: kurze Drücke nach dem Send ignorieren (0 = aus). "
                          "Anzahl Sends 1 = einmalig. Maintenance: Taste 5 s halten.",
                  style="Muted.TLabel", wraplength=340).grid(
            row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))

    def _build_flash_section(self, parent):
        f = ttk.LabelFrame(parent, text="Taster (USB-Serial)", padding=8)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(f, textvariable=self.port_var, state="readonly")
        self.port_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(4, 0))

        btns = ttk.Frame(f)
        btns.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.flash_button = ttk.Button(btns, text="⚡ Config senden",
                                       command=self._flash, style="Accent.TButton")
        self.flash_button.pack(side="left")
        ttk.Button(btns, text="📥 Auslesen", command=self._read_config,
                   style="Accent.TButton").pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="📋 Log", command=self._show_log,
                   style="Accent.TButton").pack(side="left", padx=(6, 0))

        ttk.Label(f, text="Feather ESP32-C6 mit Base-Image · per USB anstecken (Config-Modus).",
                  style="Muted.TLabel", wraplength=380).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_buttons_section(self, parent):
        # Genau eine Aktion pro Taster → kein Hinzufügen/Entfernen.
        self.buttons_frame = ttk.LabelFrame(parent, text="HTTP Aktion", padding=8)
        self.buttons_frame.pack(fill="both", expand=True)

        self.btn_container = ttk.Frame(self.buttons_frame)
        self.btn_container.pack(fill="both", expand=True)

        buttons = self.config_data.get("buttons") or [None]
        for btn_data in buttons:
            self._add_button(btn_data)

    def _add_button(self, data=None):
        if data is None:
            data = {"name": "Button 1", "url": "", "method": "GET"}
        editor = ButtonEditor(self.btn_container, len(self.button_editors), data)
        editor.pack(fill="x", pady=(0, 2))
        self.button_editors.append(editor)

    def _build_actions(self, parent):
        # Footer bar, anchored by a hairline so the buttons read as a toolbar and
        # not as loose controls. Left = Sketch-Code, right = Konfiguration.
        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=(2, 0))
        bar = ttk.Frame(parent, padding=(0, 8))
        bar.pack(fill="x")

        sketch = ttk.Frame(bar)
        sketch.pack(side="left")
        ttk.Button(sketch, text="Vorschau", command=self._preview).pack(side="left")
        ttk.Button(sketch, text="Export .ino", command=self._export).pack(side="left", padx=(6, 0))

        store = ttk.Frame(bar)
        store.pack(side="right")
        ttk.Button(store, text="Config laden", command=self._load_config).pack(side="left")
        ttk.Button(store, text="Config speichern", command=self._save_config).pack(side="left", padx=(6, 0))
        ttk.Button(store, text="In DB speichern", command=self._save_to_db).pack(side="left", padx=(6, 0))

    def _resolve_mac(self) -> str | None:
        """MAC for DB writes: the MAC field, else the connected board, else ask."""
        raw = self.mac_var.get().strip()
        if not raw:
            port = self.port_var.get().strip()
            raw = mac_from_port(port) if port else None
        if not raw:
            from tkinter import simpledialog
            raw = simpledialog.askstring(
                "MAC eingeben",
                "Keine MAC gewählt/gelesen (kein Board verbunden?).\n"
                "Bitte MAC manuell eingeben (Format AA:BB:CC:DD:EE:FF):",
                parent=self)
            if not raw:
                return None
        m = MAC_RE.search(raw.strip())
        if not m:
            messagebox.showerror("Ungültige MAC",
                                 f'"{raw}" ist keine gültige MAC-Adresse.')
            return None
        return m.group(1).upper()

    def _save_to_db(self):
        """Register the current config in the shared DB without flashing."""
        cfg = self._gather_config()
        errors = self._validate_config(cfg)
        if errors:
            messagebox.showerror("Ungültige Konfiguration", "\n".join(errors))
            return
        mac = self._resolve_mac()
        if not mac:
            return
        try:
            wb_register(mac, cfg, generate_ino(cfg))
        except Exception as e:
            messagebox.showerror("DB-Fehler", str(e))
            return
        where = "ptouch/labels.db" if DB_GIT_DIR else DB_PATH.name
        wb_git_push(f"wifi-button: {mac} {cfg.get('device_name', '')} (manuell)")
        self.mac_var.set(mac)
        self.status_var.set(f"In DB gespeichert ({where}): {mac}")
        self._db_refresh()
        self._refresh_db_dropdowns()

    def _validate_config(self, cfg: dict) -> list[str]:
        errors = []
        if not cfg["wifi_ssid"].strip():
            errors.append("SSID darf nicht leer sein.")
        if cfg["ip_mode"] == "static":
            for key, label in [("static_ip", "IP"), ("gateway", "Gateway"),
                                ("subnet", "Subnet"), ("dns", "DNS")]:
                try:
                    ipaddress.ip_address(cfg[key])
                except ValueError:
                    errors.append(f'{label} ist keine gültige IP-Adresse: "{cfg[key]}"')
        for i, btn in enumerate(cfg["buttons"], 1):
            host, _pass, request = parse_action_url(btn.get("url", ""))
            if not host.strip():
                errors.append(f"Button {i}: Host (IP) darf nicht leer sein.")
            if not request.strip():
                errors.append(f"Button {i}: Spot-Request darf nicht leer sein.")
        return errors

    def _gather_config(self) -> dict:
        return {
            "device_name": self.device_name_var.get(),
            "customer": self.customer_var.get(),
            "location": self.location_var.get(),
            "wifi_ssid": self.ssid_var.get(),
            "wifi_password": self.pw_var.get(),
            "ip_mode": self.ip_mode_var.get(),
            "static_ip": self.ip_vars["static_ip"].get(),
            "gateway": self.ip_vars["gateway"].get(),
            "subnet": self.ip_vars["subnet"].get(),
            "dns": self.ip_vars["dns"].get(),
            "wifi_timeout_s": self.timing_vars["wifi_timeout_s"].get(),
            "http_timeout_s": self.timing_vars["http_timeout_s"].get(),
            "cooldown_s": self.timing_vars["cooldown_s"].get(),
            "wifi_tx_power": self.tx_power_var.get(),
            "wifi_power_save": self.power_save_var.get(),
            "repeat_count": self.repeat_count_var.get(),
            "repeat_interval_s": self.repeat_interval_var.get(),
            "buttons": [ed.get_data() for ed in self.button_editors],
        }

    def _preview(self):
        cfg = self._gather_config()
        errors = self._validate_config(cfg)
        if errors:
            messagebox.showerror("Ungültige Konfiguration", "\n".join(errors))
            return
        code = generate_ino(cfg)

        win = tk.Toplevel(self)
        win.title(f"Vorschau — {cfg['device_name']}.ino")
        win.geometry("750x600")

        text_frame = ttk.Frame(win)
        text_frame.pack(fill="both", expand=True, padx=4, pady=4)

        text = tk.Text(text_frame, wrap="none", font=self.F["mono"],
                       background=self.C["bg"], foreground=self.C["ink"],
                       insertbackground=self.C["accent"], relief="flat",
                       borderwidth=0, highlightthickness=0, padx=10, pady=8)
        sy = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        sx = ttk.Scrollbar(text_frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        text.pack(side="left", fill="both", expand=True)

        text.insert("1.0", code)
        text.configure(state="disabled")

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=4, pady=4)
        ttk.Button(btn_frame, text="In Zwischenablage kopieren",
                   command=lambda: (self.clipboard_clear(), self.clipboard_append(code))).pack(side="left")

    def _export(self):
        cfg = self._gather_config()
        errors = self._validate_config(cfg)
        if errors:
            messagebox.showerror("Ungültige Konfiguration", "\n".join(errors))
            return
        code = generate_ino(cfg)
        name = cfg["device_name"].replace(" ", "_")

        path = filedialog.asksaveasfilename(
            defaultextension=".ino",
            filetypes=[("Arduino Sketch", "*.ino"), ("All files", "*.*")],
            initialfile=f"{name}.ino",
        )
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            f.write(code)

        messagebox.showinfo("Exportiert", f"Sketch gespeichert:\n{path}")
        self._auto_save_config()

    def _save_config(self):
        cfg = self._gather_config()
        errors = self._validate_config(cfg)
        if errors:
            messagebox.showerror("Ungültige Konfiguration", "\n".join(errors))
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile=f"{cfg['device_name']}.json",
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        self._auto_save_config()
        messagebox.showinfo("Gespeichert", f"Config gespeichert:\n{path}")

    def _load_config(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self._apply_config(cfg)

    def _apply_config(self, cfg: dict):
        self.device_name_var.set(cfg.get("device_name", "wifi-button"))
        self.customer_var.set(cfg.get("customer", ""))
        self.location_var.set(cfg.get("location", ""))
        self.ssid_var.set(cfg.get("wifi_ssid", ""))
        self.pw_var.set(cfg.get("wifi_password", ""))
        # Migrate legacy bool → enum, and legacy "dhcp" → "dhcp_cache"
        if "ip_mode" in cfg:
            mode = cfg["ip_mode"]
        else:
            mode = "static" if cfg.get("use_static_ip", True) else "dhcp_cache"
        if mode == "dhcp":
            mode = "dhcp_cache"
        self.ip_mode_var.set(mode)
        for key in ["static_ip", "gateway", "subnet", "dns"]:
            if key in self.ip_vars:
                self.ip_vars[key].set(cfg.get(key, ""))
        self._on_ip_mode_change()
        self.tx_power_var.set(cfg.get("wifi_tx_power", "20dBm"))
        self.power_save_var.set(cfg.get("wifi_power_save", False))
        self.repeat_count_var.set(cfg.get("repeat_count", 1))
        # Backward compat: legacy *_ms → seconds (rounded up, min 1)
        def _sec(key_s, key_ms, default_s):
            if key_s in cfg:
                return cfg[key_s]
            if key_ms in cfg:
                return max(1, cfg[key_ms] // 1000)
            return default_s
        self.repeat_interval_var.set(_sec("repeat_interval_s", "repeat_interval_ms", 60))
        timing_defaults = {
            "wifi_timeout_s": 10,
            "http_timeout_s": 3,
            "cooldown_s":     30,
        }
        for key_s, default_s in timing_defaults.items():
            if key_s in self.timing_vars:
                key_ms = key_s.replace("_s", "_ms")
                self.timing_vars[key_s].set(_sec(key_s, key_ms, default_s))

        for ed in self.button_editors:
            ed.destroy()
        self.button_editors.clear()
        for btn_data in (cfg.get("buttons") or [None]):
            self._add_button(btn_data)

    def _auto_save_config(self):
        cfg = self._gather_config()
        path = os.path.join(os.path.expanduser("~"), ".wifi_button_builder_last.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _load_last_config(self):
        path = os.path.join(os.path.expanduser("~"), ".wifi_button_builder_last.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self._apply_config(cfg)
            except Exception:
                pass

    # ── Status queue & device polling ──────────────────────────────────────

    def _post_status(self, msg: str):
        self.status_queue.put(("status", msg))

    def _drain_status_queue(self):
        try:
            while True:
                kind, payload = self.status_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "ports_changed":
                    ports, macs = payload
                    self._connected_macs = macs
                    self._update_conn_indicator(macs)
                    cur = self.port_var.get()
                    self.port_combo["values"] = ports
                    if cur not in ports:
                        self.port_var.set(ports[0] if ports else "")
                    self._refresh_db_dropdowns()
                elif kind == "db_changed":
                    if payload:
                        self.mac_var.set(payload)
                    self._db_refresh()
                    self._refresh_db_dropdowns()
        except queue.Empty:
            pass
        self.after(200, self._drain_status_queue)

    def _scan_devices(self, force: bool = False):
        """Detect connected boards' ports + MACs via pyserial (runs in a thread).

        Posts to the UI queue only when something changed (or when forced)."""
        ports, macs = [], []
        for entry in _detected_ports():
            p = entry.get("port", {})
            addr = p.get("address")
            if addr:
                ports.append(addr)
            sn = (p.get("properties") or {}).get("serialNumber", "")
            m = MAC_RE.search(sn or "")
            if m:
                macs.append(m.group(1).upper())
        snapshot = (tuple(ports), tuple(macs))
        if force or snapshot != self._scan_snapshot:
            self._scan_snapshot = snapshot
            self.status_queue.put(("ports_changed", (ports, macs)))

    def _device_poller(self):
        """Background loop: keep ports + MAC dropdown in sync with reality."""
        while True:
            try:
                self._scan_devices()
            except Exception:
                pass
            time.sleep(3)

    def _flash(self):
        cfg = self._gather_config()
        errors = self._validate_config(cfg)
        if errors:
            messagebox.showerror("Ungültige Konfiguration", "\n".join(errors))
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("Kein Port", "Bitte einen seriellen Port auswählen.")
            return

        self._auto_save_config()
        self._open_flash_log(port, cfg)

    def _open_flash_log(self, port: str, cfg: dict):
        win = tk.Toplevel(self)
        win.title(f"Config senden → {port}")
        win.geometry("780x500")

        text_frame = ttk.Frame(win)
        text_frame.pack(fill="both", expand=True, padx=4, pady=4)
        text = tk.Text(text_frame, wrap="none", font=self.F["mono"],
                       background=self.C["bg"], foreground=self.C["ink"],
                       insertbackground=self.C["accent"], relief="flat",
                       borderwidth=0, highlightthickness=0, padx=10, pady=8)
        sy = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)

        log_queue: queue.Queue = queue.Queue()

        def log_cb(msg: str):
            log_queue.put(msg)

        def drain():
            try:
                while True:
                    msg = log_queue.get_nowait()
                    text.insert("end", msg + "\n")
                    text.see("end")
            except queue.Empty:
                pass
            if win.winfo_exists():
                win.after(80, drain)

        def worker():
            try:
                ok = send_config_serial(port, cfg, log_cb)
                if ok:
                    self._register_flash(port, cfg, log_cb)
            except Exception as e:
                log_cb(f"✗ Exception: {e}")

        win.after(80, drain)
        threading.Thread(target=worker, daemon=True).start()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=4, pady=4)
        ttk.Button(btn_frame, text="Schließen", command=win.destroy).pack(side="right")

    def _read_config(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("Kein Port", "Bitte einen seriellen Port auswählen.")
            return
        self._open_read_log(port)

    def _open_read_log(self, port: str):
        win = tk.Toplevel(self)
        win.title(f"Taster auslesen ← {port}")
        win.geometry("780x500")

        text_frame = ttk.Frame(win)
        text_frame.pack(fill="both", expand=True, padx=4, pady=4)
        text = tk.Text(text_frame, wrap="none", font=self.F["mono"],
                       background=self.C["bg"], foreground=self.C["ink"],
                       insertbackground=self.C["accent"], relief="flat",
                       borderwidth=0, highlightthickness=0, padx=10, pady=8)
        sy = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)

        log_queue: queue.Queue = queue.Queue()

        def log_cb(msg: str):
            log_queue.put(msg)

        def drain():
            try:
                while True:
                    msg = log_queue.get_nowait()
                    text.insert("end", msg + "\n")
                    text.see("end")
            except queue.Empty:
                pass
            if win.winfo_exists():
                win.after(80, drain)

        def worker():
            try:
                cfg = read_config_serial(port, log_cb)
                if cfg is not None:
                    self.after(0, lambda: self._apply_read_config(cfg, port))
            except Exception as e:
                log_cb(f"✗ Exception: {e}")

        win.after(80, drain)
        threading.Thread(target=worker, daemon=True).start()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=4, pady=4)
        ttk.Button(btn_frame, text="Schließen", command=win.destroy).pack(side="right")

    def _show_log(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("Kein Port", "Bitte einen seriellen Port auswählen.")
            return
        self._open_log_window(port)

    def _open_log_window(self, port: str):
        win = tk.Toplevel(self)
        win.title(f"Serial Log ← {port}")
        win.geometry("780x540")

        text_frame = ttk.Frame(win)
        text_frame.pack(fill="both", expand=True, padx=4, pady=4)
        text = tk.Text(text_frame, wrap="none", font=self.F["mono"],
                       background=self.C["bg"], foreground=self.C["ink"],
                       insertbackground=self.C["accent"], relief="flat",
                       borderwidth=0, highlightthickness=0, padx=10, pady=8)
        sy = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)

        log_queue: queue.Queue = queue.Queue()
        stop_event = threading.Event()

        def log_cb(msg: str):
            log_queue.put(msg)

        def drain():
            try:
                while True:
                    msg = log_queue.get_nowait()
                    text.insert("end", msg + "\n")
                    text.see("end")
            except queue.Empty:
                pass
            if win.winfo_exists():
                win.after(80, drain)

        def start_stream(send_run: bool = False):
            stop_event.clear()
            threading.Thread(
                target=stream_serial_log,
                args=(port, log_cb, stop_event, send_run),
                daemon=True,
            ).start()

        def on_close():
            stop_event.set()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        win.after(80, drain)
        start_stream()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=4, pady=4)
        ttk.Button(btn_frame, text="▶ RUN (Test-Sendung)",
                   command=lambda: start_stream(send_run=True)).pack(side="left")
        ttk.Button(btn_frame, text="Leeren",
                   command=lambda: text.delete("1.0", "end")).pack(side="left", padx=(6, 0))
        ttk.Button(btn_frame, text="Stopp",
                   command=stop_event.set).pack(side="left", padx=(6, 0))
        ttk.Button(btn_frame, text="Schließen",
                   command=on_close).pack(side="right")

    def _apply_read_config(self, read_cfg: dict, port: str):
        """Ausgelesene Config nach Rückfrage ins Formular übernehmen und optional
        den DB-Eintrag aktualisieren. Felder, die das Base-Image nicht ausgibt
        (Passwort, Gerätename, Kunde/Standort, Cooldown), bleiben erhalten."""
        n = len(read_cfg.get("buttons", []))
        if messagebox.askyesno(
                "Ins Formular übernehmen?",
                f"{n} Button(s) ausgelesen. Das WLAN-Passwort gibt der Taster "
                "nicht aus und bleibt unverändert.\n\n"
                "Ausgelesene Config ins Formular übernehmen?"):
            merged = self._gather_config()
            merged.update(read_cfg)
            self._apply_config(merged)

        mac = mac_from_port(port)
        if not mac:
            return
        if not messagebox.askyesno(
                "DB aktualisieren?",
                f"DB-Eintrag für {mac} mit der ausgelesenen WLAN-/Button-Config "
                "aktualisieren?\n\nKunde/Standort/Gerätename, Passwort und der "
                "Flash-Zähler bleiben unverändert."):
            return
        try:
            result = wb_update_from_read(mac, read_cfg)
        except Exception as e:
            messagebox.showerror("DB-Fehler", str(e))
            return
        verb = "aktualisiert" if result == "updated" else "neu angelegt"
        where = "ptouch/labels.db" if DB_GIT_DIR else DB_PATH.name
        wb_git_push(f"wifi-button: {mac} aus Taster ausgelesen ({verb})")
        self.mac_var.set(mac)
        self.status_var.set(f"DB {verb} ({where}): {mac}")
        self._db_refresh()
        self._refresh_db_dropdowns()

    # ── Shared device DB ───────────────────────────────────────────────────

    def _register_flash(self, port: str, cfg: dict, log_cb):
        """After a successful flash, store the config in the shared DB.

        Runs in the flash worker thread, so UI updates are marshalled back to
        the main thread via self.after."""
        mac = mac_from_port(port)
        if not mac:
            log_cb("⚠ MAC nicht aus USB-Seriennummer lesbar — nicht in DB gespeichert.")
            return
        try:
            wb_register(mac, cfg, generate_ino(cfg))
        except Exception as e:
            log_cb(f"⚠ DB-Eintrag fehlgeschlagen: {e}")
            return
        where = "ptouch/labels.db" if DB_GIT_DIR else DB_PATH.name
        log_cb(f"✓ In DB gespeichert ({where}): {mac}")
        wb_git_push(f"wifi-button: {mac} {cfg.get('device_name', '')} geflasht")
        # Marshal the UI refresh onto the main thread via the status queue.
        self.status_queue.put(("db_changed", mac))

    # ── Inline device-DB panel (right pane) ───────────────────────────────

    def _build_db_panel(self, parent):
        src = "geteilt: ptouch/labels.db" if DB_GIT_DIR else f"lokal: {DB_PATH.name}"
        f = ttk.LabelFrame(parent, text="Datenbank (Klick = in Editor laden)", padding=6)
        f.pack(fill="both", expand=True)

        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Suche:").pack(side="left")
        self.db_search_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.db_search_var, width=22).pack(side="left", padx=(4, 6))
        ttk.Button(top, text="⬇ CSV", command=self._db_export_csv).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="⬇ DB", command=self._db_export_json).pack(side="left", padx=(4, 0))
        ttk.Button(top, text="⬆ Import…", command=self._db_import).pack(side="left", padx=(4, 0))
        self.db_search_var.trace_add("write", lambda *a: self._db_refresh())

        cols = ("customer", "location", "mac", "device_name", "ip", "request", "count")
        headings = {"customer": "Kunde", "location": "Standort", "mac": "MAC",
                    "device_name": "Gerät", "ip": "IP", "request": "Request",
                    "count": "#"}
        # Small base widths + stretch: the tree fills its pane instead of forcing
        # it wide, so the DB panel stays sane when the window isn't maximized.
        widths = {"customer": 90, "location": 80, "mac": 115, "device_name": 85,
                  "ip": 85, "request": 100, "count": 30}
        tree_frame = ttk.Frame(f)
        tree_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.db_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        sy = ttk.Scrollbar(tree_frame, orient="vertical", command=self.db_tree.yview)
        self.db_tree.configure(yscrollcommand=sy.set)
        for c in cols:
            self.db_tree.heading(c, text=headings[c])
            self.db_tree.column(c, width=widths[c], minwidth=30, anchor="w",
                                stretch=(c != "count"))
        self.db_tree.tag_configure("odd", background=self.C["stripe"])
        self.db_tree.tag_configure("even", background=self.C["bg"])
        sy.pack(side="right", fill="y")
        self.db_tree.pack(side="left", fill="both", expand=True)
        # Single click on a row → load that device into the editor.
        self.db_tree.bind("<<TreeviewSelect>>", lambda e: self._db_load_selected())

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="Details", command=self._db_show_details).pack(side="left")
        ttk.Button(btns, text="Notiz…", command=self._db_edit_notes).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Löschen", command=self._db_delete_selected).pack(side="left", padx=(6, 0))
        ttk.Label(f, text=src, foreground="gray").pack(anchor="w", pady=(4, 0))

    @staticmethod
    def _fmt_request(js: str) -> str:
        """Spot-Request des ersten Buttons (aus der gespeicherten URL gelöst —
        funktioniert auch für Bestandsgeräte)."""
        try:
            arr = json.loads(js) if js else []
        except Exception:
            arr = []
        if not arr or not isinstance(arr[0], dict):
            return ""
        _host, _pass, request = parse_action_url(arr[0].get("url", ""))
        return request + (f"  (+{len(arr) - 1})" if len(arr) > 1 else "")

    def _db_selected_mac(self):
        sel = self.db_tree.selection()
        return self.db_tree.item(sel[0], "values")[2] if sel else None

    def _db_refresh(self):
        if not hasattr(self, "db_tree"):
            return
        for iid in self.db_tree.get_children():
            self.db_tree.delete(iid)
        q = self.db_search_var.get().strip()
        try:
            rows = wb_search(q) if q else wb_all()
        except Exception as e:
            self.status_var.set(f"DB-Fehler: {e}")
            return
        for i, (cust, loc, mac, name, ip, _ssid, buttons, _last, cnt, _notes) in enumerate(rows):
            self.db_tree.insert("", "end",
                                 values=(cust, loc, mac, name, ip,
                                         self._fmt_request(buttons), cnt),
                                 tags=("odd" if i % 2 else "even",))

    def _refresh_db_dropdowns(self, prefill: bool = False):
        """Populate the customer/location/MAC/IP comboboxes from the DB.

        Only pre-fills empty network fields from the most-recent device when
        prefill=True (initial load / picking a new MAC). The 5s pollers call with
        prefill=False so they never overwrite a field the user is editing — e.g.
        clearing the IP to type a new one used to get refilled with a DB value."""
        if not hasattr(self, "customer_combo"):
            return
        self.customer_combo["values"] = wb_distinct("customer")
        self.location_combo["values"] = wb_distinct("location")
        # MAC dropdown: connected boards first, then known DB devices. Each
        # entry shows its Kunde/Standort (from the builder or PTouch) so a
        # board is recognisable without remembering its MAC.
        labels = wb_mac_labels()
        # Union: connected boards, then every MAC known to the shared DB
        # (flashed devices AND MACs only tagged in PTouch).
        seen, mac_values = set(), []
        for m in self._connected_macs + wb_macs() + list(labels.keys()):
            if m in seen:
                continue
            seen.add(m)
            lbl = labels.get(m, "")
            mac_values.append(f"{m}  —  {lbl}" if lbl else m)
        self.mac_combo["values"] = mac_values
        for key in ("static_ip", "gateway", "subnet", "dns"):
            vals = wb_distinct("ip" if key == "static_ip" else key)
            self.ip_entries[key]["values"] = vals
            # Carry network settings over: fill an empty field with the newest.
            # Guarded so background pollers can't clobber an in-progress edit.
            if prefill and vals and not self.ip_vars[key].get().strip():
                self.ip_vars[key].set(vals[0])

    def _on_mac_selected(self, _evt=None):
        """Picking a MAC loads its full config, or at least its Kunde/Standort."""
        m = MAC_RE.search(self.mac_var.get())
        if not m:
            return
        mac = m.group(1).upper()
        self.mac_var.set(mac)
        cfg = wb_get_config(mac)
        if cfg:
            self._apply_config(cfg)
            self.mac_var.set(mac)
            self.status_var.set(f"DB: Config von {mac} geladen")
            return
        # No full config yet (e.g. only tagged in PTouch) → prefill Kunde/Standort
        # and suggest an editable Device Name so the previous button's name does
        # not silently carry over.
        cust, loc = wb_get_meta(mac)
        if cust:
            self.customer_var.set(cust)
        if loc:
            self.location_var.set(loc)
        self.device_name_var.set(suggest_device_name(mac) or "wifi-button")
        # Carry the site's network settings (gateway/subnet/dns/IP) into the
        # fresh button — explicit user action, so prefilling empty fields is wanted.
        self._refresh_db_dropdowns(prefill=True)
        self.status_var.set(f"{mac}: Kunde/Standort aus DB übernommen" if (cust or loc)
                            else f"{mac}: noch keine Daten in der DB")

    def _db_load_selected(self):
        mac = self._db_selected_mac()
        if not mac:
            return
        cfg = wb_get_config(mac)
        if not cfg:
            return
        self._apply_config(cfg)
        self.mac_var.set(mac)
        self.status_var.set(f"DB: Config von {mac} geladen")

    def _db_delete_selected(self):
        mac = self._db_selected_mac()
        if not mac:
            return
        vals = self.db_tree.item(self.db_tree.selection()[0], "values")
        if not messagebox.askyesno("Löschen", f"{mac} ({vals[3]}) aus der DB löschen?"):
            return
        wb_delete(mac)
        wb_git_push(f"wifi-button: {mac} gelöscht")
        self._db_refresh()
        self._refresh_db_dropdowns()

    def _db_edit_notes(self):
        mac = self._db_selected_mac()
        if not mac:
            return
        from tkinter import simpledialog
        con = wb_db()
        row = con.execute("SELECT COALESCE(notes,'') FROM wifi_buttons WHERE mac=?", (mac,)).fetchone()
        con.close()
        new = simpledialog.askstring("Notiz", f"Notiz für {mac}:",
                                     initialvalue=row[0] if row else "", parent=self)
        if new is None:
            return
        wb_set_notes(mac, new)
        wb_git_push(f"wifi-button: Notiz {mac}")
        self._db_refresh()

    def _db_show_details(self):
        mac = self._db_selected_mac()
        if not mac:
            return
        cfg = wb_get_config(mac)
        ino = wb_get_ino(mac)
        dwin = tk.Toplevel(self)
        dwin.title(f"Details — {mac}")
        dwin.geometry("720x560")
        nb = ttk.Notebook(dwin)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        # Aktionen: Host / WebIF-Pass / Spot-Request je Button, aus der URL
        # gelöst (auch für Bestandsgeräte).
        lines = []
        for i, btn in enumerate((cfg or {}).get("buttons", []), 1):
            host, webif_pass, request = parse_action_url(btn.get("url", ""))
            lines.append(f"Button {i}: {btn.get('name', '')}")
            lines.append(f"  Host (IP):    {host}")
            lines.append(f"  WebIF-Pass:   {webif_pass}")
            lines.append(f"  Spot-Request: {request}")
            lines.append(f"  URL:          {btn.get('url', '')}")
            lines.append("")
        actions = "\n".join(lines).rstrip() or "—"

        for label, content in [
            ("Aktionen", actions),
            ("Config (JSON)", json.dumps(cfg, indent=2, ensure_ascii=False) if cfg else "—"),
            ("Sketch (.ino)", ino or "— (vor diesem Update geflasht)"),
        ]:
            frame = ttk.Frame(nb)
            nb.add(frame, text=label)
            txt = tk.Text(frame, wrap="none", font=self.F["mono"],
                          background=self.C["bg"], foreground=self.C["ink"],
                          insertbackground=self.C["accent"], relief="flat",
                          borderwidth=0, highlightthickness=0, padx=10, pady=8)
            sv = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
            txt.configure(yscrollcommand=sv.set)
            sv.pack(side="right", fill="y")
            txt.pack(side="left", fill="both", expand=True)
            txt.insert("1.0", content)
            txt.configure(state="disabled")

    def _db_export_csv(self):
        rows = wb_export_rows(self.db_search_var.get().strip())
        if not rows:
            messagebox.showinfo("Leer", "Keine Daten zum Exportieren.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialfile=f"wifi-buttons_{datetime.now():%Y%m%d}.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            wr = csv.writer(fh, delimiter=";")
            wr.writerow(WB_EXPORT_COLS)
            wr.writerows(rows)
        messagebox.showinfo("Exportiert", f"CSV gespeichert:\n{path}")

    def _db_export_json(self):
        """Lossless JSON export of the filtered devices (for technician → merge)."""
        bundle = wb_export_db(self.db_search_var.get().strip())
        if not bundle["devices"]:
            messagebox.showinfo("Leer", "Keine Daten zum Exportieren.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".wbdb.json",
            filetypes=[("WiFi-Button DB", "*.wbdb.json"),
                       ("JSON", "*.json"), ("All files", "*.*")],
            initialfile=f"wifi-buttons_{datetime.now():%Y%m%d}.wbdb.json")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, ensure_ascii=False, indent=2)
        messagebox.showinfo(
            "Exportiert",
            f"{len(bundle['devices'])} Geräte gespeichert:\n{path}")

    def _db_import(self):
        """Pick a *.wbdb.json file, classify against the DB, show preview."""
        path = filedialog.askopenfilename(
            filetypes=[("WiFi-Button DB", "*.wbdb.json"),
                       ("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                bundle = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Fehler", f"Datei nicht lesbar:\n{e}")
            return
        if not isinstance(bundle, dict) or bundle.get("format") != WB_EXPORT_FORMAT:
            messagebox.showerror("Falsches Format",
                                 "Das ist keine WiFi-Button-DB-Exportdatei.")
            return
        items, skipped = [], 0
        for rec in bundle.get("devices") or []:
            if not (rec.get("mac") or "").strip():
                skipped += 1
                continue
            items.append((wb_diff_record(rec), rec))
        if not items:
            messagebox.showinfo("Nichts zu importieren",
                                "Keine gültigen Geräte in der Datei.")
            return
        self._show_import_preview(items, skipped)

    def _show_import_preview(self, items, skipped):
        """Modal preview: tick which devices to merge (new/changed pre-ticked)."""
        win = tk.Toplevel(self)
        win.title("Import — Vorschau")
        win.geometry("700x470")
        win.transient(self)
        win.grab_set()

        ttk.Label(
            win, wraplength=670, foreground="gray",
            text=("Klick auf eine Zeile schaltet das Häkchen um. Konflikte "
                  "(ÄNDERT) überschreiben deine Version; first_seen und der "
                  "höhere Flash-Zähler bleiben erhalten."),
        ).pack(anchor="w", padx=8, pady=(8, 4))

        cols = ("sel", "status", "mac", "device", "where")
        tree = ttk.Treeview(win, columns=cols, show="headings", selectmode="none")
        for c, h, wdt in (("sel", "✓", 34), ("status", "Status", 80),
                          ("mac", "MAC", 150), ("device", "Gerät", 130),
                          ("where", "Kunde/Standort", 200)):
            tree.heading(c, text=h)
            tree.column(c, width=wdt, anchor="w")
        sy = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True, padx=8)

        labels = {"new": "NEU", "changed": "ÄNDERT", "same": "IDENTISCH"}
        checked, rec_by_iid = {}, {}
        for status, rec in items:
            on = status in ("new", "changed")
            where = " / ".join(x for x in (rec.get("customer", ""),
                                           rec.get("location", "")) if x)
            iid = tree.insert("", "end", values=(
                "☑" if on else "☐", labels[status], rec["mac"].upper(),
                rec.get("device_name", ""), where))
            checked[iid] = on
            rec_by_iid[iid] = rec

        import_btn = ttk.Button(win, text="Importieren")

        def update_btn():
            n = sum(1 for v in checked.values() if v)
            import_btn.configure(text=f"Importieren ({n})",
                                 state="normal" if n else "disabled")

        def toggle(event):
            iid = tree.identify_row(event.y)
            if not iid or tree.identify_region(event.x, event.y) != "cell":
                return
            checked[iid] = not checked[iid]
            vals = list(tree.item(iid, "values"))
            vals[0] = "☑" if checked[iid] else "☐"
            tree.item(iid, values=vals)
            update_btn()
        tree.bind("<Button-1>", toggle)

        def do_import():
            sel = {rec_by_iid[i]["mac"].upper()
                   for i, on in checked.items() if on}
            records = list(rec_by_iid.values())
            added, updated = wb_import_records(records, sel)
            win.destroy()
            if added or updated:
                wb_git_push(f"import: {added} neu, {updated} aktualisiert")
                self._db_refresh()
                self._refresh_db_dropdowns()
            messagebox.showinfo("Importiert",
                                f"{added} neu, {updated} aktualisiert.")

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=8, pady=8)
        note = f"  ({skipped} ohne MAC übersprungen)" if skipped else ""
        ttk.Label(bar, text=f"{len(items)} Geräte{note}",
                  foreground="gray").pack(side="left")
        import_btn.configure(command=do_import)
        import_btn.pack(in_=bar, side="right")
        ttk.Button(bar, text="Abbrechen", command=win.destroy).pack(
            side="right", padx=(0, 6))
        update_btn()


if __name__ == "__main__":
    app = WifiButtonBuilder()
    app.mainloop()
