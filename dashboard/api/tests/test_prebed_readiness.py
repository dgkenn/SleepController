"""The pre-bed readiness alert.

The existing failure detector is a NIGHTTIME pager: it fires once you're in bed. That is right for
"the reservoir just ran dry" and useless for "the daemon died this afternoon" — by then the night
is already lost. This one runs in the evening, while there is still time to fix something.

The properties that matter: it must not run the full battery on every tick, must not page for a
merely-degraded night, and must never let a failure of its own reach the control loop.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import services


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "prebed.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


def _at(hour):
    return datetime(2026, 7, 28, hour, 0, tzinfo=timezone.utc)


def _verdict(monkeypatch, verdict, blocking=()):
    import sleepctl.preflight as pf

    class _Item:
        def __init__(self, i):
            self.id, self.title = i, i.replace("_", " ")

    class _Rep:
        pass

    rep = _Rep()
    rep.verdict = verdict
    rep.blocking = [_Item(b) for b in blocking]
    monkeypatch.setattr(pf, "evaluate", lambda *a, **k: rep)


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    sent = []
    monkeypatch.setattr(services.push_sender, "deliver_custom",
                        lambda **kw: sent.append(kw) or {"sent": 1})
    return sent


# ------------------------------------------------------------------ the window
def test_runs_in_the_evening_window(repo, monkeypatch):
    _verdict(monkeypatch, "NO_GO", ["daemon_heartbeat"])
    assert services.check_pre_bed_readiness(repo, now=_at(19)) is not None
    assert services.check_pre_bed_readiness(repo, now=_at(20)) is None  # already ran today


@pytest.mark.parametrize("hour", [3, 9, 12, 15])
def test_does_not_run_outside_the_window(repo, monkeypatch, hour):
    _verdict(monkeypatch, "NO_GO", ["daemon_heartbeat"])
    assert services.check_pre_bed_readiness(repo, now=_at(hour)) is None


@pytest.mark.parametrize("hour", [21, 22])
def test_keeps_checking_into_the_night_window(repo, monkeypatch, hour):
    """A single check two hours out cannot see a failure that arrives afterwards -- which is the
    failure that happened. On 2026-09-05 the band was still streaming at 18:52, so the evening
    check said GO; it dropped shortly after, and the user went to bed at 21:40 with a dead feed,
    no page, and no data at all from the night."""
    _verdict(monkeypatch, "NO_GO", ["cardiac_sensor"])
    assert services.check_pre_bed_readiness(repo, now=_at(hour)) is not None


def test_a_go_earlier_does_not_silence_a_later_no_go(repo, monkeypatch):
    """What must not repeat is the PAGE, not the evaluation."""
    _verdict(monkeypatch, "GO", [])
    assert services.check_pre_bed_readiness(repo, now=_at(19)) is None
    _verdict(monkeypatch, "NO_GO", ["cardiac_sensor"])
    assert services.check_pre_bed_readiness(repo, now=_at(21)) is not None


def test_a_no_go_is_only_paged_once_a_night(repo, monkeypatch):
    _verdict(monkeypatch, "NO_GO", ["cardiac_sensor"])
    assert services.check_pre_bed_readiness(repo, now=_at(19)) is not None
    assert services.check_pre_bed_readiness(repo, now=_at(21)) is None


def test_runs_before_the_night_window_opens(repo, monkeypatch):
    """The whole point: fire while there is still time to act, not once you're in bed."""
    assert services._prebed_window(_at(19)) is True
    assert services._in_night_window(_at(19)) is False
    assert services._in_night_window(_at(21)) is True


def test_only_once_per_calendar_day(repo, monkeypatch):
    _verdict(monkeypatch, "NO_GO", ["daemon_heartbeat"])
    first = services.check_pre_bed_readiness(repo, now=_at(19))
    second = services.check_pre_bed_readiness(repo, now=_at(19))
    assert first is not None and second is None


def test_a_new_day_runs_again(repo, monkeypatch):
    _verdict(monkeypatch, "NO_GO", ["daemon_heartbeat"])
    assert services.check_pre_bed_readiness(repo, now=_at(19)) is not None
    tomorrow = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
    assert services.check_pre_bed_readiness(repo, now=tomorrow) is not None


# ------------------------------------------------------------------ what it pages for
def test_pages_on_no_go_with_the_reasons(repo, monkeypatch, _no_push):
    _verdict(monkeypatch, "NO_GO", ["daemon_heartbeat", "cardiac_sensor"])
    cond = services.check_pre_bed_readiness(repo, now=_at(19))
    assert cond["code"] == "prebed_not_ready"
    assert "daemon heartbeat" in cond["body"] and "cardiac sensor" in cond["body"]
    assert "still time to fix" in cond["body"]
    assert len(_no_push) == 1
    assert _no_push[0]["tag"] == "sleepctl-prebed"


@pytest.mark.parametrize("verdict", ["GO", "GO_DEGRADED"])
def test_does_not_page_when_the_night_can_go_ahead(repo, monkeypatch, _no_push, verdict):
    """A degraded night still happens — paging for it would train you to ignore the alert."""
    _verdict(monkeypatch, verdict)
    assert services.check_pre_bed_readiness(repo, now=_at(19)) is None
    assert _no_push == []


def test_a_go_verdict_is_still_recorded(repo, monkeypatch):
    """Not paging is not the same as not knowing — the evening verdict is logged either way."""
    _verdict(monkeypatch, "GO")
    services.check_pre_bed_readiness(repo, now=_at(19))
    rows = repo.recent_events(category="alert")
    assert any("prebed_readiness" in (r.get("code") or "") for r in rows)


# ------------------------------------------------------------------ robustness
def test_force_bypasses_the_window_and_the_daily_guard(repo, monkeypatch):
    _verdict(monkeypatch, "NO_GO", ["api"])
    assert services.check_pre_bed_readiness(repo, now=_at(3), force=True) is not None
    assert services.check_pre_bed_readiness(repo, now=_at(3), force=True) is not None


def test_a_broken_preflight_never_raises(repo, monkeypatch):
    """It's called from the control tick; a failure here must not reach the loop."""
    import sleepctl.preflight as pf

    def _boom(*a, **k):
        raise RuntimeError("preflight exploded")

    monkeypatch.setattr(pf, "evaluate", _boom)
    assert services.check_pre_bed_readiness(repo, now=_at(19)) is None


def test_a_broken_push_never_raises(repo, monkeypatch):
    _verdict(monkeypatch, "NO_GO", ["api"])
    monkeypatch.setattr(services.push_sender, "deliver_custom",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no network")))
    assert services.check_pre_bed_readiness(repo, now=_at(19), force=True) is not None
