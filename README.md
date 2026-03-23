# WiFi Button Builder – ESP32-C6

A graphical tool for generating Arduino `.ino` sketches for battery-powered **WiFi buttons** based on the **Adafruit Feather ESP32-C6**. Configure buttons, HTTP endpoints, deep sleep behavior, and WiFi settings via a simple GUI — no manual coding required.

---

## Quick Start

```bash
# 1. Launch the GUI
python wifi_button_builder.py

# 2. Fill in WiFi credentials, static IP, and button URLs
# 3. Click "Exportieren (.ino)" to save the sketch
# 4. Open the .ino file in Arduino IDE and flash to the board
```

---

## Features

- GUI-based configuration (no code editing needed)
- Generates complete Arduino sketches for ESP32-C6
- Deep sleep with GPIO wakeup for minimal battery consumption
- Multiple buttons, each with individual HTTP endpoint (GET/POST)
- Static IP support (skips DHCP, saves ~1 s per wakeup)
- Configurable WiFi TX power and power save mode
- IO20 (STEMMA QT / NeoPixel power) always OFF + held during deep sleep
- Save/load configurations as JSON

---

## Requirements

### Software
- Python 3.10+
- [Arduino IDE 2.x](https://www.arduino.cc/en/software)
- ESP32 Arduino Core 3.x (via Arduino Board Manager)

> Tkinter is included with standard Python on most platforms. No additional Python packages required.

### Hardware

| Component | Details |
|---|---|
| Board | Adafruit Feather ESP32-C6 |
| Framework | Arduino (ESP32 Arduino Core) |
| Power | LiPo battery via JST connector |
| Wakeup | Tactile button connected between a GPIO pin and GND |

---

## Installation

### 1. Install the ESP32 board in Arduino IDE

1. Open Arduino IDE → **File → Preferences**
2. Add this URL to **Additional boards manager URLs**:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. Go to **Tools → Board → Boards Manager**, search for **esp32** by Espressif, and install version **3.x**
4. Select **Tools → Board → ESP32 Arduino → Adafruit Feather ESP32-C6**

### 2. Required Arduino libraries

No additional libraries are needed. The sketch uses only:
- `WiFi.h` (bundled with ESP32 Arduino Core)
- `esp_sleep.h` (bundled with ESP32 Arduino Core)
- `driver/gpio.h` (bundled with ESP32 IDF)

### 3. Clone and run the builder

```bash
git clone https://github.com/dockr69/wifi-button-esphome.git
cd wifi-button-esphome
python wifi_button_builder.py
```

---

## Hardware Wiring

Connect a momentary push button between your chosen GPIO pin and **GND**:

```
ESP32-C6                  Button
─────────                 ──────
GPIO pin ──────────────── leg 1
GND      ──────────────── leg 2
```

The generated sketch enables the internal pull-up resistor on the wakeup pin, so no external resistor is needed. The device wakes on a **LOW** signal (button press pulls the pin to GND) by default. This can be changed to HIGH in the GPIO section of the GUI.

**Recommended pins** (RTC-capable, marked ★ in the GUI — these survive deep sleep):

| Feather Label | GPIO |
|---|---|
| A5 / IO2 ★ | 2 (default) |
| A0 / IO1 ★ | 1 |
| A1 / IO4 ★ | 4 |
| A2 / IO6 ★ | 6 |
| A3 / IO5 ★ | 5 |
| A4 / IO3 ★ | 3 |
| IO0 ★ | 0 |
| IO7 ★ | 7 |

> Only RTC-capable GPIO pins (★) support GPIO wakeup from deep sleep. Non-RTC pins will not wake the device.

---

## Usage

### 1. Start the GUI

```bash
python wifi_button_builder.py
```

The last-used configuration is restored automatically on startup.

### 2. Configure the device

**Device section**
- Set a device name (used as the default filename when exporting).

**WiFi section**
- Enter your SSID and password.
- Enable **Statische IP** and fill in IP, Gateway, Subnet, DNS to skip DHCP (~1 s faster connect).
- Set **TX Power** (default 20 dBm for fastest association). Lower values save power but may slow connection.

**GPIO section**
- Select the **Wakeup Pin** from the Feather pin dropdown (★ = RTC-capable, required for deep sleep wakeup).
- Set **Wakeup Level**: `LOW` for a button wired to GND (most common), `HIGH` for a button wired to VCC.

**Timing section**

| Setting | Default | Description |
|---|---|---|
| WiFi Timeout | 10000 ms | How long to wait for WiFi before giving up |
| HTTP Timeout | 3000 ms | Timeout for the HTTP request |
| Maintenance | 5000 ms | Stay-awake window after WiFi failure (for OTA/serial) |

### 3. Add HTTP actions (buttons)

Each entry in the **HTTP Aktionen** section corresponds to one HTTP request sent per button press:

| Field | Description |
|---|---|
| Name | Label for this action (informational only) |
| URL | Full URL to call, e.g. `http://192.168.1.10/webhook` |
| Method | `GET` or `POST` |

Click **+ Button hinzufügen** to add more actions. All configured actions are sent on every wakeup.

### 4. Preview and export

| Button | Action |
|---|---|
| Vorschau | Opens a window with the generated `.ino` code |
| Exportieren (.ino) | Saves the sketch to a file |
| Config speichern | Saves current settings as a JSON file |
| Config laden | Loads settings from a JSON file |

### 5. Flash to the board

1. Open the exported `.ino` file in Arduino IDE
2. Select **Tools → Board → Adafruit Feather ESP32-C6**
3. Select the correct **Port** under **Tools → Port**
4. Click **Upload** (⌘U / Ctrl+U)

> **First upload:** if the board is not detected, hold the **BOOT** button while pressing **RESET** to enter download mode, then try again.

Open **Tools → Serial Monitor** at 115200 baud to watch the connection log.

---

## Configuration Options

| Setting | Default | Description |
|---|---|---|
| `device_name` | `wifi-button` | Used as default export filename |
| `wifi_ssid` / `wifi_password` | — | WiFi credentials |
| `use_static_ip` | `true` | Skip DHCP for faster connect |
| `static_ip` / `gateway` / `subnet` / `dns` | — | Static network config |
| `wakeup_pin` | 2 | GPIO pin that wakes the device (must be RTC-capable) |
| `wakeup_level` | `LOW` | Signal level that triggers wakeup |
| `wifi_timeout_ms` | 10000 | WiFi connect timeout in ms |
| `http_timeout_ms` | 3000 | HTTP request timeout in ms |
| `maintenance_ms` | 5000 | Stay-awake time after WiFi failure |
| `wifi_tx_power` | `20dBm` | TX power level (2–20 dBm) |
| `wifi_power_save` | `false` | Enable WiFi power save mode |

---

## Project Files

```
wifi_button_builder.py          # Main GUI application
wifi-button.yaml                # ESPHome config (alternative for development)
wifi-button.json                # Example saved configuration
wifi-button-arduino/
  wifi-button-arduino.ino       # Example generated Arduino sketch
```

---

## Connection & Wake-up Flow

Every button press triggers a full boot from deep sleep. The sketch minimizes the time from wake-up to HTTP request:

```
Wake (GPIO LOW)
  │
  ├─ IO20 (STEMMA QT / NeoPixel) → OFF + hold (before anything else)
  ├─ Check wakeup cause (GPIO / other)
  │
  ├─ WiFi config: static IP, TX power, no power save
  │
  ├─ RTC cache available? ──Yes──► WiFi.begin(SSID, PW, channel, BSSID)  ← skips scan
  │         │                                │
  │         No                         connected < 4s?
  │         │                                │
  │         └──► full scan              No → fallback to full scan, clear cache
  │
  ├─ Connected → save channel + BSSID to RTC memory
  │            → send HTTP request(s)
  │            → deep sleep
  │
  └─ Timeout  → clear RTC cache
               → maintenance window (5 s for OTA/serial)
               → deep sleep
```

---

## WiFi Speed Optimizations

| Technique | Time saved |
|---|---|
| Static IP (no DHCP) | ~1000 ms |
| RTC cache: BSSID + channel (skips AP scan) | ~300–500 ms |
| Max TX power (19.5 dBm) | faster association |
| `WiFi.persistent(false)` | no flash write on connect |
| `WIFI_FAST_SCAN` | stops after first matching AP |

### RTC Memory Cache

The generated sketch stores WiFi channel and BSSID in **RTC memory** (`RTC_DATA_ATTR`), which survives deep sleep. On the next wakeup the device connects directly to the known AP without scanning:

```cpp
RTC_DATA_ATTR int     savedChannel = 0;
RTC_DATA_ATTR uint8_t savedBSSID[6] = {0};
RTC_DATA_ATTR bool    hasCachedWiFi = false;
```

If the cached connect fails after 4 seconds (AP rebooted, channel changed), it automatically falls back to a full scan and resets the cache.

---

## HTTP Fire-and-Forget

The generated sketch uses a raw `WiFiClient` instead of `HTTPClient`. It sends the HTTP request and immediately disconnects without waiting for the full response — reducing uptime by ~100–200 ms:

```
client.connect(host, port)
  → send GET/POST request
  → flush + disconnect
  → enter deep sleep
```

---

## IO20 – STEMMA QT / NeoPixel Power

IO20 is always driven LOW and held via `gpio_hold_en()` before entering deep sleep. This cuts power to the onboard STEMMA QT connector and NeoPixel, preventing leakage current while the chip sleeps. No configuration needed — this is always applied.

---

## Maintenance Window

If WiFi fails, the device stays awake for `MAINTENANCE_MS` (default 5 s) before sleeping again. This allows serial debugging or OTA updates even when the network is unreachable. Reduce this value to save battery in deployed devices.

---

## ESPHome Alternative

`wifi-button.yaml` provides an ESPHome-based alternative — useful for OTA updates and remote logging without reflashing via USB.

> Add your WiFi credentials to `secrets.yaml` and reference them via `!secret` instead of hardcoding them in the YAML.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Board not detected in Arduino IDE | Hold BOOT + press RESET to enter download mode |
| WiFi connect timeout | Check SSID/password; try disabling static IP; lower TX power if signal is saturated |
| HTTP request fails | Verify the server IP and port; check firewall rules |
| Device does not wake on button press | Ensure the wakeup pin is RTC-capable (marked ★); check wiring |
| Deep sleep current too high | Verify IO20 is held LOW; check no other GPIO is floating |
| Sketch compiles but immediately crashes | Select the correct board in Arduino IDE (Adafruit Feather ESP32-C6) |
