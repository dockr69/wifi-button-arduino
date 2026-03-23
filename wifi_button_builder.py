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
    "use_static_ip": True,
    "static_ip": "192.168.2.123",
    "gateway": "192.168.2.1",
    "subnet": "255.255.255.0",
    "dns": "8.8.8.8",
    "wakeup_pin": 2,
    "wakeup_level": "LOW",
    "peripheral_power_pin": -1,
    "peripheral_power_active": "HIGH",
    "wifi_timeout_ms": 5000,
    "http_timeout_ms": 3000,
    "maintenance_ms": 5000,
    "wifi_tx_power": "20dBm",
    "wifi_power_save": False,
    "buttons": [
        {
            "name": "Button 1",
            "url": "http://192.168.2.175/cgi-bin/index.cgi?webif-pass=St@25rten&spotrequest=test1.mp3",
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


# ── Code Generator ────────────────────────────────────────────────────────────


def generate_ino(cfg: dict) -> str:
    """Generate Arduino .ino sketch from config dict (ESP32-C6 only)."""
    lines = []

    def L(text=""):
        lines.append(text)

    L('#include <WiFi.h>')
    L('#include <HTTPClient.h>')
    L('#include <esp_sleep.h>')
    L('#include <driver/gpio.h>')
    L()

    L('// ---- Konfiguration ----')
    L(f'const char* WIFI_SSID     = "{cfg["wifi_ssid"]}";')
    L(f'const char* WIFI_PASSWORD = "{cfg["wifi_password"]}";')
    L()

    if cfg["use_static_ip"]:
        for name, key in [("STATIC_IP", "static_ip"), ("GATEWAY", "gateway"),
                          ("SUBNET", "subnet"), ("DNS", "dns")]:
            octets = cfg[key].split(".")
            L(f'const IPAddress {name}({", ".join(octets)});')
        L()

    for i, btn in enumerate(cfg["buttons"]):
        L(f'const char* HTTP_URL_{i} = "{btn["url"]}";')
    L()

    L(f'const gpio_num_t WAKEUP_PIN = GPIO_NUM_{cfg["wakeup_pin"]};')
    if cfg["peripheral_power_pin"] >= 0:
        L(f'const gpio_num_t PERIPHERAL_POWER = GPIO_NUM_{cfg["peripheral_power_pin"]};')
    L()

    L(f'const unsigned long WIFI_TIMEOUT_MS = {cfg["wifi_timeout_ms"]};')
    L(f'const unsigned long HTTP_TIMEOUT_MS = {cfg["http_timeout_ms"]};')
    L(f'const unsigned long MAINTENANCE_MS  = {cfg["maintenance_ms"]};')
    L()

    L('void sendHttpRequest(const char* url, const char* method);')
    L('void enterDeepSleep();')
    L()

    # ── setup()
    L('// ---- Setup (runs once on every wake) ----')
    L('void setup() {')

    if cfg["peripheral_power_pin"] >= 0:
        active = 1 if cfg["peripheral_power_active"] == "HIGH" else 0
        inactive = 0 if active == 1 else 1
        L('  // Peripheral power off + hold through deep sleep')
        L('  gpio_set_direction(PERIPHERAL_POWER, GPIO_MODE_OUTPUT);')
        L(f'  gpio_set_level(PERIPHERAL_POWER, {inactive});')
        L('  gpio_hold_en(PERIPHERAL_POWER);')
        L()

    L('  Serial.begin(115200);')
    L()
    L('  // Log wakeup cause')
    L('  esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();')
    L('  if (cause == ESP_SLEEP_WAKEUP_GPIO) {')
    L('    Serial.println("GPIO wakeup - button pressed");')
    L('  } else {')
    L('    Serial.printf("Other wakeup cause: %d\\n", cause);')
    L('  }')
    L()

    if cfg["use_static_ip"]:
        L('  // Static IP (skips DHCP — saves ~1s)')
        L('  WiFi.config(STATIC_IP, GATEWAY, SUBNET, DNS);')

    tx = TX_POWER_MAP.get(cfg["wifi_tx_power"], "WIFI_POWER_19_5dBm")
    L(f'  WiFi.setTxPower({tx});')
    L(f'  WiFi.setSleep({"true" if cfg["wifi_power_save"] else "false"});')
    L('  WiFi.mode(WIFI_STA);')
    L('  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);')
    L()
    L('  Serial.println("Connecting WiFi...");')
    L()
    L('  unsigned long start = millis();')
    L('  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {')
    L('    delay(10);')
    L('  }')
    L()
    L('  if (WiFi.status() == WL_CONNECTED) {')
    L('    Serial.printf("WiFi OK (%lu ms)\\n", millis() - start);')

    for i, btn in enumerate(cfg["buttons"]):
        method = btn.get("method", "GET")
        L(f'    sendHttpRequest(HTTP_URL_{i}, "{method}");')

    L('    delay(200);')
    L('    enterDeepSleep();')
    L('  } else {')
    L('    Serial.println("WiFi failed, maintenance window");')
    L('    delay(MAINTENANCE_MS);')
    L('    enterDeepSleep();')
    L('  }')
    L('}')
    L()

    L('void loop() {')
    L('  // Never reached — device sleeps after setup()')
    L('}')
    L()

    # ── HTTP Request
    L('// ---- HTTP Request ----')
    L('void sendHttpRequest(const char* url, const char* method) {')
    L('  HTTPClient http;')
    L('  http.setConnectTimeout(HTTP_TIMEOUT_MS);')
    L('  http.setTimeout(HTTP_TIMEOUT_MS);')
    L('  http.setUserAgent("wifi-button/1.0");')
    L('  http.begin(url);')
    L()
    L('  int code;')
    L('  if (strcmp(method, "POST") == 0) {')
    L('    code = http.POST("");')
    L('  } else {')
    L('    code = http.GET();')
    L('  }')
    L()
    L('  if (code > 0) {')
    L('    Serial.printf("HTTP %s -> %d\\n", method, code);')
    L('  } else {')
    L('    Serial.printf("HTTP failed: %s\\n", http.errorToString(code).c_str());')
    L('  }')
    L('  http.end();')
    L('}')
    L()

    # ── Deep Sleep (C6 specific)
    L('// ---- Deep Sleep (ESP32-C6) ----')
    L('void enterDeepSleep() {')
    L('  Serial.println("Sleeping...");')
    L('  Serial.flush();')
    L()
    L('  WiFi.disconnect(true);')
    L('  WiFi.mode(WIFI_OFF);')
    L()

    wakeup_level = "ESP_GPIO_WAKEUP_GPIO_LOW" if cfg["wakeup_level"] == "LOW" else "ESP_GPIO_WAKEUP_GPIO_HIGH"
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
        self.static_ip_var = tk.BooleanVar(value=self.config_data["use_static_ip"])
        ttk.Checkbutton(f, text="Statische IP", variable=self.static_ip_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))

        row += 1
        labels = [("IP:", "static_ip"), ("Gateway:", "gateway"), ("Subnet:", "subnet"), ("DNS:", "dns")]
        self.ip_vars = {}
        for i, (label, key) in enumerate(labels):
            c = i * 2
            ttk.Label(f, text=label).grid(row=row, column=c, sticky="w")
            var = tk.StringVar(value=self.config_data[key])
            self.ip_vars[key] = var
            ttk.Entry(f, textvariable=var, width=16).grid(row=row, column=c + 1, sticky="w", padx=(4, 4))

        row += 1
        ttk.Label(f, text="TX Power:").grid(row=row, column=0, sticky="w", pady=(4, 0))
        self.tx_power_var = tk.StringVar(value=self.config_data["wifi_tx_power"])
        ttk.Combobox(f, textvariable=self.tx_power_var, values=list(TX_POWER_MAP.keys()), width=10, state="readonly").grid(
            row=row, column=1, sticky="w", padx=(4, 0), pady=(4, 0)
        )

        self.power_save_var = tk.BooleanVar(value=self.config_data["wifi_power_save"])
        ttk.Checkbutton(f, text="Power Save", variable=self.power_save_var).grid(row=row, column=2, columnspan=2, sticky="w", pady=(4, 0))

    def _build_gpio_section(self):
        f = ttk.LabelFrame(self.scroll_frame, text="GPIO", padding=8)
        f.pack(fill="x", pady=(0, 6))

        ttk.Label(f, text="Wakeup Pin (GPIO0-7 = RTC):").grid(row=0, column=0, sticky="w")
        self.wakeup_pin_var = tk.IntVar(value=self.config_data["wakeup_pin"])
        ttk.Spinbox(f, from_=0, to=30, textvariable=self.wakeup_pin_var, width=6).grid(row=0, column=1, sticky="w", padx=(4, 0))

        ttk.Label(f, text="  Wakeup Level:").grid(row=0, column=2, sticky="w")
        self.wakeup_level_var = tk.StringVar(value=self.config_data["wakeup_level"])
        ttk.Combobox(f, textvariable=self.wakeup_level_var, values=["LOW", "HIGH"], width=6, state="readonly").grid(
            row=0, column=3, sticky="w", padx=(4, 0)
        )

        ttk.Label(f, text="Peripheral Power Pin (-1 = aus):").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.periph_pin_var = tk.IntVar(value=self.config_data["peripheral_power_pin"])
        ttk.Spinbox(f, from_=-1, to=30, textvariable=self.periph_pin_var, width=6).grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))

        ttk.Label(f, text="  Active:").grid(row=1, column=2, sticky="w", pady=(4, 0))
        self.periph_active_var = tk.StringVar(value=self.config_data["peripheral_power_active"])
        ttk.Combobox(f, textvariable=self.periph_active_var, values=["HIGH", "LOW"], width=6, state="readonly").grid(
            row=1, column=3, sticky="w", padx=(4, 0), pady=(4, 0)
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
        return {
            "device_name": self.device_name_var.get(),
            "wifi_ssid": self.ssid_var.get(),
            "wifi_password": self.pw_var.get(),
            "use_static_ip": self.static_ip_var.get(),
            "static_ip": self.ip_vars["static_ip"].get(),
            "gateway": self.ip_vars["gateway"].get(),
            "subnet": self.ip_vars["subnet"].get(),
            "dns": self.ip_vars["dns"].get(),
            "wakeup_pin": self.wakeup_pin_var.get(),
            "wakeup_level": self.wakeup_level_var.get(),
            "peripheral_power_pin": self.periph_pin_var.get(),
            "peripheral_power_active": self.periph_active_var.get(),
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
        self.static_ip_var.set(cfg.get("use_static_ip", True))
        for key in ["static_ip", "gateway", "subnet", "dns"]:
            if key in self.ip_vars:
                self.ip_vars[key].set(cfg.get(key, ""))
        self.wakeup_pin_var.set(cfg.get("wakeup_pin", 2))
        self.wakeup_level_var.set(cfg.get("wakeup_level", "LOW"))
        self.periph_pin_var.set(cfg.get("peripheral_power_pin", -1))
        self.periph_active_var.set(cfg.get("peripheral_power_active", "HIGH"))
        self.tx_power_var.set(cfg.get("wifi_tx_power", "20dBm"))
        self.power_save_var.set(cfg.get("wifi_power_save", False))
        for key in ["wifi_timeout_ms", "http_timeout_ms", "maintenance_ms"]:
            if key in self.timing_vars:
                self.timing_vars[key].set(cfg.get(key, 5000))

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
