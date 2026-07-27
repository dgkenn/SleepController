"""The MOTION channel on a Verity-only night (no iPhone).

The iPhone reports a unitless 0..1 movement index; the Verity's own accelerometer reduces to PIM
counts. Every movement threshold in the controller is calibrated against the 0..1 index, so the
counts are converted onto it (bridge.actigraphy_movement_index) rather than passed through raw.

Without this fallback ``read_fused_sensor`` returns ``movement=None`` whenever the phone is absent
or stale, and onset confirmation, arousal scoring, awakening detection and wake risk all silently
lose their motion input -- on exactly the configuration that has to work standalone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _no_leaked_rows():
    """The suite shares one DB. These tests write actigraphy rows, and any left behind would
    silently replace the activity series other tests assert on -- so clean up on the way out."""
    yield
    try:
        from app.db import get_repo
        repo = get_repo()
        _clear(repo.conn)
        repo.close()
    except Exception:
        pass


def _clear(conn):
    conn.execute("DELETE FROM actigraphy")
    conn.execute("DELETE FROM live_sensor")
    conn.execute("DELETE FROM live_cardiac")
    conn.commit()


def _write_actigraphy(conn, pim, age_s=0.0, source="verity"):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    conn.execute("INSERT INTO actigraphy (ts, pim, source) VALUES (?,?,?)", (ts, pim, source))
    conn.commit()


# ---------------------------------------------------------------- the unit conversion
def test_index_anchors_match_the_pim_semantics():
    from app import bridge
    # "essentially motionless" must land under the onset-stillness line (0.15)...
    still = bridge.actigraphy_movement_index(bridge.STILLNESS_PIM_FLOOR)
    assert still == 0.06 and still < 0.15
    # ...and "clearly moving" exactly on the wake-risk line (0.3), still under arousal (0.4).
    moving = bridge.actigraphy_movement_index(bridge.MOVEMENT_PIM_THRESHOLD)
    assert moving == 0.30
    assert 0.3 <= moving < 0.4


def test_index_is_monotonic_and_saturates():
    from app import bridge
    vals = [bridge.actigraphy_movement_index(p) for p in (0, 0.5, 1, 2, 5, 10, 17, 50, 5000)]
    assert vals == sorted(vals), vals
    assert vals[0] == 0.0
    assert vals[-1] == 1.0, "index must saturate at 1.0, never exceed the 0..1 contract"
    assert all(0.0 <= v <= 1.0 for v in vals)


def test_index_rejects_junk():
    from app import bridge
    for bad in (None, "x", float("nan"), float("inf"), -1.0):
        assert bridge.actigraphy_movement_index(bad) is None, bad


def test_index_ordering_against_every_controller_threshold():
    """The conversion is only meaningful if it preserves the ordering the thresholds assume."""
    from app import bridge
    from sleepctl.config import AppConfig
    t = AppConfig.default().tunables
    still_move = getattr(t, "onset_still_movement", 0.15)
    arousal = getattr(t, "arousal_movement", 0.4)
    unreliable = getattr(t, "onset_movement_unreliable", 0.45)
    wake_risk = getattr(t, "wake_risk_movement", 0.3)

    motionless = bridge.actigraphy_movement_index(bridge.STILLNESS_PIM_FLOOR)
    clearly_moving = bridge.actigraphy_movement_index(bridge.MOVEMENT_PIM_THRESHOLD)
    assert motionless <= still_move, "a motionless sleeper must read as stillness for onset"
    assert clearly_moving >= wake_risk, "'clearly moving' must register as restlessness"
    assert clearly_moving < arousal, "'clearly moving' alone must not score as an arousal"
    assert clearly_moving < unreliable, "'clearly moving' must not mark HR unreliable"
    # Gross movement (well past the moving anchor) SHOULD clear the arousal bar.
    assert bridge.actigraphy_movement_index(4 * bridge.MOVEMENT_PIM_THRESHOLD) > unreliable


# ---------------------------------------------------------------- the fusion behaviour
def test_verity_only_night_still_has_a_movement_channel(client):
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    try:
        _clear(repo.conn)
        _write_actigraphy(repo.conn, pim=6.0)
        fused = bridge.read_fused_sensor(repo.conn)
        assert fused is not None
        assert fused["movement"] is not None, "no phone => motion channel went dark"
        assert fused["movement_source"] == "verity"
        assert fused["movement_age_seconds"] is not None
    finally:
        repo.close()


def test_phone_index_keeps_priority_over_converted_counts(client):
    """The phone's native index is what the thresholds were tuned on, so it wins when fresh."""
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    try:
        _clear(repo.conn)
        bridge.write_sensor_sample(repo.conn, {"movement": 0.42, "source": "phone"})
        _write_actigraphy(repo.conn, pim=6.0)
        fused = bridge.read_fused_sensor(repo.conn)
        assert fused["movement"] == 0.42
        assert fused["movement_source"] == "phone"
    finally:
        repo.close()


def test_stale_counts_do_not_supply_movement(client):
    """Freshness gating applies to the fallback exactly as it does to the phone."""
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    try:
        _clear(repo.conn)
        _write_actigraphy(repo.conn, pim=6.0, age_s=600.0)
        bridge.write_cardiac_sample(repo.conn, {"hr": 55.0, "hrv": 40.0, "source": "verity"})
        fused = bridge.read_fused_sensor(repo.conn)
        assert fused is not None and fused["hr"] == 55.0  # cardiac still fresh
        assert fused["movement"] is None
        assert fused["movement_source"] is None
    finally:
        repo.close()


def test_ingest_to_fusion_end_to_end(auth_client):
    """The real path: forwarder POST with an acc block -> a usable movement index."""
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    _clear(repo.conn)
    repo.close()

    acc = {"pim": 6.0, "zcm": 90, "mad": 0.008, "std": 0.02, "pmax": 0.1, "n": 260, "fs": 52}
    r = auth_client.post("/hr/ingest", json={"hr": 56, "rr": [1030.0, 1042.0],
                                             "source": "verity", "acc": acc})
    assert r.status_code == 200 and r.json()["ok"] is True

    repo = get_repo()
    try:
        fused = bridge.read_fused_sensor(repo.conn)
        assert fused["movement"] == bridge.actigraphy_movement_index(6.0)
        assert fused["movement_source"] == "verity"
        assert fused["hr"] == 56.0 and fused["hr_source"] == "verity"
    finally:
        repo.close()


def test_daemon_wearable_source_carries_the_fallback_movement(client):
    """The adapter the daemon actually uses must surface it, not just the bridge."""
    from app import bridge
    from app.db import get_repo
    from sleepctl.adapters.bcg import BridgeWearableSource
    repo = get_repo()
    try:
        _clear(repo.conn)
        bridge.write_cardiac_sample(repo.conn, {"hr": 54.0, "hrv": 45.0, "source": "verity"})
        _write_actigraphy(repo.conn, pim=6.0)
        sample = BridgeWearableSource(repo).read_sample()
        assert sample is not None
        assert sample.movement == bridge.actigraphy_movement_index(6.0)
        assert sample.heart_rate == 54.0
    finally:
        repo.close()
