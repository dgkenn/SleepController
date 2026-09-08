"""Accelerometer breathing rate stands in for the beat-interval one; a marker gesture records
what the stager said at that declared-awake instant."""
import json
from datetime import datetime, timezone

import pytest

from app import bridge


@pytest.fixture()
def conn(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db
    r = Repository(str(tmp_path / "a.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r.conn
    r.close()


def test_a_confident_fresh_acc_breathing_rate_is_used(conn):
    bridge.append_actigraphy(conn, {"pim": 0.4, "zcm": 0, "mad": 0.0, "std": 0.0, "pmax": 0.0,
                                    "n": 104, "fs": 52, "resp_brpm": 13.6, "resp_conc": 0.6})
    assert bridge.read_acc_respiration_sample(conn) == 13.6


def test_a_flat_spectrum_or_a_nonsense_rate_yields_nothing(conn):
    bridge.append_actigraphy(conn, {"pim": 0.4, "n": 104, "fs": 52, "resp_brpm": 13.6, "resp_conc": 0.1})
    assert bridge.read_acc_respiration_sample(conn) is None
    bridge.append_actigraphy(conn, {"pim": 0.4, "n": 104, "fs": 52, "resp_brpm": 55.0, "resp_conc": 0.9})
    assert bridge.read_acc_respiration_sample(conn) is None


def test_the_fused_sample_carries_the_acc_breathing_rate_when_the_cardiac_one_is_absent(conn):
    bridge.write_cardiac_sample(conn, {"hr": 62.0, "hrv": None, "source": "verity", "respiratory_rate": None})
    bridge.append_actigraphy(conn, {"pim": 0.4, "n": 104, "fs": 52, "resp_brpm": 12.9, "resp_conc": 0.7})
    s = bridge.read_fused_sensor(conn)
    assert s is not None and s["respiratory_rate"] == 12.9


def test_a_marker_gesture_records_the_stage_the_stager_held_at_that_instant(conn):
    bridge.write_runtime_state(conn, {"stage": "rem", "updated": datetime.now(timezone.utc).isoformat()})
    bridge.append_actigraphy(conn, {"pim": 9.0, "n": 104, "fs": 52, "marker": True, "marker_hz": 3.1})
    row = conn.execute("SELECT data FROM events WHERE code='marker_vs_stage' ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert json.loads(row[0])["stage_at_marker"] == "rem"
