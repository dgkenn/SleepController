"""Advisory CBT-I sleep-window module — sleep restriction therapy (SRT) and stimulus-control
guidance, computed and explained, never enforced.

Framing (why this is advisory-only, not a controller):
  The user's system is a thermal sleep controller; CBT-I is explicitly a SIDE feature. The primary
  complaint is *maintenance* insomnia (fragmented sleep, awakenings), for which SRT + stimulus
  control are the first-line, highest-effect-size evidence-based interventions — larger than any
  thermal manipulation this system performs. But this module must never behave like the controller:
  it takes no actions, holds no state, touches no I/O, and gates nothing. It computes one number
  (a recommended time-in-bed / bedtime) and shows its work in plain language, exactly like a
  clinician's reasoning would read, so the user (an anaesthesia trainee and a clinician himself) can
  evaluate and override it.

Shift-safety is load-bearing, not cosmetic: this user works rotating shifts and on-call nights.
Classic sleep restriction *increases* daytime sleepiness during the titration phase (that's the
mechanism — it raises sleep pressure). Deliberately inducing sleepiness in someone about to
administer anaesthesia is not a tradeoff this module is allowed to make quietly. Any time the caller
flags an upcoming high-stakes duty (`upcoming_high_stakes=True`), or the data already shows the user
is short-sleeping hard (so a restriction would compound an existing deficit rather than treat a
mismatch between time-in-bed and sleep ability), the module refuses to compress and says why.

Pure-function, stdlib + typing only. No numpy, no DB access, no I/O, no imports from the rest of
sleepctl — this file is meant to be wired up later (via sleepctl/config.py) but stands alone today.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from typing import Any, List, Literal, Optional, Sequence, Union

Direction = Literal["compress", "expand", "hold"]

# Night-record fields that mark a night as ineligible for the efficiency estimate (naps, short
# "work" nights deliberately truncated for a shift, etc.) — these are not representative of the
# user's normal sleep ability and would bias the efficiency estimate.
_INELIGIBLE_NIGHT_TYPES = {"nap", "short_night", "short", "work_night", "work"}


# --------------------------------------------------------------------------- config


@dataclass
class CBTIConfig:
    """Tunable policy for the CBT-I advisor. All defaults are documented judgment calls grounded
    in standard CBT-I practice (Spielman/Morin protocols); see sleepctl/cbti.py module docstring
    and the accompanying report for citations and caveats."""

    # Sleep-efficiency band that drives the compress / hold / expand decision.
    # efficiency = total_sleep_min / time_in_bed_min. Standard SRT titrates weekly using an
    # 85%/90% band (compress below 85%, expand above 90%, hold in between).
    compress_below_efficiency: float = 0.85
    expand_above_efficiency: float = 0.90

    # One "step" of adjustment. Standard CBT-I protocols adjust in 15-20 min increments, no more
    # than about once per week, to avoid overshooting.
    step_min: int = 15

    # Safety floor: never recommend a time-in-bed prescription below this, regardless of how poor
    # efficiency looks. 5.5 h (330 min) is the conventional CBT-I minimum TIB floor (below this,
    # further restriction has diminishing therapeutic return and materially raises next-day
    # impairment risk) — see module report for the specific judgment call given this user's
    # anaesthesia-trainee shift work.
    min_time_in_bed_min: int = 330

    # Nights of *eligible* data required (within the rolling window) before advising anything other
    # than a low-confidence hold. CBT-I titration decisions are conventionally made on ~1-2 weeks
    # of sleep-diary data, not a single night.
    min_nights_required: int = 7

    # How many most-recent eligible nights feed the rolling efficiency/sleep-time estimate.
    rolling_window_nights: int = 14

    # Safety ceiling on cumulative change this module will ever suggest in one call. Because this
    # module is stateless (no memory of when it last ran), this only clamps the *single-call* step;
    # true weekly-cadence enforcement needs an external caller to track "last adjusted on" and pass
    # nights accordingly. Documented here as a placeholder for that future wiring.
    max_weekly_change_min: int = 30

    # Buffer added on top of mean total sleep time when compressing, so the new time-in-bed target
    # isn't literally equal to the measured sleep time (leaves a little room to fall asleep / for
    # night-to-night variance) — standard CBT-I practice.
    buffer_min: int = 15

    # "Already sleep-deprived" guard: if the recent average total sleep time is below this many
    # minutes, refuse to compress even if efficiency is poor — the problem there is insufficient
    # sleep opportunity, not excess time-in-bed, and compressing further would be harmful.
    severe_short_sleep_min: int = 300  # 5.0 h
    # How many of the most recent eligible nights are checked for the severe-short-sleep guard.
    recent_severe_check_nights: int = 3

    # --- stimulus-control tip thresholds ---
    # An awakening lasting at least this long is what stimulus control addresses ("get out of bed").
    long_awakening_min: int = 20
    # Fraction of pooled awakenings (when duration data is available) that must be "long" to
    # surface the get-out-of-bed tip.
    long_awakening_fraction: float = 0.30
    # Average awakenings/night (when only a count, not durations, is available) that is high enough
    # to surface the same tip on frequency grounds alone.
    frequent_awakenings_per_night: float = 2.0
    # Standard deviation of bedtime (minutes) above which bedtime is considered "highly variable",
    # supporting a consistent-anchor-time tip.
    bedtime_sd_threshold_min: float = 45.0


# --------------------------------------------------------------------------- records


@dataclass
class NightRecord:
    """A single night's sleep-diary data. Callers may pass these, or plain dicts with the same
    keys — every function in this module reads fields defensively via `_get`, so either works.

    wake_events may be either an int (a simple awakening *count*) or a list[int] of individual
    awakening durations in minutes — richer duration data enables the more specific "long
    awakenings" stimulus-control tip; a bare count still supports the frequency-based version.

    bedtime, if supplied, is "HH:MM" (24h) or a datetime.time, used only for the bedtime-variability
    stimulus-control tip. It is optional and never invented from other fields.
    """

    date: Any
    time_in_bed_min: float
    total_sleep_min: float
    sleep_efficiency: float
    wake_events: Any = field(default_factory=list)
    night_type: Optional[str] = None  # "normal" | "nap" | "short_night" | "work_night" | ...
    subjective_rating: Optional[float] = None
    bedtime: Optional[Union[str, dt_time]] = None
    eligible: Optional[bool] = None  # explicit override; None = infer from night_type


# --------------------------------------------------------------------------- output


@dataclass
class SleepWindowAdvice:
    direction: Direction
    recommended_tib_min: int
    change_min: int  # signed: negative = compress, positive = expand, 0 = hold
    rationale: str
    confidence: float  # 0..1
    safe: bool
    safety_notes: List[str] = field(default_factory=list)
    recommended_bedtime: Optional[str] = None  # "HH:MM", only if required_wake_time was given

    # Extra transparency fields (not required by the spec, but this is for a quantitatively-minded
    # clinician user — showing the inputs behind the number is the point).
    baseline_tib_min: Optional[int] = None
    mean_efficiency: Optional[float] = None
    mean_total_sleep_min: Optional[float] = None
    eligible_nights: int = 0


# --------------------------------------------------------------------------- helpers


def _get(rec: Any, key: str, default: Any = None) -> Any:
    if isinstance(rec, dict):
        return rec.get(key, default)
    return getattr(rec, key, default)


def _is_eligible(rec: Any) -> bool:
    explicit = _get(rec, "eligible", None)
    if explicit is not None:
        return bool(explicit)
    night_type = _get(rec, "night_type", None)
    if night_type and str(night_type).strip().lower() in _INELIGIBLE_NIGHT_TYPES:
        return False
    # Missing required numeric fields also makes a night unusable for the estimate.
    for field_name in ("time_in_bed_min", "total_sleep_min", "sleep_efficiency"):
        if _get(rec, field_name, None) is None:
            return False
    return True


def _sort_key(rec: Any):
    d = _get(rec, "date")
    # Sort strings/dates as-is; fall back to identity ordering (stable) if incomparable/missing.
    return (0, d) if d is not None else (1, 0)


def _recent_eligible(nights: Sequence[Any], window: int) -> List[Any]:
    ordered = sorted(nights, key=_sort_key)
    windowed = ordered[-window:] if window > 0 else ordered
    return [n for n in windowed if _is_eligible(n)]


def _awakening_durations(nights: Sequence[Any]) -> List[float]:
    """Pool individual awakening durations (minutes) from nights whose wake_events is a list."""
    durations: List[float] = []
    for n in nights:
        we = _get(n, "wake_events", None)
        if isinstance(we, (list, tuple)):
            durations.extend(float(x) for x in we if isinstance(x, (int, float)))
    return durations


def _wake_count(rec: Any) -> Optional[float]:
    we = _get(rec, "wake_events", None)
    if we is None:
        return None
    if isinstance(we, (list, tuple)):
        return float(len(we))
    if isinstance(we, (int, float)):
        return float(we)
    return None


def _parse_time_of_day(t: Union[str, dt_time]) -> dt_time:
    if isinstance(t, dt_time):
        return t
    if isinstance(t, str):
        hh, mm = t.split(":")[:2]
        return dt_time(int(hh), int(mm))
    raise TypeError(f"Unsupported time-of-day value: {t!r}")

def _minutes_of_day(t: dt_time) -> int:
    return t.hour * 60 + t.minute


def _bedtime_string(wake_time: dt_time, tib_min: int) -> str:
    anchor = datetime(2000, 1, 1, wake_time.hour, wake_time.minute)
    bedtime_dt = anchor - timedelta(minutes=tib_min)
    return f"{bedtime_dt.hour:02d}:{bedtime_dt.minute:02d}"


def _confidence(n_eligible: int, window: int) -> float:
    return round(min(1.0, 0.5 + 0.5 * (n_eligible / window)), 2) if window > 0 else 0.5


def _low_confidence(n_eligible: int, required: int) -> float:
    if required <= 0:
        return 0.1
    return round(min(0.4, 0.1 + 0.3 * (n_eligible / required)), 2)


# --------------------------------------------------------------------------- main advisory


def recommend_sleep_window(
    nights: Sequence[Any],
    *,
    required_wake_time: Optional[Union[str, dt_time]] = None,
    upcoming_high_stakes: bool = False,
    cfg: Optional[CBTIConfig] = None,
) -> SleepWindowAdvice:
    """Compute an advisory sleep-window recommendation. Never mutates `nights`, never touches any
    controller/config/storage state, and never raises for merely-imperfect input (missing/ineligible
    nights degrade gracefully into a low-confidence hold).

    `nights`: per-night records (NightRecord or dict) — see NightRecord docstring for fields.
    `required_wake_time`: if given ("HH:MM" or datetime.time), a recommended bedtime is also
        computed by subtracting the recommended time-in-bed from this fixed wake time.
    `upcoming_high_stakes`: set True for an upcoming on-call/night-shift/high-stakes duty. Forces a
        hold instead of any compression, with a safety note — see module docstring.
    """
    cfg = cfg or CBTIConfig()

    eligible = _recent_eligible(nights, cfg.rolling_window_nights)
    n_eligible = len(eligible)

    if n_eligible < cfg.min_nights_required:
        return SleepWindowAdvice(
            direction="hold",
            recommended_tib_min=0,
            change_min=0,
            rationale=(
                f"Not enough nights yet: {n_eligible} eligible night(s) of data, but "
                f"{cfg.min_nights_required} are required before advising a sleep-window change. "
                "Holding — keep logging nights (naps and deliberately short/work nights don't "
                "count toward this)."
            ),
            confidence=_low_confidence(n_eligible, cfg.min_nights_required),
            safe=True,
            safety_notes=[],
            recommended_bedtime=None,
            baseline_tib_min=None,
            mean_efficiency=None,
            mean_total_sleep_min=None,
            eligible_nights=n_eligible,
        )

    mean_eff = statistics.mean(float(_get(n, "sleep_efficiency")) for n in eligible)
    mean_sleep = statistics.mean(float(_get(n, "total_sleep_min")) for n in eligible)
    baseline_tib = round(statistics.mean(float(_get(n, "time_in_bed_min")) for n in eligible))

    confidence = _confidence(n_eligible, cfg.rolling_window_nights)
    safety_notes: List[str] = []

    # --- severe short-sleep guard: checked regardless of direction, but only forces an override
    # when it would otherwise block a compression. ---
    recent_n = min(cfg.recent_severe_check_nights, n_eligible)
    recent_slice = sorted(eligible, key=_sort_key)[-recent_n:]
    recent_mean_sleep = statistics.mean(float(_get(n, "total_sleep_min")) for n in recent_slice)
    severe_short_sleep = recent_mean_sleep < cfg.severe_short_sleep_min

    would_compress = mean_eff < cfg.compress_below_efficiency
    would_expand = mean_eff > cfg.expand_above_efficiency

    if would_compress and upcoming_high_stakes:
        safety_notes.append(
            "Compression withheld: an upcoming high-stakes duty (on-call / night shift) was "
            "flagged. Sleep restriction deliberately raises daytime sleepiness during titration — "
            "appropriate for a quiet week, not before administering anaesthesia. Holding the "
            "current time-in-bed instead."
        )
        direction: Direction = "hold"
        new_tib = baseline_tib
    elif would_compress and severe_short_sleep:
        safety_notes.append(
            f"Compression withheld: the last {recent_n} eligible night(s) averaged "
            f"{recent_mean_sleep:.0f} min of total sleep (< {cfg.severe_short_sleep_min} min). "
            "You're already short-sleeping — the problem right now is insufficient sleep "
            "opportunity, not excess time-in-bed, so restricting further would compound the "
            "deficit rather than treat it. Holding instead."
        )
        direction = "hold"
        new_tib = baseline_tib
    elif would_compress:
        target_floor = max(cfg.min_time_in_bed_min, round(mean_sleep) + cfg.buffer_min)
        step = min(cfg.step_min, cfg.max_weekly_change_min)
        new_tib = max(target_floor, baseline_tib - step)
        if new_tib >= baseline_tib:
            direction = "hold"
            new_tib = baseline_tib
            if target_floor >= baseline_tib:
                safety_notes.append(
                    f"Time-in-bed is already at/near the recommended floor ({cfg.min_time_in_bed_min} "
                    "min) or at your average-sleep-plus-buffer target — no further compression "
                    "recommended even though efficiency is below threshold."
                )
        else:
            direction = "compress"
    elif would_expand:
        step = min(cfg.step_min, cfg.max_weekly_change_min)
        new_tib = baseline_tib + step
        direction = "expand"
    else:
        direction = "hold"
        new_tib = baseline_tib

    # Universal defensive clamp: whatever branch produced new_tib, never recommend below the
    # safety floor. This mainly guards the pathological case where the *baseline* itself (the
    # user's actual current time-in-bed, from the data) is already below the floor — the hold
    # branches above would otherwise just echo that baseline back unchanged.
    new_tib = max(new_tib, cfg.min_time_in_bed_min)
    change_min = new_tib - baseline_tib

    rationale = _build_rationale(
        direction=direction,
        n_eligible=n_eligible,
        mean_eff=mean_eff,
        mean_sleep=mean_sleep,
        baseline_tib=baseline_tib,
        new_tib=new_tib,
        change_min=change_min,
        cfg=cfg,
        safety_notes=safety_notes,
    )

    recommended_bedtime = None
    if required_wake_time is not None:
        wake_t = _parse_time_of_day(required_wake_time)
        recommended_bedtime = _bedtime_string(wake_t, new_tib)

    return SleepWindowAdvice(
        direction=direction,
        recommended_tib_min=new_tib,
        change_min=change_min,
        rationale=rationale,
        confidence=confidence,
        safe=True,
        safety_notes=safety_notes,
        recommended_bedtime=recommended_bedtime,
        baseline_tib_min=baseline_tib,
        mean_efficiency=round(mean_eff, 4),
        mean_total_sleep_min=round(mean_sleep, 1),
        eligible_nights=n_eligible,
    )


def _build_rationale(
    *, direction: Direction, n_eligible: int, mean_eff: float, mean_sleep: float,
    baseline_tib: int, new_tib: int, change_min: int, cfg: CBTIConfig,
    safety_notes: List[str],
) -> str:
    base = (
        f"Mean sleep efficiency over the last {n_eligible} eligible night(s) is "
        f"{mean_eff:.1%} (mean total sleep {mean_sleep:.0f} min, current time-in-bed "
        f"~{baseline_tib} min)."
    )
    if safety_notes:
        return base + " " + " ".join(safety_notes)
    if direction == "compress":
        return (
            f"{base} That's below the {cfg.compress_below_efficiency:.0%} compression threshold, "
            f"indicating more time-in-bed than sleep ability supports (fragmented/inefficient "
            f"sleep). Compressing time-in-bed by {abs(change_min)} min (one step) to {new_tib} min, "
            f"moving toward your average sleep time plus a {cfg.buffer_min}-min buffer. This raises "
            f"sleep pressure and should consolidate sleep across the night. The {cfg.min_time_in_bed_min}"
            f"-min safety floor was not crossed."
        )
    if direction == "expand":
        return (
            f"{base} That's above the {cfg.expand_above_efficiency:.0%} expansion threshold — "
            f"you're sleeping efficiently within the current window, so there's room to extend "
            f"time-in-bed by {change_min} min (one step) to {new_tib} min and get more total sleep "
            f"without reintroducing fragmentation."
        )
    return (
        f"{base} That's within the {cfg.compress_below_efficiency:.0%}-{cfg.expand_above_efficiency:.0%} "
        "target band, so holding the current time-in-bed unchanged."
    )


# --------------------------------------------------------------------------- stimulus control


def stimulus_control_tips(nights: Sequence[Any], cfg: Optional[CBTIConfig] = None) -> List[str]:
    """Return only the standard, evidence-based stimulus-control tips that the data actually
    supports. Empty list if no qualifying pattern is present or there isn't enough data — this
    function never returns generic sleep-hygiene filler."""
    cfg = cfg or CBTIConfig()
    eligible = _recent_eligible(nights, cfg.rolling_window_nights)
    if len(eligible) < cfg.min_nights_required:
        return []

    tips: List[str] = []

    # --- long / frequent awakenings -> get out of bed after ~20 min ---
    durations = _awakening_durations(eligible)
    long_awakening_supported = False
    if durations:
        long_frac = sum(1 for d in durations if d >= cfg.long_awakening_min) / len(durations)
        if long_frac >= cfg.long_awakening_fraction:
            long_awakening_supported = True
            tips.append(
                f"Your awakenings are often long ({long_frac:.0%} of logged awakenings lasted "
                f"≥{cfg.long_awakening_min} min). Stimulus control: if you're awake in bed for "
                f"more than about {cfg.long_awakening_min} minutes (falling asleep or after waking), "
                "get up and do something quiet in low light until sleepy, then return to bed. This "
                "keeps the bed associated with sleeping, not with lying awake."
            )
    if not long_awakening_supported:
        counts = [c for c in (_wake_count(n) for n in eligible) if c is not None]
        if counts and statistics.mean(counts) >= cfg.frequent_awakenings_per_night:
            tips.append(
                f"You're waking frequently (averaging {statistics.mean(counts):.1f} awakenings/"
                "night). Stimulus control: if you can't fall back asleep within about "
                f"{cfg.long_awakening_min} minutes of waking, get out of bed until you feel sleepy "
                "again rather than lying there awake — this is the standard fragmented-sleep "
                "countermeasure and matches your primary complaint (staying asleep)."
            )

    # --- variable bedtime -> anchor a consistent wake time ---
    bedtimes_min = []
    for n in eligible:
        bt = _get(n, "bedtime", None)
        if bt is None:
            continue
        t = _parse_time_of_day(bt)
        m = _minutes_of_day(t)
        # Bedtimes after midnight (e.g. 00:30) are "later" than an evening bedtime (e.g. 23:00) on
        # the same sleep-timeline axis; unwrap so 00:30 sorts after 23:00 rather than before it.
        if m < 12 * 60:
            m += 24 * 60
        bedtimes_min.append(m)
    if len(bedtimes_min) >= cfg.min_nights_required:
        sd = statistics.pstdev(bedtimes_min)
        if sd >= cfg.bedtime_sd_threshold_min:
            tips.append(
                f"Your bedtime varied by about {sd:.0f} min (SD) over the last {len(bedtimes_min)} "
                "logged nights. Stimulus control/SRT both work by anchoring a fixed wake time (and "
                "as close to a fixed bedtime as your schedule allows) so your sleep drive and "
                "circadian signal line up consistently, rather than shifting night to night."
            )

    return tips
