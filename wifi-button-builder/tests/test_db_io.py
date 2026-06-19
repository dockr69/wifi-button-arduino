"""Tests for the GUI-free DB export/import (merge) functions."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wifi_button_builder as w


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point the module's DB_PATH at an empty temp SQLite file."""
    monkeypatch.setattr(w, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(w, "DB_GIT_DIR", None)
    return tmp_path


def _cfg(name, ip="192.168.2.50", customer="ACME", location="Halle"):
    return {
        "device_name": name, "customer": customer, "location": location,
        "wifi_ssid": "net", "wifi_password": "pw", "ip_mode": "static",
        "static_ip": ip, "gateway": "192.168.2.1", "subnet": "255.255.255.0",
        "dns": "8.8.8.8", "buttons": [{"name": "A", "url": "http://x/y"}],
    }


def test_full_row_returns_none_for_unknown_mac(fresh_db):
    assert w.wb_full_row("AA:BB:CC:DD:EE:FF") is None


def test_export_then_import_roundtrip(fresh_db, monkeypatch):
    w.wb_register("AA:BB:CC:00:00:01", _cfg("btn-1"), ino="// one")
    w.wb_register("AA:BB:CC:00:00:02", _cfg("btn-2", ip="192.168.2.51"), ino="// two")

    bundle = w.wb_export_db()
    assert bundle["format"] == "wifi-button-db"
    assert bundle["version"] == 1
    assert {d["mac"] for d in bundle["devices"]} == {
        "AA:BB:CC:00:00:01", "AA:BB:CC:00:00:02"}

    # Fresh empty DB, import everything.
    monkeypatch.setattr(w, "DB_PATH", fresh_db / "target.db")
    added, updated = w.wb_import_records(
        bundle["devices"], {"AA:BB:CC:00:00:01", "AA:BB:CC:00:00:02"})

    assert (added, updated) == (2, 0)
    row = w.wb_full_row("AA:BB:CC:00:00:01")
    assert row["device_name"] == "btn-1"
    assert row["ino"] == "// one"
    assert row["wifi_password"] == "pw"


def test_diff_classifies_new_changed_same(fresh_db):
    w.wb_register("AA:BB:CC:00:00:01", _cfg("btn-1"), ino="// one")
    base = w.wb_full_row("AA:BB:CC:00:00:01")

    # unknown MAC -> new
    assert w.wb_diff_record({"mac": "AA:BB:CC:00:00:09"}) == "new"
    # identical content -> same
    assert w.wb_diff_record(dict(base)) == "same"
    # changed content -> changed
    changed = dict(base, device_name="renamed")
    assert w.wb_diff_record(changed) == "changed"


def test_conflict_preserves_first_seen_and_higher_flash_count(fresh_db):
    # Local device flashed 5x.
    w.wb_register("AA:BB:CC:00:00:01", _cfg("local"), ino="// l")
    for _ in range(4):
        w.wb_register("AA:BB:CC:00:00:01", _cfg("local"), ino="// l")
    local = w.wb_full_row("AA:BB:CC:00:00:01")
    assert local["flash_count"] == 5

    incoming = dict(local, device_name="from-tech",
                    first_seen="2000-01-01T00:00:00", flash_count=1)
    added, updated = w.wb_import_records([incoming], {"AA:BB:CC:00:00:01"})

    assert (added, updated) == (0, 1)
    merged = w.wb_full_row("AA:BB:CC:00:00:01")
    assert merged["device_name"] == "from-tech"          # incoming wins
    assert merged["first_seen"] == local["first_seen"]   # history kept
    assert merged["flash_count"] == 5                     # higher kept


def test_only_selected_macs_are_written(fresh_db):
    recs = [
        {"mac": "AA:BB:CC:00:00:01", **_cfg("a"), "config_json": "{}"},
        {"mac": "AA:BB:CC:00:00:02", **_cfg("b"), "config_json": "{}"},
    ]
    added, updated = w.wb_import_records(recs, {"AA:BB:CC:00:00:01"})
    assert (added, updated) == (1, 0)
    assert w.wb_full_row("AA:BB:CC:00:00:01") is not None
    assert w.wb_full_row("AA:BB:CC:00:00:02") is None


def test_records_without_mac_are_skipped(fresh_db):
    added, updated = w.wb_import_records(
        [{"device_name": "no-mac"}], {"AA:BB:CC:00:00:01"})
    assert (added, updated) == (0, 0)


def test_update_from_read_preserves_metadata_and_flash_count(fresh_db):
    # Existing device, flashed twice.
    w.wb_register("AA:BB:CC:00:00:01", _cfg("Kasse 1"), ino="// k")
    w.wb_register("AA:BB:CC:00:00:01", _cfg("Kasse 1"), ino="// k")
    assert w.wb_full_row("AA:BB:CC:00:00:01")["flash_count"] == 2

    # A CFG? read only yields WLAN + buttons — no password, no metadata.
    read = {"wifi_ssid": "NewNet", "ip_mode": "static", "static_ip": "10.0.0.9",
            "gateway": "10.0.0.1", "subnet": "255.255.255.0", "dns": "8.8.8.8",
            "buttons": [{"name": "Button 1", "url": "http://y:8080/new",
                         "method": "POST"}]}
    assert w.wb_update_from_read("AA:BB:CC:00:00:01", read) == "updated"

    cfg = w.wb_get_config("AA:BB:CC:00:00:01")
    assert cfg["wifi_ssid"] == "NewNet"               # read wins
    assert cfg["static_ip"] == "10.0.0.9"
    assert cfg["buttons"] == read["buttons"]
    assert cfg["wifi_password"] == "pw"               # preserved
    assert cfg["customer"] == "ACME"                  # preserved
    assert cfg["device_name"] == "Kasse 1"            # preserved
    row = w.wb_full_row("AA:BB:CC:00:00:01")
    assert row["flash_count"] == 2                    # a read is not a flash
    assert row["ip"] == "10.0.0.9"


def test_update_from_read_creates_record_for_unknown_mac(fresh_db):
    read = {"wifi_ssid": "Net", "ip_mode": "dhcp_cache",
            "buttons": [{"name": "Button 1", "url": "http://h/p", "method": "GET"}]}
    assert w.wb_update_from_read("11:22:33:44:55:66", read) == "created"
    row = w.wb_full_row("11:22:33:44:55:66")
    assert row is not None
    assert row["flash_count"] == 0
    assert row["ip"] == "DHCP"


def test_action_url_parse_compose_roundtrip():
    u = "http://192.168.2.175/cgi-bin/index.cgi?webif-pass=1&spotrequest=test1.mp3"
    assert w.parse_action_url(u) == ("192.168.2.175", "1", "test1.mp3")
    assert w.compose_action_url("192.168.2.175", "1", "test1.mp3") == u
    # Bestandsgerät / leere URL -> leere Felder, kein Crash.
    assert w.parse_action_url("") == ("", "", "")
    # Host mit Schema/Slash wird normalisiert; Werte werden URL-kodiert.
    out = w.compose_action_url("http://10.0.0.5/", "p w", "a b.mp3")
    assert out.startswith("http://10.0.0.5/cgi-bin/index.cgi?")
    assert "webif-pass=p%20w" in out and "spotrequest=a%20b.mp3" in out
    # … und lässt sich wieder sauber zurücklesen.
    assert w.parse_action_url(out) == ("10.0.0.5", "p w", "a b.mp3")


class _FakePort:
    def __init__(self, device, serial_number):
        self.device = device
        self.serial_number = serial_number


def test_detected_ports_filters_to_mac_bearing_ports(monkeypatch):
    fake = [
        _FakePort("/dev/cu.usbmodem1", "F0:F5:BD:11:22:33"),   # a button
        _FakePort("/dev/cu.Bluetooth-Incoming", None),          # noise
        _FakePort("/dev/cu.debug-console", "no-mac-here"),      # noise
        _FakePort("/dev/cu.usbmodem2", "aa:bb:cc:dd:ee:ff"),    # a button
    ]
    import serial.tools.list_ports as lp
    monkeypatch.setattr(lp, "comports", lambda: fake)

    assert w.list_serial_ports() == ["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"]
    assert w.connected_macs() == ["F0:F5:BD:11:22:33", "AA:BB:CC:DD:EE:FF"]
    assert w.mac_from_port("/dev/cu.usbmodem2") == "AA:BB:CC:DD:EE:FF"
    assert w.mac_from_port("/dev/cu.Bluetooth-Incoming") is None


def test_parse_dump_roundtrips_firmware_output():
    dump = (
        "ssid=MyNet\nipmode=static\nip=192.168.1.50\ngw=192.168.1.1\n"
        "sn=255.255.255.0\ndns=8.8.8.8\nwifitmo=10000\nhttptmo=3000\n"
        "repcnt=2\nrepint=60000\ntxpow=20dBm\npsave=1\nbtncnt=2\n"
        "b0=192.168.1.10 8080 POST /trigger\nb1=example.com 80 GET /ping\nEND\n")
    cfg = w._parse_dump(dump)
    assert cfg["wifi_ssid"] == "MyNet"
    assert cfg["wifi_timeout_s"] == 10                # ms -> s
    assert cfg["repeat_interval_s"] == 60
    assert cfg["wifi_power_save"] is True
    assert cfg["buttons"] == [
        {"name": "Button 1", "url": "http://192.168.1.10:8080/trigger", "method": "POST"},
        {"name": "Button 2", "url": "http://example.com/ping", "method": "GET"},
    ]
    assert "wifi_password" not in cfg                 # firmware never dumps it
    assert w._parse_dump("garbage with no kv pairs") is None
