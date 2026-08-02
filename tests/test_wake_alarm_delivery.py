"""The vibration alarm actually reaching the device.

Audio is deliberately off — silence is a hard requirement — so vibration is the ONLY wake
mechanism. Anything that loses it is the difference between waking up and not, which makes the
delivery path worth pinning separately from the logic that decides when to fire.

The bug these were written for: ``pending_alarm()`` marked the spec sent when it HANDED IT OVER,
not when the device accepted it. One transient failure at the wake moment — a cloud 5xx, a token
refresh, a network blip, or the Pod having no alarm slot to drive — silently discarded the alarm
for the entire night, with the spec recorded as delivered.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sleepctl.config import AppConfig
from sleepctl.controller.smart_wake import WakeAlarmSpec


class _Ctl:
    """Minimal stand-in for the controller's one relevant attribute."""

    def __init__(self, spec=None):
        self.pending_wake_alarm = spec


def _cycle(spec):
    from sleepctl.loop.cycle import ControlCycle

    c = ControlCycle.__new__(ControlCycle)
    c.controller = _Ctl(spec)
    c._wake_alarm_sent = False
    return c


SPEC = "alarm-spec"


# ------------------------------------------------------------------ delivery semantics
def test_a_pending_alarm_is_offered():
    assert _cycle(SPEC).pending_alarm() == SPEC


def test_it_keeps_being_offered_until_confirmed():
    """THE regression. A failed send must leave the alarm pending so the next tick retries."""
    c = _cycle(SPEC)
    assert c.pending_alarm() == SPEC      # tick 1: handed over, send raises
    assert c.pending_alarm() == SPEC      # tick 2: must still be offered
    assert c.pending_alarm() == SPEC      # tick 3: and again


def test_confirming_stops_it_being_re_sent():
    """Idempotent on the device, but re-PUTting every tick all night is pointless traffic."""
    c = _cycle(SPEC)
    assert c.pending_alarm() == SPEC
    c.mark_alarm_sent()
    assert c.pending_alarm() is None
    assert c.pending_alarm() is None


def test_no_alarm_pending_offers_nothing():
    assert _cycle(None).pending_alarm() is None


def test_confirming_without_a_pending_alarm_is_harmless():
    c = _cycle(None)
    c.mark_alarm_sent()
    assert c.pending_alarm() is None


# ------------------------------------------------------------------ the spec itself
def _spec_for(cfg, now, wake):
    from sleepctl.controller.smart_wake import SmartWakeRoutine

    return SmartWakeRoutine(cfg).alarm_spec(now, wake)


def test_vibration_power_comes_from_config_when_enabled():
    cfg = AppConfig.default()
    cfg.tunables.wake_vibration_enabled = True
    cfg.tunables.wake_vibration_power = 55
    wake = datetime(2026, 8, 1, 7, 0)
    spec = _spec_for(cfg, wake - timedelta(minutes=5), wake)
    assert spec is not None
    assert spec.vibration_power == 55


def test_disabling_vibration_yields_zero_power_not_a_missing_alarm():
    """Power 0 still programs the alarm (the thermal ramp remains); it just doesn't buzz."""
    cfg = AppConfig.default()
    cfg.tunables.wake_vibration_enabled = False
    wake = datetime(2026, 8, 1, 7, 0)
    spec = _spec_for(cfg, wake - timedelta(minutes=5), wake)
    assert spec is not None and spec.vibration_power == 0


def test_no_alarm_before_the_wake_window_opens():
    cfg = AppConfig.default()
    wake = datetime(2026, 8, 1, 7, 0)
    early = wake - timedelta(hours=4)
    assert _spec_for(cfg, early, wake) is None


# ------------------------------------------------------------------ the escalation ladder
def _orch(**kw):
    from sleepctl.controller.wake_orchestrator import WakeConfig, WakeOrchestrator

    return WakeOrchestrator(WakeConfig(**kw))


def test_vibration_escalates_and_is_never_silent_at_the_deadline():
    """Whatever happened earlier in the window, the deadline fires at full power. Oversleeping
    because the gentle nudge went unnoticed is the one outcome that must be impossible."""
    from sleepctl.models import SensorFrame, SleepStage

    cfg = AppConfig.default()
    deadline = datetime(2026, 8, 1, 7, 0)
    orch = _orch(window_min=30)

    def frame(t):
        return SensorFrame(timestamp=t, stage=SleepStage.DEEP, presence=True, heart_rate=50.0,
                           hrv=60.0, respiratory_rate=13.0, movement=0.01, bed_temp_f=72.0,
                           room_temp_f=68.0, data_age_seconds=5)

    at_deadline = orch.evaluate(deadline, frame(deadline), [], deadline)
    assert at_deadline.should_wake is True
    assert at_deadline.vibration_power == orch.cfg.max_vibration > 0
    assert at_deadline.vibration_pulse != "off"


def test_disabling_vibration_zeroes_every_rung_of_the_ladder():
    """A user who turns vibration off must not get a surprise buzz from the escalation path."""
    cfg = AppConfig.default()
    cfg.tunables.wake_vibration_enabled = False
    from sleepctl.controller.wake_orchestrator import WakeConfig

    wc = WakeConfig.from_tunables(cfg.tunables)
    assert wc.gentle_vibration == 0
    assert wc.strong_vibration == 0
    assert wc.max_vibration == 0


def test_audio_is_never_used_at_any_power():
    """Silence is a hard requirement: the ladder only ever has a vibration channel."""
    from sleepctl.controller.wake_orchestrator import WakeAction

    fields = WakeAction.__dataclass_fields__
    assert "vibration_power" in fields
    assert not any("audio" in f for f in fields), fields.keys()


# ------------------------------------------------------------------ failure containment
def test_a_failing_alarm_send_does_not_stop_the_rest_of_the_tick():
    """Unhandled, the failure propagated to the tick handler, which HOLDS the whole control loop.
    A missing alarm slot then cost thermal steering on every tick through the wake window too --
    the alarm is a backstop for the in-loop ladder, and must never take maintenance down with it.

    Asserted against the source because reproducing it needs a live daemon + cloud client; the
    property is structural: the send is inside its own try, and mark_alarm_sent is in that try."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "dashboard" / "daemon" / "live_daemon.py").read_text()
    m = re.search(r"alarm = self\.cycle\.pending_alarm\(\).*?mark_alarm_sent\(\)", src, re.S)
    assert m, "the alarm send block moved -- re-check that it is still contained"
    block = m.group(0)
    assert "try:" in block, "the alarm send must be inside its own try"
    idx_try, idx_send = block.index("try:"), block.index("set_wake_alarm")
    assert idx_try < idx_send, "the try must OPEN before the send"
    assert "except" in src[m.start():m.start() + len(block) + 400], "…and be caught"


def test_a_failed_send_is_recorded_as_a_degradation_not_swallowed():
    """It must reach the `degraded` check (and so the health snapshot), or a wake alarm that
    never programs looks exactly like one that did."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "dashboard" / "daemon" / "live_daemon.py").read_text()
    i = src.index("await self.client.set_wake_alarm")
    window = src[i:i + 2400]
    assert "_skip(" in window, "a failed alarm send must be recorded to the degradation ledger"
    assert "Eight Sleep app" in window, "the remedy for a missing alarm slot must be stated"
    # A 402/403 is a SERVER refusal no client can talk past: it must latch and name the fallback,
    # not retry forever against a wall.
    assert "402" in window and "403" in window, "a subscription refusal must be detected as such"
    assert "alarm_write_denied" in window, "the refusal must be latched and published"
