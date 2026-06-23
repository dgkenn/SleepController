"""Confounder handling for training.

Nights disrupted by external causes (illness, travel, alcohol, big schedule changes /
short-sleep) tell us little about how a thermal action performs, and can bias the learner.
We exclude them from training (and flag low confidence) — but NEVER block control: a
confounded night just means the optimizer trains on the clean nights.
"""

from __future__ import annotations

from sleepctl.ml.dataset import FeatureRow


def is_confounded(row: FeatureRow) -> bool:
    return bool(
        row.illness or row.travel or row.alcohol or row.is_short_sleep_day
    )


def clean_rows(rows: list[FeatureRow]) -> list[FeatureRow]:
    """Keep only non-confounded nights for model fitting."""
    return [r for r in rows if not is_confounded(r)]
