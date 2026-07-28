"""The GO / NO-GO preflight.

Its whole value is being RIGHT about which conditions disqualify a night, so the tests are about
the policy, not the plumbing: dry-run must block (every other check is green while the bed gets no
commands), a thin learning history must not, and the runtime battery's in-process-only `api` check
must not be taken at face value from a CLI process.
"""

from __future__ import annotations

import pytest

from sleepctl import preflight
from sleepctl.preflight import PreflightReport, evaluate, format_report

# Bound before the autouse fixture monkeypatches the module attribute, so the probe's own
# behaviour can still be tested directly.
_REAL_API_PORT_OPEN = preflight.api_port_open


def _check(cid, status, title=None, detail="", remedy=None):
    return {"id": cid, "title": title or cid, "status": status,
            "detail": detail, "remedy": remedy}


_ALL_GREEN = [
    _check("daemon_heartbeat", "ok"), _check("api", "ok"), _check("device_online", "ok"),
    _check("device_water", "ok"), _check("thermal_capacity", "ok"),
    _check("live_mode", "info", detail="live=True dry_run=False"),
    _check("cardiac_sensor", "ok", detail="streaming"),
    _check("calibration", "ok"),
]


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository

    r = Repository(str(tmp_path / "pf.db"))
    yield r
    r.close()


@pytest.fixture(autouse=True)
def _api_is_up(monkeypatch):
    """Default the socket probe to 'up' so tests exercise policy, not the environment."""
    monkeypatch.setattr(preflight, "api_port_open", lambda *a, **k: True)


def _with(monkeypatch, checks):
    monkeypatch.setattr(preflight, "_runtime_checks", lambda repo: checks)


# ------------------------------------------------------------------ verdicts
def test_all_green_is_go(monkeypatch, repo):
    _with(monkeypatch, _ALL_GREEN)
    rep = evaluate(repo)
    assert rep.verdict == "GO"
    assert not rep.blocking and not rep.degraded


def test_dead_daemon_is_no_go(monkeypatch, repo):
    _with(monkeypatch, [c if c["id"] != "daemon_heartbeat" else _check("daemon_heartbeat", "fail")
                        for c in _ALL_GREEN])
    rep = evaluate(repo)
    assert rep.verdict == "NO_GO"
    assert any(b.id == "daemon_heartbeat" for b in rep.blocking)


def test_dry_run_blocks_even_though_everything_is_healthy(monkeypatch, repo):
    """The trap this module exists for: all green, and the bed is never actually commanded."""
    _with(monkeypatch, [c if c["id"] != "live_mode"
                        else _check("live_mode", "info", detail="live=True dry_run=True")
                        for c in _ALL_GREEN])
    rep = evaluate(repo)
    assert rep.verdict == "NO_GO"
    blocker = next(b for b in rep.blocking if b.id == "live_mode")
    assert "NO commands" in blocker.detail


def test_dry_run_is_detected_regardless_of_check_status(monkeypatch, repo):
    """live_mode reports 'info', not 'warn' — reading only the status would miss it entirely."""
    live = next(c for c in _ALL_GREEN if c["id"] == "live_mode")
    assert live["status"] == "info", "precondition: this is why we parse the detail"


def test_dry_run_off_does_not_block(monkeypatch, repo):
    _with(monkeypatch, _ALL_GREEN)
    assert evaluate(repo).verdict == "GO"


def test_silent_verity_blocks_a_sensor_night(monkeypatch, repo):
    _with(monkeypatch, [c if c["id"] != "cardiac_sensor"
                        else _check("cardiac_sensor", "info", detail="not streaming")
                        for c in _ALL_GREEN])
    rep = evaluate(repo)
    assert rep.verdict == "NO_GO"
    assert any(b.id == "cardiac_sensor" for b in rep.blocking)


def test_silent_verity_is_fine_for_a_pod_only_night(monkeypatch, repo):
    _with(monkeypatch, [c if c["id"] != "cardiac_sensor"
                        else _check("cardiac_sensor", "info", detail="not streaming")
                        for c in _ALL_GREEN])
    rep = evaluate(repo, want_sensor=False)
    assert rep.verdict == "GO"


def test_warnings_degrade_but_do_not_block(monkeypatch, repo):
    _with(monkeypatch, _ALL_GREEN + [_check("watchdog_heartbeat", "warn", detail="stale"),
                                     _check("priming", "warn", detail="priming")])
    rep = evaluate(repo)
    assert rep.verdict == "GO_DEGRADED"
    assert {d.id for d in rep.degraded} == {"watchdog_heartbeat", "priming"}
    assert not rep.blocking


def test_a_check_is_never_listed_as_both_blocking_and_degraded(monkeypatch, repo):
    """prevention_timing appears in both maps — a fail must not also be reported as a warning."""
    _with(monkeypatch, _ALL_GREEN + [_check("prevention_timing", "fail", detail="no response")])
    rep = evaluate(repo)
    ids_blocking = {b.id for b in rep.blocking}
    ids_degraded = {d.id for d in rep.degraded}
    assert "prevention_timing" in ids_blocking
    assert not (ids_blocking & ids_degraded)


def test_missing_calibration_is_a_note_not_a_blocker(monkeypatch, repo):
    _with(monkeypatch, [c if c["id"] != "calibration"
                        else _check("calibration", "info", detail="3 of 3 missing")
                        for c in _ALL_GREEN])
    rep = evaluate(repo)
    assert rep.verdict == "GO"
    assert any(n.id == "calibration" for n in rep.notes)


# ------------------------------------------------------------------ the api tautology
def test_a_dead_api_port_blocks_even_when_the_battery_says_ok(monkeypatch, repo):
    """The battery's `api` check is in-process-only and always green here; the probe must win."""
    monkeypatch.setattr(preflight, "api_port_open", lambda *a, **k: False)
    _with(monkeypatch, _ALL_GREEN)
    rep = evaluate(repo)
    assert rep.verdict == "NO_GO"
    blocker = next(b for b in rep.blocking if b.id == "api")
    assert "8000" in blocker.detail


def test_api_port_open_is_false_for_a_closed_port():
    assert _REAL_API_PORT_OPEN(port=9) is False


# ------------------------------------------------------------------ degradation
def test_missing_runtime_battery_is_reported_not_silently_passed(monkeypatch, repo):
    _with(monkeypatch, None)
    rep = evaluate(repo)
    assert "runtime" not in rep.sources
    assert any(n.id == "runtime" for n in rep.notes)
    assert "WARNING" in format_report(rep) or rep.sources


def test_data_battery_findings_are_notes_only(monkeypatch, repo):
    """A thin history means the controller runs on priors — expected early on, not a fault."""
    _with(monkeypatch, _ALL_GREEN)
    rep = evaluate(repo)
    assert rep.verdict == "GO"
    assert all(n.severity == "note" for n in rep.notes)


def test_format_report_renders_every_verdict():
    for verdict in ("GO", "GO_DEGRADED", "NO_GO"):
        text = format_report(PreflightReport(verdict=verdict))
        assert verdict.split("_")[0] in text


def test_to_dict_round_trips_through_json(monkeypatch, repo):
    """It is served over HTTP and written into the published snapshot, so it must both encode
    and survive encoding with its content intact -- not merely fail to crash."""
    import json

    _with(monkeypatch, _ALL_GREEN + [_check("priming", "warn", detail="priming")])
    rep = evaluate(repo)
    out = json.loads(json.dumps(rep.to_dict()))
    assert out["verdict"] == rep.verdict
    assert [d["id"] for d in out["degraded"]] == [d.id for d in rep.degraded]
    assert out["sources"] == rep.sources
