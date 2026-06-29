"""Philips Hue dawn-light adapter: sunrise mapping + throttled driver (no network in tests)."""

from sleepctl.adapters import hue


def test_sunrise_state_off_at_zero():
    s = hue.sunrise_state(0.0)
    assert s["on"] is False


def test_sunrise_state_ramps_brighter_and_cooler():
    low = hue.sunrise_state(0.2)
    high = hue.sunrise_state(1.0)
    assert low["on"] is True and high["on"] is True
    assert high["bri"] > low["bri"]           # brighter toward wake
    assert high["ct"] < low["ct"]             # cooler (lower mirek) toward wake
    assert 1 <= low["bri"] <= 254 and 153 <= high["ct"] <= 500


def test_driver_throttles_small_changes(monkeypatch):
    calls = []
    monkeypatch.setattr(hue, "apply", lambda *a, **k: (calls.append(a) or True))
    d = hue.HueDawnDriver("1.2.3.4", "tok", ["1", "2"], "lights", min_delta=0.05)
    d.set_level(0.10)            # first push
    d.set_level(0.12)            # +0.02 < delta -> skipped
    d.set_level(0.30)            # +0.20 -> push
    d.set_level(0.0)             # off always pushes
    assert len(calls) == 3


def test_apply_drives_multiple_lights(monkeypatch):
    seen = []
    monkeypatch.setattr(hue, "_req", lambda method, url, body=None, timeout=4.0: seen.append(url))
    assert hue.apply("1.2.3.4", "tok", ["3", "7"], 0.5, kind="lights") is True
    assert any("/lights/3/state" in u for u in seen)
    assert any("/lights/7/state" in u for u in seen)    # both bulbs driven


def test_set_power_drives_multiple_plugs(monkeypatch):
    seen = []
    monkeypatch.setattr(hue, "_req",
                        lambda method, url, body=None, timeout=4.0: seen.append((url, body)))
    assert hue.set_power("1.2.3.4", "tok", ["9", "11"], True) is True
    assert any("/lights/9/state" in u and b == {"on": True} for u, b in seen)
    assert any("/lights/11/state" in u and b == {"on": True} for u, b in seen)  # both plugs on


def test_set_therapy_only_fires_on_change(monkeypatch):
    calls = []
    monkeypatch.setattr(hue, "set_power", lambda ip, tok, ids, on: (calls.append(on) or True))
    d = hue.HueDawnDriver("1.2.3.4", "tok", [], "lights", therapy_ids=["9"])
    d.set_therapy(False)         # no-op: already implicitly off (None != False -> one push)
    d.set_therapy(False)         # unchanged -> skipped
    d.set_therapy(True)          # change -> push
    d.set_therapy(True)          # unchanged -> skipped
    d.set_therapy(False)         # change -> push
    assert calls == [False, True, False]


def test_set_level_noop_without_sunrise_targets(monkeypatch):
    calls = []
    monkeypatch.setattr(hue, "apply", lambda *a, **k: (calls.append(a) or True))
    d = hue.HueDawnDriver("1.2.3.4", "tok", [], "lights", therapy_ids=["9"])
    d.set_level(0.5)             # therapy-only config -> sunrise ramp must not call the bridge
    assert calls == []
