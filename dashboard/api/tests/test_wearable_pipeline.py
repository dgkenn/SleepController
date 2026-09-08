"""Connected, streaming, consumed -- three facts that used to be one word.

On 2026-09-07 a BLE session was open for sixteen hours while every batch was rejected and nothing
downstream saw a sample; the health page said "streaming". The pipeline walks the actual path
and names the first stage that is wrong.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import services


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "pipe.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r


def _iso(ago_s=0.0):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat()


def _cardiac(repo, ago_s, hr=62.0, hrv=41.0):
    repo.conn.execute(
        "INSERT OR REPLACE INTO live_cardiac (id, updated, hr, hrv, source, respiratory_rate) "
        "VALUES (1, ?, ?, ?, 'verity', NULL)", (_iso(ago_s), hr, hrv))
    repo.conn.commit()


def _rr(repo, ago_s, n=12):
    repo.conn.execute("INSERT INTO rr_intervals (ts, rr_ms, n, source) VALUES (?,?,?,?)",
                      (_iso(ago_s), json.dumps([980.0] * n), n, "verity"))
    repo.conn.commit()


def _acc(repo, ago_s):
    repo.conn.execute(
        "INSERT INTO actigraphy (ts, pim, zcm, mad, std, pmax, n, fs, source) "
        "VALUES (?, 2.1, 3, 0.01, 0.02, 0.9, 104, 52, 'verity')", (_iso(ago_s),))
    repo.conn.commit()


def _decision(repo, state="maintenance", ago_s=10.0, **inputs):
    ts = (datetime.now() - timedelta(seconds=ago_s)).isoformat()   # decisions.ts is naive local
    payload = {"stage_source": "model", "wearable_inputs": {
        "hr_history_n": inputs.get("hr_n", 0), "activity_history_n": inputs.get("acc_n", 0),
        "activity_units": inputs.get("units")}}
    repo.conn.execute(
        "INSERT INTO decisions (ts, night_date, state, action, target_level, log_payload) "
        "VALUES (?, date('now'), ?, 'hold', -54, ?)", (ts, state, json.dumps(payload)))
    repo.conn.commit()


def test_nothing_at_all_is_not_connected(repo):
    r = services.wearable_pipeline(repo)
    assert r["verdict"] == "not_connected"
    assert r["streams"] == []


def test_a_full_stream_is_full(repo):
    _cardiac(repo, 3); _rr(repo, 4); _acc(repo, 2)
    r = services.wearable_pipeline(repo)
    assert r["verdict"] == "streaming_full"
    assert r["streams"] == ["HR", "PPI", "ACC"]
    assert r["hr"]["bpm"] == 62.0 and r["ppi"]["intervals_5min"] == 12


def test_hr_only_is_partial_and_names_what_is_missing(repo):
    _cardiac(repo, 3)
    r = services.wearable_pipeline(repo)
    assert r["verdict"] == "streaming_partial"
    assert "PPI" in r["headline"] and "ACC" in r["headline"]


def test_a_linked_band_with_stale_data_is_silent_not_streaming(repo):
    """The 2026-09-07 case: link said connected, nothing had landed for hours."""
    services._kv_set_json(repo, services._WEARABLE_LINK_KEY,
                          {"state": "connected", "streams": ["HR/RR (generic 0x180D)"], "ts": _iso(600)})
    _cardiac(repo, 6 * 3600)
    r = services.wearable_pipeline(repo)
    assert r["verdict"] == "connected_silent"
    assert r["hr"]["ok"] is False


def test_landed_but_unused_is_its_own_verdict(repo):
    """ACC batches arriving, but the controller's last tick had no counts -- the actigraphy
    wake detector is off while the page would otherwise say 'streaming'."""
    _cardiac(repo, 3); _rr(repo, 4); _acc(repo, 2)
    _decision(repo, state="maintenance", hr_n=800, acc_n=0, units=None)
    r = services.wearable_pipeline(repo)
    assert r["verdict"] == "streaming_unused"
    ids = {c["id"]: c for c in r["used"]["checks"]}
    assert ids["wake_detector_acc"]["ok"] is False
    assert ids["stager_hr"]["ok"] is True


def test_usage_is_not_judged_while_idle(repo):
    _cardiac(repo, 3); _rr(repo, 4); _acc(repo, 2)
    _decision(repo, state="idle", hr_n=0, acc_n=0, units=None)
    r = services.wearable_pipeline(repo)
    assert r["verdict"] == "streaming_full"
    assert r["used"]["in_session"] is False
    assert r["used"]["checks"] == []


def test_a_fully_consumed_stream_passes_every_check(repo):
    _cardiac(repo, 3); _rr(repo, 4); _acc(repo, 2)
    _decision(repo, state="maintenance", hr_n=800, acc_n=400, units="counts")
    r = services.wearable_pipeline(repo)
    assert r["verdict"] == "streaming_full"
    assert all(c["ok"] for c in r["used"]["checks"])
    assert {c["id"] for c in r["used"]["checks"]} >= {"stager_hr", "wake_detector_acc", "hrv"}


def test_refusing_shape_from_the_forwarder_log_wins_when_nothing_lands(repo, tmp_path):
    (tmp_path / "verity.log").write_text("\n".join([
        "found 'Polar Sense 16961D33' at 24:AC:AC:16:96:1D",
        "connecting to 24:AC:AC:16:96:1D ...",
        "session error (TimeoutError: ); reconnecting in 25s (consecutive failures: 7)",
    ] * 3) + "\n")
    r = services.wearable_pipeline(repo, run_dir=str(tmp_path))
    assert r["verdict"] == "refusing"
    assert "Polar app" in r["remedy"]


def test_the_route_never_500s(auth_client):
    r = auth_client.get("/wearable/pipeline")
    assert r.status_code == 200
    body = r.json()
    assert "verdict" in body and "streams" in body and "used" in body
