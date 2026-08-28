"""The pre-emption lead had silently sat on a 12-minute preset forever, because both of its
measured sources are structurally unavailable here:

  * `learn_response_lag` requires `bed_temp_f IS NOT NULL` -- 6514 consecutive nulls, since the
    Pod exposes no sensed bed temperature -- so it can never fire;
  * the in-bed self-test has not been run.

Against the measurable thermal latency model the bed traverses the whole comfort band in ~9
minutes cooling, while the preset produced window leads of 14-24 minutes: pre-emption was
starting roughly twice as early as the hardware needs, which is part of why it fired on 43% of
maintenance ticks.
"""
from sleepctl.learning.lead_time import (_DEFAULT_LAG_MIN, _reach_time_lag,
                                         build_lead_time_profile)
from sleepctl.storage.repository import Repository

BAND = {"neutral_f": 69.0, "cool_edge_f": 67.0, "warm_edge_f": 69.5, "source": "test"}


def _repo(band=BAND):
    r = Repository(":memory:")
    if band:
        r.save_comfort_profile(band)
    return r


def test_reach_time_is_derived_without_any_bed_temperature():
    """The whole point: this must work on a deployment whose Pod reports no bed temp."""
    lag = _reach_time_lag(_repo())
    assert lag is not None and lag > 0


def test_the_reach_time_is_shorter_than_the_preset_it_replaces():
    lag = _reach_time_lag(_repo())
    assert lag < _DEFAULT_LAG_MIN, "the preset was starting pre-emption earlier than needed"


def test_a_wider_band_takes_longer_to_traverse():
    narrow = _reach_time_lag(_repo({"neutral_f": 68.0, "cool_edge_f": 67.5,
                                    "warm_edge_f": 68.5, "source": "t"}))
    wide = _reach_time_lag(_repo({"neutral_f": 68.0, "cool_edge_f": 62.0,
                                  "warm_edge_f": 74.0, "source": "t"}))
    assert wide > narrow


def test_no_comfort_band_leaves_the_caller_on_its_existing_fallbacks():
    assert _reach_time_lag(_repo(band=None)) is None


def test_a_broken_repo_returns_none_rather_than_raising():
    class Broken:
        def get_comfort_profile(self):
            raise RuntimeError("no table")
    assert _reach_time_lag(Broken()) is None


def test_the_built_profile_reports_where_its_lag_came_from():
    """`source` is how anyone later can tell a measured lead from a guessed one."""
    prof = build_lead_time_profile(_repo())
    assert prof.source in ("reach_time", "learned", "measured")


def test_the_built_leads_are_shorter_than_the_preset_derived_ones():
    from sleepctl.learning.lead_time import LeadTimeProfile
    preset = LeadTimeProfile.evidence_default()
    built = build_lead_time_profile(_repo())
    assert built.leads["recurring"] < preset.leads["recurring"]


def test_every_window_type_still_gets_a_lead():
    built = build_lead_time_profile(_repo())
    for w in ("cycle_boundary", "recurring", "circadian", "warm_threshold"):
        assert built.leads.get(w) is not None and built.leads[w] > 0
