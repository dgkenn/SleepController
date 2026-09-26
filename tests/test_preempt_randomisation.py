"""Per-episode randomised pre-emption (2026-09-25): each pre-empt EPISODE is assigned once,
acted or withheld, and logged to the steer ledger as maneuver "preempt" so the resolver can
measure whether pre-empting prevents the awakening."""
from datetime import datetime
from types import SimpleNamespace

from sleepctl.benchmarks import perfect_sleep_index
from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import NightSummary


def test_an_episode_is_assigned_once_and_logged_with_its_arm():
    cfg = AppConfig.default()
    cfg.tunables.preempt_withhold_frac = 0.5
    ctrl = SleepController(cfg)
    draws = iter([0.1, 0.9])                  # first episode withheld, second acted
    ctrl._preempt_rng = SimpleNamespace(random=lambda: next(draws))
    # episode 1 -> withheld
    _episode(ctrl, cfg, raw=True)
    assert ctrl.last_preempt_withheld and not ctrl._preempt_cool
    assert ctrl.pending_preempt_event["applied"] == 0
    ctrl.pending_preempt_event = None
    _episode(ctrl, cfg, raw=True)             # same episode: no new draw, no new event
    assert ctrl.last_preempt_withheld and ctrl.pending_preempt_event is None
    _episode(ctrl, cfg, raw=False)            # episode ends
    assert ctrl._preempt_episode is None
    _episode(ctrl, cfg, raw=True)             # episode 2 -> acted
    assert ctrl._preempt_cool and not ctrl.last_preempt_withheld
    assert ctrl.pending_preempt_event["applied"] == 1


def C_path():
    import sleepctl.controller.controller as C
    return C.__file__


def _episode(ctrl, cfg, raw):
    """Run the controller's randomisation block exactly as written, on a given raw decision."""
    src = open(C_path()).read()
    start = src.index("                raw_preempt = self._preempt_cool")
    end = src.index("                    self._preempt_episode = None\n", start) + len(
        "                    self._preempt_episode = None\n")
    body = "\n".join(line[16:] for line in src[start:end].splitlines())
    from sleepctl.controller.controller import PREEMPT_EVENT_HORIZON_MIN

    class _F:
        stage = None
    ctrl._preempt_cool = raw
    exec(body, {"self": ctrl, "cfg": cfg, "now": datetime(2026, 9, 26, 2, 0), "frame": _F(),
                "evidence_backed": True, "PREEMPT_EVENT_HORIZON_MIN": PREEMPT_EVENT_HORIZON_MIN})


def test_withhold_share_is_capped_and_off_means_always_act():
    cfg = AppConfig.default()
    assert 0.0 < cfg.tunables.preempt_withhold_frac <= 0.5
    cfg.tunables.preempt_withhold_frac = 0.0
    ctrl = SleepController(cfg)
    ctrl._preempt_rng = SimpleNamespace(random=lambda: 0.0)
    _episode(ctrl, cfg, raw=True)
    assert ctrl._preempt_cool and ctrl.pending_preempt_event["applied"] == 1
    cfg.tunables.preempt_withhold_frac = 0.9  # capped at 0.5: a draw of 0.6 still acts
    ctrl2 = SleepController(cfg)
    ctrl2._preempt_rng = SimpleNamespace(random=lambda: 0.6)
    _episode(ctrl2, cfg, raw=True)
    assert ctrl2._preempt_cool


def test_the_night_score_trusts_continuity_over_stage_percentages():
    """A night with perfect continuity and 'bad' stager percentages now outscores one with
    ideal percentages and repeated awakenings; trust 1.0 restores the literature weights."""
    calm = NightSummary(date="2026-09-26", total_sleep_min=480, deep_min=20, rem_min=60,
                        light_min=400, wake_events=0, waso_min=5, sleep_efficiency=0.95)
    broken = NightSummary(date="2026-09-27", total_sleep_min=480, deep_min=96, rem_min=120,
                          light_min=264, wake_events=4, waso_min=45, sleep_efficiency=0.88)
    assert perfect_sleep_index(calm)["score"] > perfect_sleep_index(broken)["score"]
    full_calm = perfect_sleep_index(calm, stage_trust=1.0)["score"]
    assert perfect_sleep_index(calm)["score"] > full_calm


def test_the_default_draw_is_uniform_and_reproducible():
    from datetime import timedelta
    from sleepctl.controller.controller import _episode_draw
    t0 = datetime(2026, 9, 26, 1, 0)
    draws = [_episode_draw(t0 + timedelta(minutes=k)) for k in range(2000)]
    assert all(0.0 <= d < 1.0 for d in draws)
    assert 0.27 < sum(d < 0.3 for d in draws) / len(draws) < 0.33
    assert _episode_draw(t0) == _episode_draw(t0.replace(second=42))
