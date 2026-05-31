#!/usr/bin/env python3
"""
ESP32-C6 WiFi Button Builder
Generates Arduino IDE .ino sketches for battery-powered WiFi buttons.
Hardcoded for Adafruit Feather ESP32-C6.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import os
import re
import ipaddress
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import queue
import urllib.request
import zipfile
from pathlib import Path

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "device_name": "wifi-button",
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


def list_serial_ports() -> list[str]:
    """Return detected serial port addresses."""
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
        result = []
        for entry in ports:
            addr = entry.get("port", {}).get("address")
            if addr:
                result.append(addr)
        return result
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []


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
        self.geometry("920x980")
        self.minsize(820, 720)

        self.config_data = dict(DEFAULT_CONFIG)
        self.button_editors: list[ButtonEditor] = []
        self.bootstrap_ready = False
        self.status_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._load_last_config()
        self.after(100, self._drain_status_queue)
        threading.Thread(target=self._bootstrap, daemon=True).start()

    def _build_ui(self):
        style = ttk.Style()
        style.configure("TLabelframe.Label", font=("", 10, "bold"))

        # Statusbar at the very bottom (packed first with side=bottom so it sticks)
        self.status_var = tk.StringVar(value="Starte arduino-cli Bootstrap…")
        status = ttk.Label(self, textvariable=self.status_var,
                           relief="sunken", anchor="w", padding=(6, 2))
        status.pack(side="bottom", fill="x")

        main = ttk.Frame(self, padding=8)
        main.pack(side="top", fill="both", expand=True)

        canvas = tk.Canvas(main, highlightthickness=0)
        scrollbar = ttk.Scrollbar(main, orient="vertical", command=canvas.yview)
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

    def _build_device_section(self):
        f = ttk.LabelFrame(self.scroll_frame, text="Gerät (Adafruit Feather ESP32-C6)", padding=8)
        f.pack(fill="x", pady=(0, 6))

        ttk.Label(f, text="Device Name:").grid(row=0, column=0, sticky="w")
        self.device_name_var = tk.StringVar(value=self.config_data["device_name"])
        ttk.Entry(f, textvariable=self.device_name_var, width=30).grid(row=0, column=1, sticky="w", padx=(4, 0))

    def _build_wifi_section(self):
        f = ttk.LabelFrame(self.scroll_frame, text="WiFi", padding=8)
        f.pack(fill="x", pady=(0, 6))

        row = 0
        ttk.Label(f, text="SSID:").grid(row=row, column=0, sticky="w")
        self.ssid_var = tk.StringVar(value=self.config_data["wifi_ssid"])
        ttk.Entry(f, textvariable=self.ssid_var, width=28).grid(row=row, column=1, sticky="w", padx=(4, 0))

        ttk.Label(f, text="  Passwort:").grid(row=row, column=2, sticky="w")
        self.pw_var = tk.StringVar(value=self.config_data["wifi_password"])
        ttk.Entry(f, textvariable=self.pw_var, width=28, show="*").grid(row=row, column=3, sticky="w", padx=(4, 0))

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

        row += 1
        labels = [("IP:", "static_ip"), ("Gateway:", "gateway"), ("Subnet:", "subnet"), ("DNS:", "dns")]
        self.ip_vars = {}
        self.ip_entries = {}
        for i, (label, key) in enumerate(labels):
            c = i * 2
            ttk.Label(f, text=label).grid(row=row, column=c, sticky="w")
            var = tk.StringVar(value=self.config_data[key])
            self.ip_vars[key] = var
            ent = ttk.Entry(f, textvariable=var, width=16)
            ent.grid(row=row, column=c + 1, sticky="w", padx=(4, 4))
            self.ip_entries[key] = ent
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

        ttk.Button(f, text="🔄", width=3, command=self._refresh_ports).grid(row=0, column=2, padx=(0, 8))

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
                    self._refresh_ports()
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

    def _refresh_ports(self):
        ports = list_serial_ports()
        # Filter Bluetooth/debug noise on macOS
        filtered = [p for p in ports if "Bluetooth" not in p and "debug-console" not in p]
        self.port_combo["values"] = filtered or ports
        if (filtered or ports) and not self.port_var.get():
            self.port_var.set((filtered or ports)[0])

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
        self._open_flash_log(sketch_dir, port)

    def _open_flash_log(self, sketch_dir: Path, port: str):
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
                compile_and_upload(sketch_dir, port, log_cb)
            except Exception as e:
                log_cb(f"✗ Exception: {e}")

        win.after(80, drain)
        threading.Thread(target=worker, daemon=True).start()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=4, pady=4)
        ttk.Button(btn_frame, text="Schließen", command=win.destroy).pack(side="right")


if __name__ == "__main__":
    app = WifiButtonBuilder()
    app.mainloop()
