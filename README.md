# WiFi Button Builder – ESP32-C6

A graphical tool to **configure battery-powered WiFi buttons** based on the
**Adafruit Feather ESP32-C6**. Each button is a small device that, on a single
press, wakes from deep sleep, connects to WiFi, fires one HTTP request, and goes
back to sleep.

The buttons run a generic **base image** that is flashed **once**. The builder
does **not** compile or flash per device — it writes the configuration (WiFi,
target URL, timing) into the button over **USB serial**, and can read it back.
This makes it a fast field/bench tool for technicians: plug in, configure, done.

> Need to build the base image or a one-off custom sketch? The builder can still
> preview and export a complete Arduino `.ino` (see [Sketch export](#sketch-export-developers)).

---

## Quick Start (technician)

You only need the app and a **USB data cable** — no drivers, no Arduino IDE,
no Python (on Windows).

**Windows**
1. Download the latest `WiFi Button Builder-Windows.zip` from the
   [Releases page](https://github.com/dockr69/wifi-button-arduino/releases),
   unzip it, and run `WiFi Button Builder.exe`.
2. Plug the button into USB. It appears automatically as a COM port — see
   [USB & drivers](#usb--drivers).
3. On the **Konfiguration** page fill in WiFi, the HTTP action (host + request),
   then pick the port and click **⚡ Config senden**.

**macOS**
```bash
cd wifi-button-builder
python3 wifi_button_builder.py
```
Same steps — the button shows up as `/dev/cu.usbmodem…` with no driver.

> The header shows a live indicator: **● Verbunden** (teal) when a button is
> plugged in, **○ Kein Taster verbunden** otherwise.

---

## How it works

```
        ┌─────────────────┐   USB serial (SET/SAVE)   ┌──────────────────┐
        │  WiFi Button     │ ────────────────────────► │  Feather ESP32-C6 │
        │  Builder (GUI)   │ ◄──────────────────────── │  + base image     │
        └─────────────────┘   USB serial (CFG?)        └──────────────────┘
```

- The **base image** (`firmware/wifi-button-base/`) is a generic firmware flashed
  once per board. While on USB it boots into a **config mode** and listens on a
  simple serial protocol.
- The builder writes settings into the board's NVS via `SET …` / `SAVE`, and
  reads the current config back via `CFG?` (the **📥 Auslesen** button).
- On battery the board sleeps; a button press wakes it, it sends the configured
  HTTP request, and it sleeps again.

No per-device compiling or flashing is involved in normal use.

---

## The interface

The app has two pages, switched via the segmented control in the header:

### Konfiguration

| Card | Contents |
|---|---|
| **Gerät** | Device name, MAC (auto-filled from the connected board), Kunde, Standort |
| **WiFi** | SSID, password, IP mode (`Statisch` / `DHCP + IP-Cache`), IP/Gateway/Subnet/DNS, TX power, power save |
| **Timing & Wiederholung** | WiFi/HTTP timeout, cooldown, number of sends, repeat interval |
| **HTTP Aktion** | The single action this button triggers (see below) |
| **Taster (USB-Serial)** | Port selector, **⚡ Config senden**, **📥 Auslesen** |

Footer: **Vorschau** / **Export .ino** (sketch), **Config laden** / **Config
speichern** (JSON), **In DB speichern**.

### Datenbank

A shared device database (keyed by MAC) listing every configured button —
Kunde, Standort, MAC, device name, IP and **Request**. Click a row to load that
device into the editor. Search, CSV/JSON export and import are available. The DB
is shared with the label-printing tool (ptouch) via `labels.db`.

---

## HTTP action

Every button sends exactly **one** request, with a fixed URL shape:

```
http://<host>/cgi-bin/index.cgi?webif-pass=<pass>&spotrequest=<request>
```

In the editor you only set three fields — **Host (IP)**, **WebIF-Pass** and
**Spot-Request**. The path is fixed (`/cgi-bin/index.cgi`) and the method is
always **GET**. The full URL is what gets stored; for devices configured before
this layout, the three fields are parsed back out of the stored URL.

---

## USB & drivers

**No driver installation is required.** The Feather ESP32-C6 uses the chip's
**native USB-Serial-JTAG** (the base image is built with `CDCOnBoot=cdc`), so the
board enumerates as a standard **USB CDC serial** device:

| OS | Result |
|---|---|
| Windows 10 / 11 | In-box `usbser.sys` driver → appears automatically as `COM3`, `COM4`, … |
| macOS | Appears as `/dev/cu.usbmodem…` |
| Linux | Appears as `/dev/ttyACM0` (add your user to the `dialout` group) |

> **Not** a CP210x/CH340 board — do not install those drivers. The only common
> pitfall is a **charge-only USB cable**; use a data cable. The base MAC is read
> straight from the USB serial number, so the port and MAC show up even before
> any firmware logic runs.

---

## Installation (running from source / development)

Only needed on macOS/Linux, or for development. On Windows use the released
`.exe` instead.

### 1. Python + Tkinter + pyserial

- **macOS:** `brew install python` (Tkinter included)
- **Linux (Debian/Ubuntu):** `sudo apt install python3 python3-pip python3-tk`
- **Linux (Fedora):** `sudo dnf install python3 python3-pip python3-tkinter`

```bash
git clone https://github.com/dockr69/wifi-button-arduino.git
cd wifi-button-arduino/wifi-button-builder
python3 -m venv .venv && . .venv/bin/activate
pip install pyserial
python3 wifi_button_builder.py
```

### 2. Linux serial permissions

```bash
sudo usermod -aG dialout $USER   # then log out and back in
```

### 3. Building the Windows `.exe`

Pushing a `v*` git tag triggers the GitHub Actions workflow
(`.github/workflows/build-builder.yml`), which runs the tests and builds the
Windows one-folder ZIP, attaching it to a GitHub Release. (macOS runs from the
`.py`, so no `.app` is built.)

---

## Hardware wiring

A momentary push button between GPIO **IO2** (Feather label **A5 / IO2**) and
**GND** — this RTC-capable pin is fixed in the firmware:

```
ESP32-C6                  Button
─────────                 ──────
IO2  ──────────────────── leg 1
GND  ──────────────────── leg 2
```

The internal pull-up is enabled, so no external resistor is needed. The device
wakes on a **LOW** signal (press pulls IO2 to GND). Power is a LiPo battery on
the JST connector.

---

## Firmware behavior

These apply to the base image / generated sketch and don't need configuration.

### Wake-up flow

```
Wake (IO2 LOW)
  ├─ IO20 (STEMMA QT / NeoPixel) → OFF + hold
  ├─ WiFi config: static IP / cached DHCP, TX power, power save
  ├─ RTC cache present? ──Yes──► WiFi.begin(SSID, PW, channel, BSSID)  ← skips scan
  │        └─No──► full scan
  ├─ Connected → save channel + BSSID to RTC → send HTTP request(s) → deep sleep
  └─ Timeout   → clear RTC cache → short stay-awake → deep sleep
```

### WiFi speed optimizations

| Technique | Time saved |
|---|---|
| Static IP (no DHCP) | ~1000 ms |
| RTC cache: BSSID + channel (skips AP scan) | ~300–500 ms |
| Max TX power | faster association |
| `WiFi.persistent(false)` | no flash write on connect |
| `WIFI_FAST_SCAN` | stops after first matching AP |

Channel + BSSID are stored in `RTC_DATA_ATTR` memory (survives deep sleep). A
cached connect that fails within ~4 s falls back to a full scan and resets the
cache.

### Fire-and-forget HTTP

A raw `WiFiClient` sends the request and disconnects without waiting for the full
response, shaving ~100–200 ms off uptime.

### IO20 power

IO20 (STEMMA QT / NeoPixel power) is driven LOW and `gpio_hold_en()`-held before
deep sleep to stop leakage current. Always applied.

### Cooldown, repeat & maintenance

- **Cooldown:** presses within *N* seconds of the last send are ignored
  (`cooldown_s`, 0 = off); survives deep sleep via RTC timekeeping.
- **Repeat:** the batch is sent `repeat_count` times with `repeat_interval_s`
  between sends (1 = single shot); a press during the sequence cancels it.
- **Maintenance:** hold the button ~5 s to keep the board awake for reflashing.

---

## Serial config protocol (base image)

While on USB the base image accepts line commands at **115200 baud**:

| Command | Meaning |
|---|---|
| `VER?` | `VER wbtn <fw>` — identify the base image |
| `MAC?` | report the base MAC |
| `CFG?` | dump the current config (ends with `END`) — used by **Auslesen** |
| `SET <key> <value>` | stage a value into NVS |
| `SAVE` | commit staged values → `OK saved` |
| `CLEAR` | wipe stored config |
| `RUN` | leave config mode |

---

## Configuration options

| Setting | Default | Description |
|---|---|---|
| `device_name` | `wifi-button` | Default export filename |
| `customer` / `location` | — | Site metadata (drives DB sorting) |
| `wifi_ssid` / `wifi_password` | — | WiFi credentials (the password is never read back by `CFG?`) |
| `ip_mode` | `static` | `static` (no DHCP) or `dhcp_cache` (DHCP once, cache lease in RTC) |
| `static_ip` / `gateway` / `subnet` / `dns` | — | Static network config |
| `wifi_timeout_s` | 10 | WiFi connect timeout |
| `http_timeout_s` | 3 | HTTP request timeout |
| `cooldown_s` | 30 | Ignore presses within N s of last send (0 = off) |
| `wifi_tx_power` | `20dBm` | TX power level |
| `wifi_power_save` | `false` | WiFi power save mode |
| `repeat_count` | 1 | Number of sends per press (1 = single) |
| `repeat_interval_s` | 60 | Delay between repeated sends |
| `buttons` | — | The action (host / webif-pass / spotrequest, stored as a URL) |

---

## Sketch export (developers)

The builder can still generate a standalone Arduino sketch from the current
config: **Vorschau** previews it, **Export .ino** saves it. To compile/flash it
you need Arduino IDE 2.x with the **ESP32 Arduino Core 3.x** (board: *Adafruit
Feather ESP32-C6*, **USB CDC On Boot = Enabled**). This is only needed to build
the base image or experiment — normal configuration uses serial config instead.

---

## Project structure

```
wifi-button-builder/
  wifi_button_builder.py        # the GUI app
  tests/                        # pytest (DB I/O, URL parsing, port detection)
firmware/wifi-button-base/      # generic base image (.ino + prebuilt .bin)
wifi-button-arduino/            # example generated sketch
.github/workflows/              # CI: tests + Windows .exe on v* tags
```

---

## Troubleshooting

| Problem | Platform | Solution |
|---|---|---|
| Button not detected / no COM port | All | Use a **USB data cable** (not charge-only); re-plug. No driver is needed — it's native USB CDC. |
| No port in the dropdown | Linux | `sudo usermod -aG dialout $USER`, then log out/in |
| `✗ Keine Antwort vom Base-Image (VER?)` | All | Base image not flashed, or board not in config mode — re-plug USB / tap RESET |
| `No module named tkinter` | Linux | `sudo apt install python3-tk` (Debian/Ubuntu) / `sudo dnf install python3-tkinter` (Fedora) |
| WiFi connect timeout | All | Check SSID/password; verify the static IP fits the network |
| HTTP request fails | All | Verify host/IP reachable; check firewall |
| Device does not wake on press | All | Check button wiring between **IO2** and **GND** |
| Deep sleep current too high | All | Ensure IO20 is held LOW; no other GPIO floating |
| `python` not found | Windows | Use the released `.exe`, or `py` instead of `python` |
