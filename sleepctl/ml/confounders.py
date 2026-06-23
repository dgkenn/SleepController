"""Confounder handling for training.

Nights disrupted by external causes (illness, travel, alcohol, big schedule changes /
short-sleep) tell us little about how a thermal action performs, and can bias the learner.
We exclude them from training (and flag low confidence) — but NEVER block control: a
confounded night just means the optimizer trains on the clean nights.
"""

from __future__ import annotations

from sleepctl.ml.dataset import FeatureRow


# A night with this many manual overrides reflects the user's manual temps, not the
# automated setpoint, so it's excluded from automated-action attribution (it still informs
# the revealed-preference anchor in ml/preference.py).
MANUAL_OVERRIDE_CONFOUND = 3


def is_confounded(row: FeatureRow) -> bool:
    return bool(
        row.illness or row.travel or row.alcohol or row.is_short_sleep_day
        or (row.manual_overrides or 0) >= MANUAL_OVERRIDE_CONFOUND
    )


def clean_rows(rows: list[FeatureRow]) -> list[FeatureRow]:
    """Keep only non-confounded nights for model fitting."""
    return [r for r in rows if not is_confounded(r)]
