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
