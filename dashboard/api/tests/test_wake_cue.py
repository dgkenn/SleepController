"""What will actually wake you tomorrow, as an inventory rather than per-channel status.

`wake_alarm` reports the Pod refused the alarm write and says waking now happens "via the
thermal ramp + dawn light only". Nothing checked whether there IS a dawn light. Each channel
behaves correctly on its own; the failure exists only in the total.
"""

import app.diagnostics as diag


class _Repo:
    conn = None


def _run(monkeypatch, denied, hue, plug):
    """Patch the ATTRIBUTES, not sys.modules.

    `diagnostics` reaches these with `from app import bridge`, which reads the attribute already
    bound on the `app` package once it has been imported -- so replacing `sys.modules["app.bridge"]`
    has no effect in a full-suite run where the real module is loaded, while passing in isolation.
    """
    monkeypatch.setattr("app.bridge.read_runtime_state",
                        lambda conn, secs=180: {"extra": {"alarm_write_denied": denied}})
    monkeypatch.setattr("app.services._get_hue_config", lambda repo: hue)
    monkeypatch.setattr("app.services._get_plug_config", lambda repo: plug)
    return diag._check_wake_cue(_Repo())


_NO_HUE = {"enabled": False, "bridge_ip": None, "target_ids": [], "therapy_ids": []}
_HUE = {"enabled": True, "bridge_ip": "10.0.0.5", "target_ids": ["1", "2"],
        "therapy_ids": ["9"]}
_NO_PLUG = {"enabled": False}


def test_a_warming_bed_alone_is_a_failure(monkeypatch):
    """Vibration subscription-gated and no light configured: the whole wake system is a bed
    that gets warm, and this user needs silence and works clinical shifts."""
    r = _run(monkeypatch, denied=True, hue=_NO_HUE, plug=_NO_PLUG)
    assert r["status"] == "fail"
    assert "warming bed" in r["detail"]


def test_vibration_gone_but_a_dawn_light_configured_is_only_a_warning(monkeypatch):
    r = _run(monkeypatch, denied=True, hue=_HUE, plug=_NO_PLUG)
    assert r["status"] == "warn"
    assert "dawn light" in r["detail"]
    assert "Pod vibration (subscription-gated)" in r["detail"]


def test_everything_available_is_ok(monkeypatch):
    r = _run(monkeypatch, denied=False, hue=_HUE, plug=_NO_PLUG)
    assert r["status"] == "ok"
    assert "Pod vibration" in r["detail"]


def test_a_wifi_therapy_plug_counts_without_hue_therapy_ids(monkeypatch):
    hue = dict(_HUE, therapy_ids=[])
    r = _run(monkeypatch, denied=False, hue=hue, plug={"enabled": True})
    assert r["status"] == "ok"
    assert "bright therapy lamp" in r["detail"]


def test_an_unconfigured_light_is_a_preference_not_a_fault(monkeypatch):
    """Grading every unconfigured channel as a warning leaves the page permanently amber for
    anyone who simply does not own a Hue, and an alert that is always on is one nobody reads."""
    r = _run(monkeypatch, denied=False, hue=_NO_HUE, plug=_NO_PLUG)
    assert r["status"] == "info"
    assert "Pod vibration" in r["detail"]
