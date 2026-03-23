# WiFi Button Builder – ESP32-C6

A graphical tool for generating Arduino `.ino` sketches for battery-powered **WiFi buttons** based on the **Adafruit Feather ESP32-C6**. Configure buttons, HTTP endpoints, deep sleep behavior, and WiFi settings via a simple GUI — no manual coding required.

## Features

- GUI-based configuration (no code editing needed)
- Generates complete Arduino sketches for ESP32-C6
- Deep sleep with GPIO wakeup for minimal battery consumption
- Multiple buttons, each with individual HTTP endpoint (GET/POST)
- Static IP support (skips DHCP, saves ~1s per wakeup)
- Configurable WiFi TX power and power save mode
- Optional peripheral power pin (hold during deep sleep)
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
3. Set wakeup pin, timeouts, and TX power
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
| `wifi_timeout_ms` | How long to wait for WiFi (default 5000ms) |
| `http_timeout_ms` | HTTP request timeout (default 3000ms) |
| `maintenance_ms` | Time window for OTA/serial after wakeup |
| `wifi_tx_power` | TX power level (2–20 dBm) |
| `peripheral_power_pin` | Optional pin to cut peripheral power during sleep |

## Project Files

```
wifi_button_builder.py          # Main GUI application
wifi-button.yaml                # ESPHome config (debug/development)
wifi-button.json                # Example saved configuration
wifi-button-arduino/            # Example generated Arduino sketch
wifi-button.ino/                # Additional sketch variant
```

## ESPHome Alternative

`wifi-button.yaml` provides an ESPHome-based alternative for development and debugging — useful for OTA updates and logging without reflashing via USB.

> **Note:** Add your WiFi credentials to `secrets.yaml` and reference them via `!secret` instead of hardcoding them in the YAML.
