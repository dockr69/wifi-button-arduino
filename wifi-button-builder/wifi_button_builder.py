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

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "device_name": "wifi-button",
    "wifi_ssid": "",
    "wifi_password": "",
    # ip_mode: "static" | "dhcp" | "dhcp_cache"
    #   static     — never use DHCP (fastest, requires fixed IP at router)
    #   dhcp       — DHCP every wake (slowest, ~1s extra)
    #   dhcp_cache — DHCP once, cache lease in RTC RAM, reuse on wake
    "ip_mode": "static",
    "static_ip": "192.168.2.123",
    "gateway": "192.168.2.1",
    "subnet": "255.255.255.0",
    "dns": "8.8.8.8",
    "wakeup_pin": 2,
    "wakeup_level": "LOW",
    "wifi_timeout_ms": 10000,
    "http_timeout_ms": 3000,
    "maintenance_ms": 5000,
    "wifi_tx_power": "20dBm",
    "wifi_power_save": False,
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

# Adafruit Feather ESP32-C6 pin names → GPIO number
# RTC pins (GPIO0-7) marked with ★
FEATHER_PINS = [
    ("A0 / IO1 ★", 1),
    ("A1 / IO4 ★", 4),
    ("A2 / IO6 ★", 6),
    ("A3 / IO5 ★", 5),
    ("A4 / IO3 ★", 3),
    ("A5 / IO2 ★", 2),
    ("IO0 ★", 0),
    ("IO7 ★", 7),
    ("IO8", 8),
    ("IO9 (BUTTON)", 9),
    ("IO12", 12),
    ("IO14", 14),
    ("IO15 (LED)", 15),
    ("IO16 (TX)", 16),
    ("IO17 (RX)", 17),
    ("IO18 (SCL)", 18),
    ("IO19 (SDA)", 19),
    ("IO20 (I2C_PWR)", 20),
    ("IO21 (SCK)", 21),
    ("IO22 (MOSI)", 22),
    ("IO23 (MISO)", 23),
]
FEATHER_PIN_LABELS = [p[0] for p in FEATHER_PINS]
FEATHER_PIN_GPIO = {p[0]: p[1] for p in FEATHER_PINS}
FEATHER_GPIO_LABEL = {p[1]: p[0] for p in FEATHER_PINS}


# ── Code Generator ────────────────────────────────────────────────────────────


def generate_ino(cfg: dict) -> str:
    """Generate Arduino .ino sketch from config dict (ESP32-C6 only)."""
    lines = []

    def L(text=""):
        lines.append(text)

    # Parse URLs into host/port/path for fire-and-forget
    import re
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

    L(f'const gpio_num_t WAKEUP_PIN = GPIO_NUM_{cfg["wakeup_pin"]};')
    L()

    L(f'const unsigned long WIFI_TIMEOUT_MS = {cfg["wifi_timeout_ms"]};')
    L(f'const unsigned long HTTP_TIMEOUT_MS = {cfg["http_timeout_ms"]};')
    L(f'const unsigned long MAINTENANCE_MS  = {cfg["maintenance_ms"]};')
    L()

    L('// WiFi cache survives deep sleep')
    L('RTC_DATA_ATTR int savedChannel = 0;')
    L('RTC_DATA_ATTR uint8_t savedBSSID[6] = {0};')
    L('RTC_DATA_ATTR bool hasCachedWiFi = false;')
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
    L('    Serial.println("GPIO wakeup - button pressed");')
    L('  } else {')
    L('    Serial.printf("Other wakeup cause: %d\\n", cause);')
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
    L('  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {')
    L('    // Cached connect failed? Retry with full scan')
    L('    if (hasCachedWiFi && millis() - start > 4000 && WiFi.status() != WL_CONNECTED) {')
    L('      Serial.println("Cache miss - fallback to full scan");')
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

    for i, pu in enumerate(parsed_urls):
        L(f'    sendHttpRequest(HTTP_HOST_{i}, HTTP_PORT_{i}, HTTP_PATH_{i}, "{pu["method"]}");')

    L('    enterDeepSleep();')
    L('  } else {')
    L('    Serial.printf("WiFi FAILED after %lu ms, status: %d\\n", millis() - start, WiFi.status());')
    L('    hasCachedWiFi = false;')
    L('    savedChannel = 0;')
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
    L('  Serial.flush();')
    L()
    L('  WiFi.disconnect(true);')
    L('  WiFi.mode(WIFI_OFF);')
    L()

    wakeup_level = "ESP_GPIO_WAKEUP_GPIO_LOW" if cfg["wakeup_level"] == "LOW" else "ESP_GPIO_WAKEUP_GPIO_HIGH"

    if cfg["wakeup_level"] == "LOW":
        L('  gpio_pullup_en(WAKEUP_PIN);')
        L('  gpio_pulldown_dis(WAKEUP_PIN);')
    else:
        L('  gpio_pulldown_en(WAKEUP_PIN);')
        L('  gpio_pullup_dis(WAKEUP_PIN);')

    L()
    L(f'  esp_deep_sleep_enable_gpio_wakeup(1ULL << WAKEUP_PIN, {wakeup_level});')
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
        self.geometry("780x750")
        self.minsize(700, 550)

        self.config_data = dict(DEFAULT_CONFIG)
        self.button_editors: list[ButtonEditor] = []

        self._build_ui()
        self._load_last_config()

    def _build_ui(self):
        style = ttk.Style()
        style.configure("TLabelframe.Label", font=("", 10, "bold"))

        main = ttk.Frame(self, padding=8)
        main.pack(fill="both", expand=True)

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
        self._build_gpio_section()
        self._build_timing_section()
        self._build_buttons_section()
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
            ("DHCP",             "dhcp"),
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

    def _build_gpio_section(self):
        f = ttk.LabelFrame(self.scroll_frame, text="GPIO (★ = RTC / Deep Sleep fähig)", padding=8)
        f.pack(fill="x", pady=(0, 6))

        ttk.Label(f, text="Wakeup Pin:").grid(row=0, column=0, sticky="w")
        default_label = FEATHER_GPIO_LABEL.get(self.config_data["wakeup_pin"], f"IO{self.config_data['wakeup_pin']}")
        self.wakeup_pin_var = tk.StringVar(value=default_label)
        ttk.Combobox(f, textvariable=self.wakeup_pin_var, values=FEATHER_PIN_LABELS, width=20, state="readonly").grid(
            row=0, column=1, sticky="w", padx=(4, 0)
        )

        ttk.Label(f, text="  Wakeup Level:").grid(row=0, column=2, sticky="w")
        self.wakeup_level_var = tk.StringVar(value=self.config_data["wakeup_level"])
        ttk.Combobox(f, textvariable=self.wakeup_level_var, values=["LOW", "HIGH"], width=6, state="readonly").grid(
            row=0, column=3, sticky="w", padx=(4, 0)
        )

        ttk.Label(f, text="IO20 (STEMMA QT / NeoPixel Power) ist immer aus.", foreground="gray").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0)
        )

    def _build_timing_section(self):
        f = ttk.LabelFrame(self.scroll_frame, text="Timing (ms)", padding=8)
        f.pack(fill="x", pady=(0, 6))

        self.timing_vars = {}
        items = [
            ("WiFi Timeout:", "wifi_timeout_ms"),
            ("HTTP Timeout:", "http_timeout_ms"),
            ("Maintenance:", "maintenance_ms"),
        ]
        for i, (label, key) in enumerate(items):
            ttk.Label(f, text=label).grid(row=0, column=i * 2, sticky="w")
            var = tk.IntVar(value=self.config_data[key])
            self.timing_vars[key] = var
            ttk.Spinbox(f, from_=500, to=30000, increment=500, textvariable=var, width=8).grid(
                row=0, column=i * 2 + 1, sticky="w", padx=(4, 12)
            )

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

    def _gather_config(self) -> dict:
        wakeup_label = self.wakeup_pin_var.get()
        wakeup_gpio = FEATHER_PIN_GPIO.get(wakeup_label, 2)

        return {
            "device_name": self.device_name_var.get(),
            "wifi_ssid": self.ssid_var.get(),
            "wifi_password": self.pw_var.get(),
            "ip_mode": self.ip_mode_var.get(),
            "static_ip": self.ip_vars["static_ip"].get(),
            "gateway": self.ip_vars["gateway"].get(),
            "subnet": self.ip_vars["subnet"].get(),
            "dns": self.ip_vars["dns"].get(),
            "wakeup_pin": wakeup_gpio,
            "wakeup_level": self.wakeup_level_var.get(),
            "wifi_timeout_ms": self.timing_vars["wifi_timeout_ms"].get(),
            "http_timeout_ms": self.timing_vars["http_timeout_ms"].get(),
            "maintenance_ms": self.timing_vars["maintenance_ms"].get(),
            "wifi_tx_power": self.tx_power_var.get(),
            "wifi_power_save": self.power_save_var.get(),
            "buttons": [ed.get_data() for ed in self.button_editors],
        }

    def _preview(self):
        cfg = self._gather_config()
        code = generate_ino(cfg)

        win = tk.Toplevel(self)
        win.title(f"Vorschau — {cfg['device_name']}.ino")
        win.geometry("750x600")

        text = tk.Text(win, wrap="none", font=("Consolas", 10))
        text.pack(fill="both", expand=True, padx=4, pady=4)

        sy = ttk.Scrollbar(text, orient="vertical", command=text.yview)
        sx = ttk.Scrollbar(text, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")

        text.insert("1.0", code)
        text.configure(state="disabled")

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=4, pady=4)
        ttk.Button(btn_frame, text="In Zwischenablage kopieren",
                   command=lambda: (self.clipboard_clear(), self.clipboard_append(code))).pack(side="left")

    def _export(self):
        cfg = self._gather_config()
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
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile=f"{cfg['device_name']}.json",
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
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
        # Migrate legacy bool → tri-state enum
        if "ip_mode" in cfg:
            mode = cfg["ip_mode"]
        else:
            mode = "static" if cfg.get("use_static_ip", True) else "dhcp"
        self.ip_mode_var.set(mode)
        for key in ["static_ip", "gateway", "subnet", "dns"]:
            if key in self.ip_vars:
                self.ip_vars[key].set(cfg.get(key, ""))
        self._on_ip_mode_change()
        wakeup_gpio = cfg.get("wakeup_pin", 2)
        self.wakeup_pin_var.set(FEATHER_GPIO_LABEL.get(wakeup_gpio, f"IO{wakeup_gpio}"))
        self.wakeup_level_var.set(cfg.get("wakeup_level", "LOW"))
        self.tx_power_var.set(cfg.get("wifi_tx_power", "20dBm"))
        self.power_save_var.set(cfg.get("wifi_power_save", False))
        timing_defaults = {"wifi_timeout_ms": 10000, "http_timeout_ms": 3000, "maintenance_ms": 5000}
        for key in timing_defaults:
            if key in self.timing_vars:
                self.timing_vars[key].set(cfg.get(key, timing_defaults[key]))

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


if __name__ == "__main__":
    app = WifiButtonBuilder()
    app.mainloop()
