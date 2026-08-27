"""Tests for the generic Wi-Fi smart-plug wake-therapy driver.

The behaviour worth pinning is the safety envelope: a high-output lamp must never be left
energised by a stuck caller, and every failure must fall toward OFF.
"""

from __future__ import annotations

import pytest

from sleepctl.adapters import smart_plug


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance_min(self, m: float) -> None:
        self.t += m * 60.0


def _driver(monkeypatch, *, max_on_min=45.0, fail_on=False, fail_off=False):
    clock = _Clock()
    calls: list = []

    def fake_switch(backend, cfg, on):
        calls.append(on)
        if on and fail_on:
            return False
        if (not on) and fail_off:
            return False
        return True

    monkeypatch.setattr(smart_plug, "switch", fake_switch)
    d = smart_plug.SmartPlugTherapyDriver("tuya", {"device_id": "x", "ip": "1.2.3.4",
                                                   "local_key": "k"},
                                          max_on_min=max_on_min, clock=clock)
    return d, calls, clock


# ------------------------------------------------------------------ basic contract
def test_it_fires_the_lamp_at_wake_and_only_on_a_state_change(monkeypatch):
    d, calls, _ = _driver(monkeypatch)
    d.set_therapy(True)
    d.set_therapy(True)
    d.set_therapy(True)
    assert calls == [True], "an already-on plug must not be re-commanded every tick"


def test_it_turns_the_lamp_off_again(monkeypatch):
    d, calls, _ = _driver(monkeypatch)
    d.set_therapy(True)
    d.set_therapy(False)
    assert calls == [True, False]


# ------------------------------------------------------------------ the safety envelope
def test_a_stuck_caller_cannot_leave_the_lamp_on_all_day(monkeypatch):
    """THE safety property. A high-output therapy lamp -- and especially a UV source -- must not
    stay energised because should_wake got stuck, the daemon wedged, or an OFF was missed."""
    d, calls, clock = _driver(monkeypatch, max_on_min=45.0)
    d.set_therapy(True)
    assert calls == [True]
    clock.advance_min(50)
    d.set_therapy(True)                      # caller still insisting
    assert calls == [True, False], "the cap must force it off"


def test_the_cap_latches_so_it_cannot_immediately_re_energise(monkeypatch):
    """Without a latch, the very next tick's still-true should_wake would switch it straight back
    on and the cap would just produce a flicker."""
    d, calls, clock = _driver(monkeypatch, max_on_min=45.0)
    d.set_therapy(True)
    clock.advance_min(50)
    d.set_therapy(True)                      # trips the cap -> OFF
    for _ in range(5):
        d.set_therapy(True)                  # caller keeps insisting
    assert calls == [True, False]


def test_the_cap_clears_once_the_caller_asks_for_off(monkeypatch):
    """A new night must get a fresh dose -- the latch is per-wake, not permanent."""
    d, calls, clock = _driver(monkeypatch, max_on_min=45.0)
    d.set_therapy(True)
    clock.advance_min(50)
    d.set_therapy(True)                      # capped off
    d.set_therapy(False)                     # end of the wake window -> clears the latch
    d.set_therapy(True)                      # next morning
    assert calls[-1] is True


def test_a_failed_off_is_retried_because_off_is_the_safe_direction(monkeypatch):
    """Failing to turn a lamp OFF is the dangerous failure. The driver must keep believing it may
    still be energised so the next tick tries again, rather than concluding it is done."""
    d, calls, _ = _driver(monkeypatch, fail_off=True)
    d.set_therapy(True)
    d.set_therapy(False)
    d.set_therapy(False)
    assert calls == [True, False, False], "a failed OFF must be retried"


def test_a_failed_on_does_not_start_the_on_timer(monkeypatch):
    """If the ON never landed nothing is energised, so the driver must not think it is on."""
    d, calls, _ = _driver(monkeypatch, fail_on=True)
    d.set_therapy(True)
    assert d._on is False and d._on_since is None


def test_a_disabled_cap_means_no_forced_off(monkeypatch):
    d, calls, clock = _driver(monkeypatch, max_on_min=0.0)
    d.set_therapy(True)
    clock.advance_min(600)
    d.set_therapy(True)
    assert calls == [True]


def test_the_driver_never_raises_into_the_control_loop(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("plug exploded")
    monkeypatch.setattr(smart_plug, "switch", boom)
    d = smart_plug.SmartPlugTherapyDriver("tuya", {})
    d.set_therapy(True)          # must not raise
    d.off()


# ------------------------------------------------------------------ backends
def test_an_unknown_backend_is_a_clean_no_op():
    assert smart_plug.switch("nonsense", {}, True) is False
    assert smart_plug.switch("", {}, True) is False


def test_tuya_without_credentials_does_not_attempt_anything():
    assert smart_plug._tuya_switch({}, True) is False
    assert smart_plug._tuya_switch({"device_id": "x"}, True) is False


def test_http_backend_uses_the_right_url(monkeypatch):
    seen: list = []
    monkeypatch.setattr(smart_plug, "_http_switch", lambda u: seen.append(u) or True)
    smart_plug.switch("http", {"on_url": "http://x/on", "off_url": "http://x/off"}, True)
    smart_plug.switch("http", {"on_url": "http://x/on", "off_url": "http://x/off"}, False)
    assert seen == ["http://x/on", "http://x/off"]


def test_http_backend_with_no_url_configured_is_a_no_op():
    assert smart_plug._http_switch("") is False
    assert smart_plug._http_switch(None) is False
