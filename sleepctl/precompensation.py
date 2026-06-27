"""Environmental pre-compensation — feed-forward bed bias from the overnight forecast.

A reactive loop only corrects after the room has already drifted; for a hot sleeper in a
warm apartment that means waking *then* cooling. This computes a small, bounded feed-forward
bias from tonight's outdoor temperature trajectory (Open-Meteo), so the bed is pre-biased
cooler ahead of a heat soak (or warmer on a cold night). Bounded and advisory — it nudges the
neutral setpoint within a cap; the controller's slew/variability limits still apply.
"""

from __future__ import annotations

from typing import Optional


def compute_precompensation(forecast: Optional[dict], cfg) -> dict:
    """forecast: the dict from OpenMeteoWeather.overnight_forecast() (or None)."""
    t = cfg.tunables
    base = {"bias_f": 0.0, "pre_cool": False, "trend": None,
            "overnight_low_f": None, "overnight_high_f": None,
            "overnight_mean_f": None, "reason": "no forecast available"}
    if not forecast or not forecast.get("hours"):
        return base

    temps = [h["temp_f"] for h in forecast["hours"] if h.get("temp_f") is not None]
    if not temps:
        return base
    mean_f = sum(temps) / len(temps)
    trend = forecast.get("trend")

    bias = 0.0
    if mean_f >= t.precomp_hot_threshold_f:
        bias = -min(t.precomp_max_bias_f,
                    (mean_f - t.precomp_hot_threshold_f) / t.precomp_f_per_deg)
        reason = (f"Warm night (outdoor ~{round(mean_f)}°F overnight) — biasing the bed "
                  f"{abs(round(bias,1))}°F cooler ahead of the heat.")
    elif mean_f <= t.precomp_cold_threshold_f:
        bias = min(t.precomp_max_bias_f,
                   (t.precomp_cold_threshold_f - mean_f) / t.precomp_f_per_deg)
        reason = (f"Cold night (outdoor ~{round(mean_f)}°F overnight) — biasing the bed "
                  f"{round(bias,1)}°F warmer.")
    else:
        reason = f"Mild night (outdoor ~{round(mean_f)}°F overnight) — no pre-compensation needed."

    pre_cool = trend == "warming" and bias < 0
    if pre_cool:
        reason += " Room is forecast to warm overnight — pre-cooling early."

    return {
        "bias_f": round(bias, 2),
        "pre_cool": pre_cool,
        "trend": trend,
        "overnight_low_f": forecast.get("low_f"),
        "overnight_high_f": forecast.get("high_f"),
        "overnight_mean_f": round(mean_f, 1),
        "reason": reason,
    }
