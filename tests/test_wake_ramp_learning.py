"""Learn the wake-window ramp from grogginess (the wake end of the trajectory)."""

import tempfile

import pytest

from sleepctl.config import AppConfig
from sleepctl.learning.wake_ramp import learn_wake_ramp
from sleepctl.models import ContextRecord, NightSummary, SetpointProfile
from sleepctl.storage.repository import Repository

CFG = AppConfig.default()


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def _seed(repo, ramp, grog, version):
    repo.save_setpoints(SetpointProfile(neutral_f=70, deep_bias_f=66, rem_warm_offset_f=1.5,
                                        wake_ramp_f=ramp, composite_bed_weight=0.75,
                                        version=version, source="test"))


def test_thin_history_holds(repo):
    out = learn_wake_ramp(repo, CFG, current_f=74.0)
    assert out == 74.0


def test_warmer_ramp_groggier_nudges_cooler(repo):
    # nights at a WARM ramp (80) were groggy (high), nights at a COOL ramp (72) were not.
    for i in range(4):
        v = 100 + i
        _seed(repo, 80.0, None, v)
        repo.save_night_summary(NightSummary(date=f"2026-06-{10+i:02d}", setpoint_version=v))
        repo.save_context(ContextRecord(date=f"2026-06-{10+i:02d}", grogginess=8.0))
    for i in range(4):
        v = 200 + i
        _seed(repo, 72.0, None, v)
        repo.save_night_summary(NightSummary(date=f"2026-06-{20+i:02d}", setpoint_version=v))
        repo.save_context(ContextRecord(date=f"2026-06-{20+i:02d}", grogginess=2.0))
    out = learn_wake_ramp(repo, CFG, current_f=78.0, step=1.0)
    assert out < 78.0  # warmer ramp -> groggier => learn cooler


def test_no_grogginess_variation_holds(repo):
    for i in range(8):
        v = 300 + i
        _seed(repo, 74.0 + (i % 2) * 4, None, v)  # ramp varies
        repo.save_night_summary(NightSummary(date=f"2026-06-{1+i:02d}", setpoint_version=v))
        repo.save_context(ContextRecord(date=f"2026-06-{1+i:02d}", grogginess=5.0))  # flat
    out = learn_wake_ramp(repo, CFG, current_f=74.0)
    assert out == 74.0
