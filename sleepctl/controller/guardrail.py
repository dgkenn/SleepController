"""Decision guardrail — a top-level invariant monitor over the recent TRAJECTORY.

Every other detector in ``sleepctl.controller`` (arousal, precursor, wake-risk, steering,
thermal_health, data_quality) judges a *single* frame or a narrow sub-problem. This module is
different: it is a backstop that watches the recent SEQUENCE of frames and decisions for
patterns that no single-tick check would catch —

  (a) sustained aggressive cooling while HR is rising above the sleep baseline (we may be
      DRIVING an arousal instead of preventing one);
  (b) a target setpoint outside the user's personal comfort band (from the in-bed comfort
      sweep, ``repo.get_comfort_profile``), when one exists;
  (c) thermal oscillation — rapid target reversals over a short window (hunting/flapping);
  (d) sustained commanded-vs-device divergence (the bed isn't actually responding), reusing
      ``ThermalHealth`` state when the caller has one.

It is intentionally conservative: a CRITICAL finding recommends/forces a SAFE HOLD (revert
toward neutral, no aggressive move) but the guardrail never picks a target itself — it is a
backstop, not a second controller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from sleepctl.models import CorrectionAction, SensorFrame


@dataclass
class GuardrailFinding:
    code: str
    severity: str  # "info" | "warning" | "critical"
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass
class GuardrailAssessment:
    findings: List[GuardrailFinding] = field(default_factory=list)

    @property
    def critical(self) -> bool:
        return any(f.severity == "critical" for f in self.findings)

    @property
    def triggered(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "critical": self.critical,
            "findings": [f.to_dict() for f in self.findings],
        }


class DecisionGuardrail:
    """Stateless-ish invariant monitor: call ``evaluate`` once per tick with recent history.

    Keeps no state itself beyond the last few reads it's given (all history is passed in by
    the caller — ``recent`` frames and ``recent_decisions``), so it's trivially inspectable
    and never fights the primary controller; it only ever recommends/forces HOLD.
    """

    def __init__(self, cfg) -> None:
        t = getattr(cfg, "tunables", cfg)
        self.window_min = getattr(t, "guardrail_window_min", 20.0)
        self.hr_rise_bpm = getattr(t, "guardrail_hr_rise_bpm", 4.0)
        self.cool_run_count = getattr(t, "guardrail_cool_run_count", 3)
        self.comfort_margin_f = getattr(t, "guardrail_comfort_margin_f", 2.0)
        self.osc_window_min = getattr(t, "guardrail_oscillation_window_min", 30.0)
        self.osc_reversals = getattr(t, "guardrail_oscillation_reversals", 3)
        self.osc_min_delta_f = getattr(t, "guardrail_oscillation_min_delta_f", 0.75)
        self.stall_ticks = getattr(t, "guardrail_stall_ticks", 3)

    def evaluate(
        self,
        recent_frames: List[SensorFrame],
        recent_decisions: list,
        current_target_f: float,
        now: Optional[datetime] = None,
        sleep_hr_baseline: Optional[float] = None,
        comfort_profile: Optional[dict] = None,
        thermal_health=None,
    ) -> GuardrailAssessment:
        findings: List[GuardrailFinding] = []
        now = now or (recent_frames[-1].timestamp if recent_frames else datetime.utcnow())

        findings.extend(self._check_driving_arousal(recent_frames, recent_decisions, now,
                                                     sleep_hr_baseline))
        findings.extend(self._check_comfort_band(current_target_f, comfort_profile))
        findings.extend(self._check_oscillation(recent_decisions, now))
        findings.extend(self._check_device_divergence(thermal_health))

        return GuardrailAssessment(findings=findings)

    # -- (a) sustained aggressive cooling while HR rises above baseline ---------------------
    def _check_driving_arousal(self, recent_frames, recent_decisions, now,
                               sleep_hr_baseline) -> List[GuardrailFinding]:
        if sleep_hr_baseline is None or not recent_decisions:
            return []
        window = [d for d in recent_decisions
                  if (now - d.timestamp).total_seconds() <= self.window_min * 60.0]
        if len(window) < self.cool_run_count:
            return []
        # Sustained = the tail of the window is a consecutive run of COOLER actions.
        tail = window[-self.cool_run_count:]
        if not all(d.action is CorrectionAction.COOLER for d in tail):
            return []
        cutoff = now - timedelta(minutes=self.window_min)
        hrs = [(f.timestamp, f.heart_rate) for f in recent_frames
              if f.heart_rate is not None and f.timestamp >= cutoff]
        if len(hrs) < 3:
            return []
        latest_hr = hrs[-1][1]
        # Require the RISE to be a real, held elevation (not one noisy point): the latest
        # reading AND the recent-tail average must both clear the bar above baseline.
        tail_mean = sum(v for _, v in hrs[-3:]) / min(3, len(hrs))
        if (latest_hr - sleep_hr_baseline >= self.hr_rise_bpm
                and tail_mean - sleep_hr_baseline >= self.hr_rise_bpm * 0.75):
            return [GuardrailFinding(
                code="driving_arousal",
                severity="critical",
                message=(f"sustained cooling ({len(tail)} ticks) while HR is "
                        f"{latest_hr - sleep_hr_baseline:.1f} bpm above sleep baseline — "
                        "may be driving an arousal; recommend HOLD"),
            )]
        return []

    # -- (b) target outside the personal comfort band ----------------------------------------
    def _check_comfort_band(self, current_target_f, comfort_profile) -> List[GuardrailFinding]:
        if not comfort_profile:
            return []
        cool_edge = comfort_profile.get("cool_edge_f")
        warm_edge = comfort_profile.get("warm_edge_f")
        if cool_edge is None or warm_edge is None:
            return []
        lo = min(cool_edge, warm_edge) - self.comfort_margin_f
        hi = max(cool_edge, warm_edge) + self.comfort_margin_f
        if current_target_f < lo or current_target_f > hi:
            return [GuardrailFinding(
                code="outside_comfort_band",
                severity="warning",
                message=(f"target {current_target_f:.1f}F is outside the personal comfort "
                        f"band [{lo:.1f}, {hi:.1f}]F"),
            )]
        return []

    # -- (c) thermal oscillation: rapid target reversals -------------------------------------
    def _check_oscillation(self, recent_decisions, now) -> List[GuardrailFinding]:
        if not recent_decisions:
            return []
        cutoff = now - timedelta(minutes=self.osc_window_min)
        window = [d for d in recent_decisions if d.timestamp >= cutoff]
        if len(window) < 3:
            return []
        targets = [d.target_temp_f for d in window]
        reversals = 0
        last_dir = 0
        for prev, cur in zip(targets, targets[1:]):
            delta = cur - prev
            if abs(delta) < self.osc_min_delta_f:
                continue
            direction = 1 if delta > 0 else -1
            if last_dir != 0 and direction != last_dir:
                reversals += 1
            last_dir = direction
        if reversals >= self.osc_reversals:
            return [GuardrailFinding(
                code="thermal_oscillation",
                severity="critical",
                message=(f"{reversals} target reversals within {self.osc_window_min:.0f} min — "
                        "hunting/flapping; recommend HOLD"),
            )]
        return []

    # -- (d) sustained commanded-vs-device divergence ----------------------------------------
    def _check_device_divergence(self, thermal_health) -> List[GuardrailFinding]:
        if thermal_health is None:
            return []
        state = getattr(thermal_health, "state", None)
        if state == "stalled":
            return [GuardrailFinding(
                code="device_divergence",
                severity="warning",
                message=(getattr(thermal_health, "reason", None)
                        or "device level not tracking the commanded target"),
            )]
        return []
