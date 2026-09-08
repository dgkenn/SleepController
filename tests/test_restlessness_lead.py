"""The restlessness-lead learner: how far ahead of awakenings movement density climbs."""
from datetime import datetime, timedelta

from sleepctl.learning.wake_causation import restlessness_lead_profile
from sleepctl.storage.repository import Repository


class _Repo:
    def __init__(self, conn, dates):
        self.conn = conn
        self._dates = dates

    def recent_nights(self, n):
        class _S:
            def __init__(self, d): self.date = d
        return [_S(d) for d in self._dates]


def _night(repo, night, wake_at_minutes, ramp_min):
    t0 = datetime(2026, 9, 1, 23, 0)
    for i in range(0, 420):                              # 7 h at 1 sample/min
        t = t0 + timedelta(minutes=i)
        mv = 0.05
        wake = 0
        for w in wake_at_minutes:
            if w - ramp_min <= i < w:
                mv = 0.3 if (i % 2 == 0) else 0.1      # the ramp: every other minute a burst
            if i == w:
                wake, mv = 1, 0.6
        repo.conn.execute(
            "INSERT INTO raw_samples (ts, night_date, controller_state, movement, wake_event) "
            "VALUES (?,?,?,?,?)", (t.isoformat(), night, "maintenance", mv, wake))
    repo.conn.commit()


def test_a_consistent_ramp_yields_a_lead_and_a_trigger_ratio(tmp_path):
    r = Repository(str(tmp_path / "r.db"), check_same_thread=False)
    repo = _Repo(r.conn, ["2026-09-01"])
    _night(r, "2026-09-01", [90, 180, 260, 330, 400], ramp_min=8)
    prof = restlessness_lead_profile(repo, min_events=5)
    assert prof["predictive"] is True, prof
    assert 5.0 <= prof["lead_min"] <= 10.0
    assert prof["ratio_threshold"] >= 1.3


def test_awakenings_without_a_ramp_are_reported_as_such(tmp_path):
    r = Repository(str(tmp_path / "r.db"), check_same_thread=False)
    repo = _Repo(r.conn, ["2026-09-01"])
    _night(r, "2026-09-01", [90, 180, 260, 330, 400], ramp_min=0)
    prof = restlessness_lead_profile(repo, min_events=5)
    assert prof["predictive"] is False and prof["n"] == 5
