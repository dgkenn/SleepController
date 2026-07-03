"""Thermal-response dataset: bridge writer/reader round-trip + direction/delta correctness.

The daemon logs one row per control tick WHILE the bed is actively heating/cooling; this covers
the persistence contract the daemon relies on (record_thermal_sample / recent_thermal_samples)."""

from __future__ import annotations

from app import bridge
from app.db import connect


def test_record_and_read_thermal_samples():
    conn = connect(":memory:")

    bridge.record_thermal_sample(conn, {
        "ts": "2026-07-03T22:00:00+00:00",
        "device_level": 10, "target_level": 40, "delta_level": 30,
        "direction": "heating", "bed_temp_f": 88.0, "room_temp_f": 72.0,
        "state": "INDUCTION", "session_mode": "night",
    })
    bridge.record_thermal_sample(conn, {
        "ts": "2026-07-03T22:01:00+00:00",
        "device_level": 20, "target_level": -30, "delta_level": -50,
        "direction": "cooling", "bed_temp_f": 92.0, "room_temp_f": 74.0,
        "state": "MAINTENANCE", "session_mode": "night",
    })

    rows = bridge.recent_thermal_samples(conn, limit=10)
    assert len(rows) == 2

    # most-recent first (ts DESC): the cooling sample leads
    cooling, heating = rows[0], rows[1]
    assert cooling["direction"] == "cooling" and cooling["delta_level"] == -50
    assert cooling["room_temp_f"] == 74.0
    assert heating["direction"] == "heating" and heating["delta_level"] == 30
    assert heating["target_level"] == 40 and heating["device_level"] == 10
    assert heating["session_mode"] == "night" and heating["state"] == "INDUCTION"

    conn.close()


def test_recent_thermal_samples_respects_limit():
    conn = connect(":memory:")
    for i in range(5):
        bridge.record_thermal_sample(conn, {
            "ts": f"2026-07-03T22:0{i}:00+00:00",
            "device_level": 0, "target_level": 20, "delta_level": 20,
            "direction": "heating", "bed_temp_f": 85.0, "room_temp_f": 70.0,
            "state": "INDUCTION", "session_mode": "night",
        })
    assert len(bridge.recent_thermal_samples(conn, limit=3)) == 3
    conn.close()
