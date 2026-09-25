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
_HUE = {"enabled": True, "bridge_ip": "10.0.0.5", "token": "paired", "target_ids": ["1", "2"],
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
    plug = {"enabled": True, "config": {"ip": "10.0.0.9", "local_key": "k"}}
    r = _run(monkeypatch, denied=False, hue=hue, plug=plug)
    assert r["status"] == "ok"
    assert "bright therapy lamp" in r["detail"]


def test_an_unconfigured_light_is_a_preference_not_a_fault(monkeypatch):
    """Grading every unconfigured channel as a warning leaves the page permanently amber for
    anyone who simply does not own a Hue, and an alert that is always on is one nobody reads."""
    r = _run(monkeypatch, denied=False, hue=_NO_HUE, plug=_NO_PLUG)
    assert r["status"] == "info"
    assert "Pod vibration" in r["detail"]


def test_an_unpaired_hue_and_an_empty_plug_are_not_wake_cues(monkeypatch):
    """A Hue bridge that was never paired has no token and cannot switch a lamp; a plug toggled
    "enabled" with no address or key cannot either. Counting them turned "nothing but a warming
    bed" into a mere warning."""
    hue = dict(_HUE, token=None)
    r = _run(monkeypatch, denied=True, hue=hue, plug={"enabled": True, "config": {}})
    assert r["status"] == "fail"
    assert "not paired" in r["detail"]
    assert "no address/key" in r["detail"]


def test_an_http_plug_with_an_on_url_counts(monkeypatch):
    plug = {"enabled": True, "backend": "http", "config": {"on_url": "http://10.0.0.9/on"}}
    r = _run(monkeypatch, denied=True, hue=_NO_HUE, plug=plug)
    assert r["status"] == "warn"
    assert "bright therapy lamp" in r["detail"].split("|")[0]


def test_a_config_read_error_is_info_not_a_failure(monkeypatch):
    """Not being able to READ the light config is not evidence that there is no light."""
    def boom(repo):
        raise RuntimeError("database is locked")
    monkeypatch.setattr("app.bridge.read_runtime_state",
                        lambda conn, secs=180: {"extra": {"alarm_write_denied": True}})
    monkeypatch.setattr("app.services._get_hue_config", boom)
    monkeypatch.setattr("app.services._get_plug_config", boom)
    r = diag._check_wake_cue(_Repo())
    assert r["status"] == "info"
    assert "unreadable" in r["detail"]
