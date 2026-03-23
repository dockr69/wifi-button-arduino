# WiFi Button Builder – ESP32-C6

A graphical tool for generating Arduino `.ino` sketches for battery-powered **WiFi buttons** based on the **Adafruit Feather ESP32-C6**. Configure buttons, HTTP endpoints, deep sleep behavior, and WiFi settings via a simple GUI — no manual coding required.

## Features

- GUI-based configuration (no code editing needed)
- Generates complete Arduino sketches for ESP32-C6
- Deep sleep with GPIO wakeup for minimal battery consumption
- Multiple buttons, each with individual HTTP endpoint (GET/POST)
- Static IP support (skips DHCP, saves ~1s per wakeup)
- Configurable WiFi TX power and power save mode
- IO20 (STEMMA QT / NeoPixel power) always OFF + held during deep sleep
- Save/load configurations as JSON

## Requirements

- Python 3.10+
- [Arduino IDE](https://www.arduino.cc/en/software) with ESP32 board support
- Adafruit Feather ESP32-C6 board

> Tkinter is included with standard Python on most platforms. No additional Python packages required.

## Usage

```bash
python wifi_button_builder.py
```

### Workflow

1. Configure WiFi credentials, static IP, and device name
2. Add buttons and assign HTTP URLs (e.g. a Home Assistant webhook)
3. Select wakeup pin from the Feather pin dropdown (RTC-capable pins marked with ★)
4. Click **Generate & Save** to export the `.ino` sketch
5. Open the sketch in Arduino IDE and flash to the ESP32-C6

## Hardware

| Component | Details |
|---|---|
| Board | Adafruit Feather ESP32-C6 |
| Framework | Arduino (ESP32 Arduino Core) |
| Power | Battery via deep sleep + GPIO wakeup |
| Wakeup | GPIO pin (configurable level) |

## Configuration Options

| Setting | Description |
|---|---|
| `wifi_ssid` / `wifi_password` | WiFi credentials |
| `use_static_ip` | Skip DHCP for faster connect |
| `wakeup_pin` | GPIO pin that wakes the device |
| `wakeup_pin` | Selected via Feather pin dropdown (★ = RTC-capable) |
| `wifi_timeout_ms` | How long to wait for WiFi (default 10000ms) |
| `http_timeout_ms` | HTTP request timeout (default 3000ms) |
| `maintenance_ms` | Time window for OTA/serial after wakeup (default 5000ms) |
| `wifi_tx_power` | TX power level (2–20 dBm) |

## Project Files

```
wifi_button_builder.py          # Main GUI application
wifi-button.yaml                # ESPHome config (debug/development)
wifi-button.json                # Example saved configuration
wifi-button-arduino/            # Example generated Arduino sketch
wifi-button.ino/                # Additional sketch variant
```

## Connection & Wake-up Flow

Every button press triggers a full boot from deep sleep. The sketch is optimized to minimize the time from wake-up to HTTP request:

```
Wake (GPIO LOW)
  │
  ├─ Peripheral power pin → OFF + hold (before anything else)
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
  │            → send HTTP request
  │            → deep sleep
  │
  └─ Timeout  → clear RTC cache
               → maintenance window (5s for OTA/serial)
               → deep sleep
```

### WiFi Speed Optimizations

| Technique | Saves |
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

### HTTP Fire-and-Forget

The generated sketch uses a raw `WiFiClient` instead of `HTTPClient`. It sends the HTTP request and disconnects immediately without waiting for the full response — reducing uptime by ~100–200 ms:

```
client.connect(host, port)
  → send GET request
  → flush + disconnect
  → enter deep sleep
```

### IO20 – STEMMA QT / NeoPixel Power

IO20 is always driven LOW and held via `gpio_hold_en()` before entering deep sleep. This cuts power to the onboard STEMMA QT connector and NeoPixel, preventing leakage current while the chip sleeps. No configuration needed.

### Maintenance Window

If WiFi fails, the device stays awake for `MAINTENANCE_MS` (default 5 s) before sleeping again. This allows serial debugging or OTA updates even when the network is unreachable.

## ESPHome Alternative

`wifi-button.yaml` provides an ESPHome-based alternative for development and debugging — useful for OTA updates and logging without reflashing via USB.

> **Note:** Add your WiFi credentials to `secrets.yaml` and reference them via `!secret` instead of hardcoding them in the YAML.
