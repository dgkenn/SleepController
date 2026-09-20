"""The band connects or drops, and the person wearing it gets told.

On 2026-09-05 the user put the band on, said "about to go to bed", and slept. The forwarder had
been failing to connect for two hours and failed all night; the night recorded nothing.
Everything that knew lived on a box the user does not look at before bed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import services


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "link.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(services.push_sender, "deliver_custom",
                        lambda **kw: sent.append(kw) or {"sent": 1})
    monkeypatch.setattr(services, "list_push_subscriptions", lambda repo: [{"endpoint": "x"}])
    return sent


def _link(repo, state, streams=()):
    return services.ingest_hr(repo, {"source": "verity", "link": state, "streams": list(streams)})


def test_a_link_only_post_is_accepted_without_hr(repo, pushes):
    r = _link(repo, "connected", ["ACC@52Hz", "PPI"])
    assert r["ok"] is True and r["ingested"] == 0 and r["link"] == "connected"


def test_a_full_connection_confirms_all_three_streams(repo, pushes):
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    assert len(pushes) == 1
    assert pushes[0]["title"] == "Armband connected"
    assert "You're good" in pushes[0]["body"]


def test_an_hr_only_connection_says_wake_detection_is_blind(repo, pushes):
    """'HR only' is a different night from 'ACC + PPI', and the push has to say which."""
    _link(repo, "connected", ["HR/RR (generic 0x180D)"])
    assert "no accelerometer" in pushes[0]["body"]


def test_a_partial_pmd_connection_names_the_missing_stream(repo, pushes):
    _link(repo, "connected", ["PPI"])
    assert "WITHOUT movement" in pushes[0]["body"]


def test_connected_pushes_are_rate_limited(repo, pushes):
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    assert len(pushes) == 1


def test_the_link_state_is_recorded(repo, pushes):
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    st = services._kv_get_json(repo, services._WEARABLE_LINK_KEY)
    assert st["state"] == "connected" and "PPI" in st["streams"]


def _at_night(monkeypatch):
    monkeypatch.setattr(services, "_in_night_window", lambda now: True)
    monkeypatch.setattr(services, "_prebed_window", lambda now: False)


def _in_daytime(monkeypatch):
    monkeypatch.setattr(services, "_in_night_window", lambda now: False)
    monkeypatch.setattr(services, "_prebed_window", lambda now: False)


def test_losing_a_live_band_at_night_pages(repo, pushes, monkeypatch):
    _at_night(monkeypatch)
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    _link(repo, "lost")
    assert len(pushes) == 2
    assert pushes[1]["title"] == "Armband dropped"
    assert "nothing is recording" in pushes[1]["body"]


def test_losing_a_band_in_the_daytime_does_not_page(repo, pushes, monkeypatch):
    """Taking it off at lunch is not an outage."""
    _in_daytime(monkeypatch)
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    _link(repo, "lost")
    assert len(pushes) == 1


def test_a_loss_long_after_the_last_connection_does_not_page(repo, pushes, monkeypatch):
    """A stale 'connected' from hours ago means the band was not live; nothing was lost now."""
    _at_night(monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    services._kv_set_json(repo, services._WEARABLE_LINK_KEY,
                          {"state": "connected", "streams": ["PPI"], "ts": old})
    _link(repo, "lost")
    assert pushes == []


def test_a_loss_without_any_prior_connection_does_not_page(repo, pushes, monkeypatch):
    _at_night(monkeypatch)
    _link(repo, "lost")
    assert pushes == []


def test_ingest_never_raises_when_push_fails(repo, monkeypatch):
    monkeypatch.setattr(services.push_sender, "deliver_custom",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(services, "list_push_subscriptions", lambda repo: [{"endpoint": "x"}])
    assert _link(repo, "connected", ["PPI"])["ok"] is True


def test_a_reasserted_connected_link_is_a_refresh_not_a_new_event(repo, pushes):
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    n = repo.conn.execute("SELECT COUNT(*) FROM events WHERE code='wearable_link_connected'").fetchone()[0]
    assert n == 1
    assert len(pushes) == 1
    # a CHANGE of streams is a new link event
    _link(repo, "connected", ["HR/RR (generic 0x180D)"])
    n = repo.conn.execute("SELECT COUNT(*) FROM events WHERE code='wearable_link_connected'").fetchone()[0]
    assert n == 2


# ---------------------------------------------------------------- two receivers, one band
def _acc(repo, source):
    return services.ingest_hr(repo, {"source": source, "hr": 62.0, "rr": [980.0, 1000.0, 990.0],
                                     "acc": {"pim": 3.0, "zcm": 1.0, "mad": 0.1, "std": 0.2,
                                             "pmax": 0.5, "n": 104, "fs": 52}})


def test_each_receiver_is_recorded_and_the_pmd_holder_is_named(repo, pushes):
    _link(repo, "connected", ["ACC@52Hz", "PPI"])                       # verity (Windows)
    services.ingest_hr(repo, {"source": "verity-pi", "link": "connected",
                              "streams": ["HR/RR (generic 0x180D)"]})
    _acc(repo, "verity")
    rx = services.wearable_receivers(repo)
    assert {r["source"] for r in rx} == {"verity", "verity-pi"}
    st = services.wearable_stream_ages(repo)
    assert st["pmd_holder"] == "verity"
    assert st["acc"]["source"] == "verity" and st["acc"]["age_s"] < 5


def test_a_receiver_that_only_serves_heart_rate_is_not_the_pmd_holder(repo, pushes):
    services.ingest_hr(repo, {"source": "verity-pi", "link": "connected",
                              "streams": ["HR/RR (generic 0x180D)"]})
    st = services.wearable_stream_ages(repo)
    assert st["pmd_holder"] is None
    assert st["receivers"][0]["source"] == "verity-pi"


def test_beat_intervals_are_read_from_one_receiver_at_a_time(repo, pushes):
    """Both receivers post beats for the same heart; counting both halves every HRV timescale."""
    from app import bridge
    services.ingest_hr(repo, {"source": "verity-pi", "hr": 60.0, "rr": [1000.0] * 10})
    services.ingest_hr(repo, {"source": "verity", "hr": 60.0, "rr": [1000.0] * 10})
    rr = bridge.recent_rr_intervals(repo.conn, minutes=5.0)
    assert len(rr) == 10


def test_the_streams_endpoint_answers_with_the_ingest_token(auth_client):
    r = auth_client.get("/hr/streams")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"hr", "ppi", "acc", "pmd_holder", "receivers"}


# ---------------------------------------------------------------- charger / off arm
def test_a_charging_report_is_recorded_read_by_the_daemon_and_never_paged(repo, pushes):
    from app import bridge
    r = services.ingest_hr(repo, {"source": "verity", "link": "charging"})
    assert r["ok"] is True and r["link"] == "charging"
    off = bridge.read_wearable_off_arm(repo.conn)
    assert off and off["state"] == "charging" and off["age_s"] < 5
    assert pushes == []
    pl = services.wearable_pipeline(repo)
    assert pl["verdict"] == "off_arm" and "charger" in pl["headline"]


def test_a_stale_off_arm_report_is_ignored(repo, pushes):
    from app import bridge
    services._kv_set_json(repo, services._WEARABLE_LINK_KEY,
                          {"state": "charging", "streams": [], "source": "verity",
                           "ts": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()})
    assert bridge.read_wearable_off_arm(repo.conn) is None


def test_reconnecting_clears_the_off_arm_state(repo, pushes):
    from app import bridge
    services.ingest_hr(repo, {"source": "verity", "link": "off_arm"})
    assert bridge.read_wearable_off_arm(repo.conn)["state"] == "off_arm"
    _link(repo, "connected", ["ACC@52Hz", "PPI"])
    assert bridge.read_wearable_off_arm(repo.conn) is None


def test_an_accelerometer_only_batch_is_stored_not_rejected(repo, pushes):
    """PPI warms up ~25 s after connect; the ACC frames from those seconds were thrown away."""
    from app import bridge
    r = services.ingest_hr(repo, {"source": "verity",
                                  "acc": {"pim": 2.0, "zcm": 1.0, "mad": 0.1, "std": 0.2,
                                          "pmax": 0.4, "n": 104, "fs": 52}})
    assert r["ok"] is True and r.get("acc_only") is True
    assert bridge.recent_actigraphy(repo.conn, minutes=5.0)
    r = services.ingest_hr(repo, {"source": "verity"})
    assert r["ok"] is False


# ------------------------------------------------- every stream proved all the way downstream
def _decision(repo, payload):
    import json as _j
    from datetime import datetime as _dt
    repo.conn.execute(
        "INSERT INTO decisions (ts, night_date, state, log_payload) VALUES (?,?,?,?)",
        (_dt.now().isoformat(), "2026-09-19", "maintenance", _j.dumps(payload)))
    repo.conn.commit()


def _streaming(repo):
    """All three streams live, so the consumption checks are actually evaluated."""
    services.ingest_hr(repo, {"source": "verity", "hr": 61.0, "rr": [980.0, 1000.0, 990.0] * 4,
                              "acc": {"pim": 3.0, "zcm": 1.0, "mad": 0.1, "std": 0.2,
                                      "pmax": 0.5, "n": 104, "fs": 52}})


def _check(pl, cid):
    return next((c for c in pl["used"]["checks"] if c["id"] == cid), None)


def test_the_beat_series_reaching_the_estimator_is_proved_not_assumed(repo, pushes):
    """HRV is computed at ingest, so it can look healthy while frame.rr_history is empty and
    the autonomic REM/deep rescorer silently never runs."""
    _streaming(repo)
    _decision(repo, {"wearable_inputs": {"hr_history_n": 200, "activity_history_n": 90,
                                         "activity_units": "counts", "rr_history_n": 0}})
    c = _check(services.wearable_pipeline(repo), "autonomic_rr")
    assert c and c["ok"] is False and "rescorer cannot run" in c["detail"]

    _decision(repo, {"wearable_inputs": {"hr_history_n": 200, "activity_history_n": 90,
                                         "activity_units": "counts", "rr_history_n": 320}})
    c = _check(services.wearable_pipeline(repo), "autonomic_rr")
    assert c and c["ok"] is True and "320" in c["detail"]


def test_breathing_reaching_the_controller_is_proved(repo, pushes):
    _streaming(repo)
    _decision(repo, {"wearable_inputs": {"hr_history_n": 200, "activity_history_n": 90,
                                         "activity_units": "counts", "rr_history_n": 320}})
    c = _check(services.wearable_pipeline(repo), "respiration")
    assert c and c["ok"] is False and "onset signals" in c["detail"]

    _decision(repo, {"wearable_inputs": {"hr_history_n": 200, "activity_history_n": 90,
                                         "activity_units": "counts", "rr_history_n": 320},
                     "respiratory_rate_conf": 0.85, "respiratory_rate_source": "rsa+acc"})
    c = _check(services.wearable_pipeline(repo), "respiration")
    assert c and c["ok"] is True and "rsa+acc" in c["detail"]
