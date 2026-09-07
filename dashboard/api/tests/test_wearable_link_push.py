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
