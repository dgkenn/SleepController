"""`magnitude_f` is how big a thermal correction was -- the ledger field the learning loop reads
to score interventions. It was computed as ``abs(target - (bed_temp or target))``, so with no
measured bed temperature the `else` arm subtracted the target from ITSELF and produced exactly
0.0. On this deployment that is every sample of every night (6835/6835 across 2026-08-25..27),
so every intervention ever recorded had magnitude 0.0 and an entire learning input was null.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.loop.cycle import ControlCycle
from sleepctl.models import (ControllerState, Decision, NightObjective, SensorFrame,
                             CorrectionAction, SleepStage, ThermalIntent)
from sleepctl.storage.repository import Repository

T0 = datetime(2026, 8, 27, 23, 0)


def _cycle():
    cfg = AppConfig.default()
    repo = Repository(":memory:")
    return ControlCycle(cfg, repo, SleepController(cfg)), repo


def _decision(target_f, level, i=0):
    return Decision(timestamp=T0 + timedelta(minutes=i), state=ControllerState.MAINTENANCE,
                    objective=NightObjective.OPTIMIZE,
                    thermal_intent=ThermalIntent.SETTLE_COOL, target_temp_f=target_f,
                    target_level=level, action=CorrectionAction.COOLER, reason="test",
                    confidence=0.8)


def _frame(bed_temp_f=None, i=0):
    return SensorFrame(timestamp=T0 + timedelta(minutes=i), stage=SleepStage.LIGHT,
                       heart_rate=60.0, bed_temp_f=bed_temp_f, presence=True)


def _mags(repo):
    return [r[0] for r in repo.conn.execute(
        "SELECT magnitude_f FROM interventions ORDER BY id").fetchall()]


def test_a_real_move_is_never_recorded_as_zero_without_a_bed_temperature():
    c, repo = _cycle()
    c.pending_level(_decision(68.0, -58, 0), _frame(i=0), T0)
    c.pending_level(_decision(66.0, -68, 1), _frame(i=1), T0 + timedelta(minutes=1))
    mags = _mags(repo)
    assert len(mags) == 2
    assert mags[1] == 2.0, "the 68->66F command change must be recorded as 2.0F"


def test_the_measured_error_is_preferred_when_a_bed_temperature_exists():
    c, repo = _cycle()
    c.pending_level(_decision(66.0, -68, 0), _frame(bed_temp_f=70.0, i=0), T0)
    assert _mags(repo)[0] == 4.0


def test_the_first_command_of_a_session_has_nothing_to_measure_against():
    c, repo = _cycle()
    c.pending_level(_decision(66.0, -68, 0), _frame(i=0), T0)
    assert _mags(repo)[0] == 0.0


def test_successive_moves_each_report_their_own_size():
    c, repo = _cycle()
    for i, (t, lv) in enumerate([(68.0, -58), (67.0, -63), (65.0, -72)]):
        c.pending_level(_decision(t, lv, i), _frame(i=i), T0 + timedelta(minutes=i))
    assert _mags(repo)[1:] == [1.0, 2.0]


def test_an_unchanged_level_is_not_logged_at_all():
    """Re-asserting the same level is device compliance, not a new decision."""
    c, repo = _cycle()
    c.pending_level(_decision(66.0, -68, 0), _frame(i=0), T0)
    c.pending_level(_decision(66.0, -68, 1), _frame(i=1), T0 + timedelta(minutes=1))
    assert len(_mags(repo)) == 1
