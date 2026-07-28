#!/usr/bin/env python3
"""Base-Image bauen und als flashbare .bin-Artefakte exportieren.

Der Provisioner (ptouch/mars-provisioner) kompiliert NICHT — er flasht die hier
erzeugten Binaries per esptool. Wer die .ino ändert, muss dieses Skript laufen
lassen und die Artefakte committen, sonst landet die Änderung nie auf einem Board.

    python3 firmware/export-base-image.py

Der FQBN ist nicht verhandelbar: ohne CDCOnBoot=cdc landet `Serial` auf UART0,
und der USB-Config-Modus ist tot.
"""
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

FQBN = "esp32:esp32:adafruit_feather_esp32c6:CDCOnBoot=cdc"
CHIP = "esp32c6"
# Muss zu WIFI_OFFSETS in ptouch_gui.py passen.
OFFSETS = {"bootloader.bin": "0x0", "partitions.bin": "0x8000",
           "boot_app0.bin": "0xe000", "firmware.bin": "0x10000"}

SKETCH_DIR = Path(__file__).resolve().parent / "wifi-button-base"
SKETCH = SKETCH_DIR / "wifi-button-base.ino"
BUILD_DIR = SKETCH_DIR / ".build"


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"✗ {' '.join(cmd[:3])} …\n{proc.stdout}{proc.stderr}")
    return proc.stdout


def fw_version() -> int:
    """FW_VERSION aus der .ino ziehen — die Quelle der Wahrheit."""
    for line in SKETCH.read_text().splitlines():
        if line.startswith("#define FW_VERSION"):
            return int(line.split()[2])
    sys.exit("✗ FW_VERSION nicht in der .ino gefunden")


def boot_app0() -> Path:
    """boot_app0.bin liegt im installierten ESP32-Core, nicht im Build-Output."""
    root = Path(run(["arduino-cli", "config", "get", "directories.data"]).strip())
    hits = sorted(root.glob("packages/esp32/hardware/esp32/*/tools/partitions/boot_app0.bin"))
    if not hits:
        sys.exit("✗ boot_app0.bin nicht gefunden — esp32-Core installiert?")
    return hits[-1]


def main() -> None:
    version = fw_version()
    print(f"Base-Image FW={version}  FQBN={FQBN}")

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    run(["arduino-cli", "compile", "--fqbn", FQBN,
         "--output-dir", str(BUILD_DIR), str(SKETCH_DIR)])

    produced = {
        "firmware.bin":   BUILD_DIR / "wifi-button-base.ino.bin",
        "bootloader.bin": BUILD_DIR / "wifi-button-base.ino.bootloader.bin",
        "partitions.bin": BUILD_DIR / "wifi-button-base.ino.partitions.bin",
        "boot_app0.bin":  boot_app0(),
    }
    for name, src in produced.items():
        if not src.is_file():
            sys.exit(f"✗ Artefakt fehlt: {src}")
        shutil.copy2(src, SKETCH_DIR / name)
        print(f"  → {name:16} {(SKETCH_DIR / name).stat().st_size:>8} B  @ {OFFSETS[name]}")

    (SKETCH_DIR / "VERSION").write_text(json.dumps({
        "chip": CHIP,
        "fqbn": FQBN,
        "fw": version,
        "note": "USB CDC On Boot MUSS aktiviert sein (sonst Serial=UART0, kein USB-Config)",
        "source_sha": hashlib.sha256(SKETCH.read_bytes()).hexdigest(),
        "built_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "offsets": OFFSETS,
    }, indent=2) + "\n")

    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    print("✓ VERSION aktualisiert. Artefakte committen, dann neu flashen.")


if __name__ == "__main__":
    main()
