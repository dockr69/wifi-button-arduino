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
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import queue
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# ESP32-C6 (USB-Serial-JTAG) puts the base MAC into the USB descriptor
# serial number, so we can read it without any firmware running.
MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")

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

# Arduino CLI target board
FQBN = "esp32:esp32:adafruit_feather_esp32c6"

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
    DB_PATH = SCRIPT_DIR / "buttons.db"
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
            check=False, timeout=15, capture_output=True)
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
                capture_output=True, timeout=10)
            if r.returncode != 0:
                return
            r = subprocess.run(
                ["git", "-C", str(DB_GIT_DIR), "diff", "--cached", "--quiet"],
                capture_output=True)
            if r.returncode == 0:
                return  # nothing staged
            subprocess.run(
                ["git", "-C", str(DB_GIT_DIR), "commit", "-m", message],
                capture_output=True, timeout=10)
            subprocess.run(
                ["git", "-C", str(DB_GIT_DIR), "pull", "--rebase",
                 "--autostash", "--quiet"],
                capture_output=True, timeout=15)
            subprocess.run(
                ["git", "-C", str(DB_GIT_DIR), "push", "--quiet"],
                capture_output=True, timeout=20)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


# ── Arduino CLI integration ───────────────────────────────────────────────────

CLI_HOME = Path.home() / ".wifi-button-builder"
CLI_BIN_NAME = "arduino-cli.exe" if sys.platform == "win32" else "arduino-cli"
CLI_BIN = CLI_HOME / "bin" / CLI_BIN_NAME
CLI_DATA = CLI_HOME / "data"
CLI_CFG_DIR = CLI_HOME / "config"


def arduino_cli_path() -> Path | None:
    """Return path to a usable arduino-cli, preferring our managed copy."""
    if CLI_BIN.exists():
        return CLI_BIN
    sys_path = shutil.which("arduino-cli")
    return Path(sys_path) if sys_path else None


def _cli_download_url() -> str:
    """Build the official arduino-cli download URL for the current platform."""
    base = "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest"
    if sys.platform == "darwin":
        suffix = "macOS_ARM64.tar.gz" if "arm" in os.uname().machine.lower() else "macOS_64bit.tar.gz"
    elif sys.platform == "win32":
        suffix = "Windows_64bit.zip"
    else:
        suffix = "Linux_64bit.tar.gz"
    return f"{base}_{suffix}"


def download_arduino_cli(log_cb):
    """Download + extract arduino-cli into CLI_BIN. log_cb(str) for progress."""
    url = _cli_download_url()
    log_cb(f"Downloading arduino-cli from {url}")
    CLI_BIN.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(url).suffix) as tmp:
        tmp_path = Path(tmp.name)
    try:
        urllib.request.urlretrieve(url, tmp_path)
        log_cb(f"Downloaded {tmp_path.stat().st_size // 1024} KB, extracting…")
        if url.endswith(".zip"):
            with zipfile.ZipFile(tmp_path) as zf:
                for member in zf.namelist():
                    if member.endswith(CLI_BIN_NAME):
                        with zf.open(member) as src, open(CLI_BIN, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        break
        else:
            with tarfile.open(tmp_path, "r:gz") as tf:
                for member in tf.getmembers():
                    if member.name.endswith(CLI_BIN_NAME):
                        with tf.extractfile(member) as src, open(CLI_BIN, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        break
        if sys.platform != "win32":
            CLI_BIN.chmod(0o755)
        log_cb(f"arduino-cli installed: {CLI_BIN}")
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _cli_base_args() -> list[str]:
    """Common args isolating arduino-cli state to our home dir."""
    CLI_DATA.mkdir(parents=True, exist_ok=True)
    CLI_CFG_DIR.mkdir(parents=True, exist_ok=True)
    return [
        str(arduino_cli_path()),
        "--config-dir", str(CLI_CFG_DIR),
    ]


def run_cli(args: list[str], log_cb) -> int:
    """Run arduino-cli with given args, stream output via log_cb. Returns exit code."""
    cmd = _cli_base_args() + args
    log_cb(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        log_cb(line.rstrip())
    proc.wait()
    return proc.returncode


def ensure_esp32_core(log_cb) -> bool:
    """Install esp32:esp32 core if missing. Returns True on success."""
    # First, ensure the board manager URL is set (arduino-cli ships without it)
    # `core list` triggers config-load; we use a dedicated check via `core search`.
    try:
        out = subprocess.check_output(
            _cli_base_args() + ["core", "list", "--format", "json"],
            text=True, stderr=subprocess.STDOUT,
        )
        data = json.loads(out)
        platforms = data.get("platforms", []) if isinstance(data, dict) else data
        for p in platforms:
            if p.get("id") == "esp32:esp32" and p.get("installed_version"):
                log_cb(f"esp32:esp32 already installed (v{p['installed_version']})")
                return True
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as e:
        log_cb(f"core list check failed: {e}")

    log_cb("Updating package index…")
    if run_cli(["core", "update-index"], log_cb) != 0:
        log_cb("core update-index failed")
        return False
    log_cb("Installing esp32:esp32 core (this downloads ~250MB, takes a while)…")
    return run_cli(["core", "install", "esp32:esp32"], log_cb) == 0


def _detected_ports() -> list[dict]:
    """Return arduino-cli's detected_ports entries (raw)."""
    cli = arduino_cli_path()
    if not cli:
        return []
    try:
        out = subprocess.check_output(
            _cli_base_args() + ["board", "list", "--format", "json"],
            text=True, stderr=subprocess.STDOUT, timeout=10,
        )
        data = json.loads(out)
        ports = data.get("detected_ports", []) if isinstance(data, dict) else data
        return ports or []
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []


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


def compile_and_upload(sketch_dir: Path, port: str, log_cb) -> bool:
    """Compile sketch and upload to port. Returns True on success."""
    log_cb(f"Compiling for {FQBN}…")
    if run_cli(["compile", "--fqbn", FQBN, str(sketch_dir)], log_cb) != 0:
        log_cb("✗ Compile failed")
        return False
    log_cb(f"Uploading to {port}…")
    if run_cli(["upload", "-p", port, "--fqbn", FQBN, str(sketch_dir)], log_cb) != 0:
        log_cb("✗ Upload failed")
        return False
    log_cb("✓ Flash erfolgreich")
    return True


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


class ButtonEditor(ttk.LabelFrame):
    def __init__(self, parent, index, data, on_delete):
        super().__init__(parent, text=f"Button {index + 1}", padding=6)
        self.index = index
        self.on_delete = on_delete

        row = 0
        ttk.Label(self, text="Name:").grid(row=row, column=0, sticky="w")
        self.name_var = tk.StringVar(value=data.get("name", f"Button {index + 1}"))
        ttk.Entry(self, textvariable=self.name_var, width=30).grid(row=row, column=1, sticky="ew", padx=(4, 0))

        row += 1
        ttk.Label(self, text="URL:").grid(row=row, column=0, sticky="w")
        self.url_var = tk.StringVar(value=data.get("url", ""))
        ttk.Entry(self, textvariable=self.url_var, width=60).grid(row=row, column=1, columnspan=2, sticky="ew", padx=(4, 0))

        row += 1
        ttk.Label(self, text="Method:").grid(row=row, column=0, sticky="w")
        self.method_var = tk.StringVar(value=data.get("method", "GET"))
        ttk.Combobox(self, textvariable=self.method_var, values=["GET", "POST"], width=8, state="readonly").grid(
            row=row, column=1, sticky="w", padx=(4, 0)
        )
        ttk.Button(self, text="✕ Entfernen", command=self._delete, width=14).grid(row=row, column=2, sticky="e", padx=(8, 0))

        self.columnconfigure(1, weight=1)

    def _delete(self):
        self.on_delete(self.index)

    def get_data(self) -> dict:
        return {
            "name": self.name_var.get(),
            "url": self.url_var.get(),
            "method": self.method_var.get(),
        }


class WifiButtonBuilder(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ESP32-C6 WiFi Button Builder")
        self.geometry("1340x880")
        self.minsize(1080, 660)

        self.config_data = dict(DEFAULT_CONFIG)
        self.button_editors: list[ButtonEditor] = []
        self.bootstrap_ready = False
        self.status_queue: queue.Queue = queue.Queue()
        # Connected boards, kept fresh by a background poller (see _device_poller).
        self._connected_macs: list[str] = []
        self._scan_snapshot = None

        self._build_ui()
        self._load_last_config()
        self._db_refresh()
        self._refresh_db_dropdowns()
        self.after(100, self._drain_status_queue)
        threading.Thread(target=self._bootstrap, daemon=True).start()
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

    def _build_ui(self):
        style = ttk.Style()
        style.configure("TLabelframe.Label", font=("", 10, "bold"))

        # Statusbar at the very bottom (packed first with side=bottom so it sticks)
        self.status_var = tk.StringVar(value="Starte arduino-cli Bootstrap…")
        status = ttk.Label(self, textvariable=self.status_var,
                           relief="sunken", anchor="w", padding=(6, 2))
        status.pack(side="bottom", fill="x")

        # Split window: config editor (left) | always-on device DB (right).
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(side="top", fill="both", expand=True)

        left = ttk.Frame(paned, padding=(8, 8, 4, 8))
        right = ttk.Frame(paned, padding=(4, 8, 8, 8))
        paned.add(left, weight=3)
        paned.add(right, weight=2)

        canvas = tk.Canvas(left, highlightthickness=0)
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=canvas.yview)
        self.scroll_frame = ttk.Frame(canvas)

        self.scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        self._build_device_section()
        self._build_wifi_section()
        self._build_timing_section()
        self._build_repeat_section()
        self._build_buttons_section()
        self._build_flash_section()
        self._build_actions()

        self._build_db_panel(right)

    def _build_device_section(self):
        f = ttk.LabelFrame(self.scroll_frame, text="Gerät (Adafruit Feather ESP32-C6)", padding=8)
        f.pack(fill="x", pady=(0, 6))

        ttk.Label(f, text="Device Name:").grid(row=0, column=0, sticky="w")
        self.device_name_var = tk.StringVar(value=self.config_data["device_name"])
        ttk.Entry(f, textvariable=self.device_name_var, width=28).grid(row=0, column=1, sticky="w", padx=(4, 0))

        # MAC: pick a connected board or an existing DB device. Choosing a
        # known MAC loads that device's full config into the editor.
        ttk.Label(f, text="MAC:").grid(row=0, column=2, sticky="e", pady=(4, 0))
        self.mac_var = tk.StringVar()
        self.mac_combo = ttk.Combobox(f, textvariable=self.mac_var, width=24)
        self.mac_combo.grid(row=0, column=3, sticky="w", padx=(4, 0), pady=(4, 0))
        self.mac_combo.bind("<<ComboboxSelected>>", self._on_mac_selected)

        ttk.Label(f, text="Kunde:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.customer_var = tk.StringVar(value=self.config_data.get("customer", ""))
        self.customer_combo = ttk.Combobox(f, textvariable=self.customer_var, width=28)
        self.customer_combo.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))

        ttk.Label(f, text="Standort:").grid(row=1, column=2, sticky="e", pady=(4, 0))
        self.location_var = tk.StringVar(value=self.config_data.get("location", ""))
        self.location_combo = ttk.Combobox(f, textvariable=self.location_var, width=24)
        self.location_combo.grid(row=1, column=3, sticky="w", padx=(4, 0), pady=(4, 0))

    def _build_wifi_section(self):
        f = ttk.LabelFrame(self.scroll_frame, text="WiFi", padding=8)
        f.pack(fill="x", pady=(0, 6))

        row = 0
        ttk.Label(f, text="SSID:").grid(row=row, column=0, sticky="w")
        self.ssid_var = tk.StringVar(value=self.config_data["wifi_ssid"])
        ttk.Entry(f, textvariable=self.ssid_var, width=28).grid(row=row, column=1, sticky="w", padx=(4, 0))

        ttk.Label(f, text="  Passwort:").grid(row=row, column=2, sticky="w")
        self.pw_var = tk.StringVar(value=self.config_data["wifi_password"])
        ttk.Entry(f, textvariable=self.pw_var, width=28).grid(row=row, column=3, sticky="w", padx=(4, 0))

        row += 1
        self.ip_mode_var = tk.StringVar(value=self.config_data.get("ip_mode", "static"))
        mode_frame = ttk.Frame(f)
        mode_frame.grid(row=row, column=0, columnspan=8, sticky="w", pady=(4, 0))
        ttk.Label(mode_frame, text="IP-Modus:").pack(side="left")
        for label, val in [
            ("Statisch",         "static"),
            ("DHCP + IP-Cache",  "dhcp_cache"),
        ]:
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
            r = row + 1 + (i // 2)
            c = (i % 2) * 2
            ttk.Label(f, text=label).grid(row=r, column=c, sticky="w", pady=(4, 0))
            var = tk.StringVar(value=self.config_data[key])
            self.ip_vars[key] = var
            cb = ttk.Combobox(f, textvariable=var, width=18)
            cb.grid(row=r, column=c + 1, sticky="w", padx=(4, 12), pady=(4, 0))
            self.ip_entries[key] = cb
        row += 2
        self._on_ip_mode_change()

        row += 1
        ttk.Label(f, text="TX Power:").grid(row=row, column=0, sticky="w", pady=(4, 0))
        self.tx_power_var = tk.StringVar(value=self.config_data["wifi_tx_power"])
        ttk.Combobox(f, textvariable=self.tx_power_var, values=list(TX_POWER_MAP.keys()), width=10, state="readonly").grid(
            row=row, column=1, sticky="w", padx=(4, 0), pady=(4, 0)
        )

        self.power_save_var = tk.BooleanVar(value=self.config_data["wifi_power_save"])
        ttk.Checkbutton(f, text="Power Save", variable=self.power_save_var).grid(row=row, column=2, columnspan=2, sticky="w", pady=(4, 0))

    def _on_ip_mode_change(self):
        # Manual IP fields are only relevant in "static" mode
        state = "normal" if self.ip_mode_var.get() == "static" else "disabled"
        for ent in self.ip_entries.values():
            ent.configure(state=state)

    def _build_timing_section(self):
        f = ttk.LabelFrame(self.scroll_frame, text="Timing (Sekunden)", padding=8)
        f.pack(fill="x", pady=(0, 6))

        self.timing_vars = {}
        items = [
            ("WiFi Timeout:", "wifi_timeout_s", 1, 120),
            ("HTTP Timeout:", "http_timeout_s", 1, 120),
            ("Cooldown:",     "cooldown_s",    0, 3600),
        ]
        for i, (label, key, lo, hi) in enumerate(items):
            ttk.Label(f, text=label).grid(row=0, column=i * 2, sticky="w")
            var = tk.IntVar(value=self.config_data[key])
            self.timing_vars[key] = var
            ttk.Spinbox(f, from_=lo, to=hi, increment=1, textvariable=var, width=6).grid(
                row=0, column=i * 2 + 1, sticky="w", padx=(4, 12)
            )
        ttk.Label(f, text="Maintenance: Taste 5 s halten → bleibt 5 s wach zum Reflashen (fest).",
                  foreground="gray").grid(row=1, column=0, columnspan=8, sticky="w", pady=(4, 0))
        ttk.Label(f, text="Cooldown: kurze Tastendrücke innerhalb dieser Zeit nach dem letzten Send werden ignoriert (0 = aus). Langer Druck → Maintenance bleibt immer aktiv.",
                  foreground="gray", wraplength=820).grid(row=2, column=0, columnspan=8, sticky="w", pady=(2, 0))

    def _build_repeat_section(self):
        f = ttk.LabelFrame(self.scroll_frame,
                           text="Wiederholung (Tastendruck während Sequenz = Cancel)", padding=8)
        f.pack(fill="x", pady=(0, 6))

        ttk.Label(f, text="Anzahl Sends:").grid(row=0, column=0, sticky="w")
        self.repeat_count_var = tk.IntVar(value=self.config_data.get("repeat_count", 1))
        ttk.Spinbox(f, from_=1, to=20, textvariable=self.repeat_count_var, width=6).grid(
            row=0, column=1, sticky="w", padx=(4, 12)
        )

        ttk.Label(f, text="Intervall (s):").grid(row=0, column=2, sticky="w")
        self.repeat_interval_var = tk.IntVar(value=self.config_data.get("repeat_interval_s", 60))
        ttk.Spinbox(f, from_=1, to=3600, increment=1,
                    textvariable=self.repeat_interval_var, width=8).grid(
            row=0, column=3, sticky="w", padx=(4, 0)
        )

        ttk.Label(f, text="1 = einmaliger Send (kein Wiederholen).",
                  foreground="gray").grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _build_flash_section(self):
        f = ttk.LabelFrame(self.scroll_frame, text="Flashen (arduino-cli)", padding=8)
        f.pack(fill="x", pady=(0, 6))

        ttk.Label(f, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(f, textvariable=self.port_var, width=30, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky="w", padx=(4, 4))

        self.flash_button = ttk.Button(f, text="⚡ Flash", command=self._flash, state="disabled")
        self.flash_button.grid(row=0, column=3, padx=(8, 0))

        ttk.Label(f, text="Adafruit Feather ESP32-C6 · Board im Bootloader-Modus (BOOT halten, RESET tippen)",
                  foreground="gray").grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _build_buttons_section(self):
        self.buttons_frame = ttk.LabelFrame(self.scroll_frame, text="HTTP Aktionen", padding=8)
        self.buttons_frame.pack(fill="x", pady=(0, 6))

        self.btn_container = ttk.Frame(self.buttons_frame)
        self.btn_container.pack(fill="x")

        ttk.Button(self.buttons_frame, text="+ Button hinzufügen", command=self._add_button).pack(anchor="w", pady=(6, 0))

        for btn_data in self.config_data["buttons"]:
            self._add_button(btn_data)

    def _add_button(self, data=None):
        if data is None:
            data = {"name": f"Button {len(self.button_editors) + 1}", "url": "", "method": "GET"}
        idx = len(self.button_editors)
        editor = ButtonEditor(self.btn_container, idx, data, self._remove_button)
        editor.pack(fill="x", pady=(0, 4))
        self.button_editors.append(editor)

    def _remove_button(self, index):
        if len(self.button_editors) <= 1:
            messagebox.showwarning("Hinweis", "Mindestens ein Button muss vorhanden sein.")
            return
        editor = self.button_editors.pop(index)
        editor.destroy()
        for i, ed in enumerate(self.button_editors):
            ed.index = i
            ed.configure(text=f"Button {i + 1}")

    def _build_actions(self):
        f = ttk.Frame(self.scroll_frame, padding=(0, 6))
        f.pack(fill="x")

        ttk.Button(f, text="Vorschau", command=self._preview).pack(side="left", padx=(0, 6))
        ttk.Button(f, text="Exportieren (.ino)", command=self._export).pack(side="left", padx=(0, 6))
        ttk.Button(f, text="Config speichern", command=self._save_config).pack(side="left", padx=(0, 6))
        ttk.Button(f, text="Config laden", command=self._load_config).pack(side="left", padx=(0, 6))
        ttk.Button(f, text="In DB speichern…", command=self._save_to_db).pack(side="left", padx=(0, 6))

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
            url = btn["url"].strip()
            if not url:
                errors.append(f"Button {i}: URL darf nicht leer sein.")
            elif not re.match(r'https?://[^/:]+', url):
                errors.append(f"Button {i}: URL muss mit http:// oder https:// beginnen.")
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

        text = tk.Text(text_frame, wrap="none", font=("Consolas", 10))
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
        for btn_data in cfg.get("buttons", []):
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

    # ── Bootstrap & arduino-cli integration ───────────────────────────────

    def _post_status(self, msg: str):
        self.status_queue.put(("status", msg))

    def _post_ready(self):
        self.status_queue.put(("ready", None))

    def _drain_status_queue(self):
        try:
            while True:
                kind, payload = self.status_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "ready":
                    self.bootstrap_ready = True
                    self.flash_button.configure(state="normal")
                elif kind == "ports_changed":
                    ports, macs = payload
                    self._connected_macs = macs
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

    def _bootstrap(self):
        try:
            cli = arduino_cli_path()
            if cli is None:
                self._post_status("arduino-cli wird heruntergeladen…")
                download_arduino_cli(self._post_status)
            else:
                self._post_status(f"arduino-cli gefunden: {cli}")
            self._post_status("Prüfe ESP32-Core…")
            if not ensure_esp32_core(self._post_status):
                self._post_status("✗ ESP32-Core Installation fehlgeschlagen")
                return
            self._post_status("✓ arduino-cli bereit")
            self._post_ready()
        except Exception as e:
            self._post_status(f"✗ Bootstrap-Fehler: {e}")

    def _scan_devices(self, force: bool = False):
        """Detect ports + board MACs in ONE board-list call (runs in a thread).

        Posts to the UI queue only when something changed (or when forced), so
        the main thread never runs the arduino-cli subprocess itself."""
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

        code = generate_ino(cfg)
        name = cfg["device_name"].replace(" ", "_") or "wifi_button"
        sketch_root = Path(tempfile.mkdtemp(prefix="wifi-button-"))
        sketch_dir = sketch_root / name
        sketch_dir.mkdir()
        (sketch_dir / f"{name}.ino").write_text(code, encoding="utf-8")

        self._auto_save_config()
        self._open_flash_log(sketch_dir, port, cfg)

    def _open_flash_log(self, sketch_dir: Path, port: str, cfg: dict):
        win = tk.Toplevel(self)
        win.title(f"Flash → {port}")
        win.geometry("780x500")

        text_frame = ttk.Frame(win)
        text_frame.pack(fill="both", expand=True, padx=4, pady=4)
        text = tk.Text(text_frame, wrap="none", font=("Menlo", 10))
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
                ok = compile_and_upload(sketch_dir, port, log_cb)
                if ok:
                    self._register_flash(port, cfg, log_cb)
            except Exception as e:
                log_cb(f"✗ Exception: {e}")

        win.after(80, drain)
        threading.Thread(target=worker, daemon=True).start()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=4, pady=4)
        ttk.Button(btn_frame, text="Schließen", command=win.destroy).pack(side="right")

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
        self.db_search_var.trace_add("write", lambda *a: self._db_refresh())

        cols = ("customer", "location", "mac", "device_name", "ip", "buttons", "count")
        headings = {"customer": "Kunde", "location": "Standort", "mac": "MAC",
                    "device_name": "Gerät", "ip": "IP", "buttons": "Buttons",
                    "count": "#"}
        widths = {"customer": 90, "location": 80, "mac": 120, "device_name": 90,
                  "ip": 95, "buttons": 110, "count": 28}
        tree_frame = ttk.Frame(f)
        tree_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.db_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        sy = ttk.Scrollbar(tree_frame, orient="vertical", command=self.db_tree.yview)
        self.db_tree.configure(yscrollcommand=sy.set)
        for c in cols:
            self.db_tree.heading(c, text=headings[c])
            self.db_tree.column(c, width=widths[c], anchor="w")
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
    def _fmt_buttons(js: str) -> str:
        try:
            arr = json.loads(js) if js else []
        except Exception:
            arr = []
        if not arr:
            return ""
        first = arr[0].get("name", "") if isinstance(arr[0], dict) else ""
        return f"{len(arr)} × {first}" + (" …" if len(arr) > 1 else "")

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
        for cust, loc, mac, name, ip, _ssid, buttons, _last, cnt, _notes in rows:
            self.db_tree.insert("", "end",
                                 values=(cust, loc, mac, name, ip,
                                         self._fmt_buttons(buttons), cnt))

    def _refresh_db_dropdowns(self):
        """Populate the customer/location/MAC/IP comboboxes from the DB and
        pre-fill empty network fields from the most-recent device."""
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
            if vals and not self.ip_vars[key].get().strip():
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
        # No full config yet (e.g. only tagged in PTouch) → prefill Kunde/Standort.
        cust, loc = wb_get_meta(mac)
        if cust:
            self.customer_var.set(cust)
        if loc:
            self.location_var.set(loc)
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
        for label, content in [
            ("Config (JSON)", json.dumps(cfg, indent=2, ensure_ascii=False) if cfg else "—"),
            ("Sketch (.ino)", ino or "— (vor diesem Update geflasht)"),
        ]:
            frame = ttk.Frame(nb)
            nb.add(frame, text=label)
            txt = tk.Text(frame, wrap="none", font=("Menlo", 10))
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


if __name__ == "__main__":
    app = WifiButtonBuilder()
    app.mainloop()
