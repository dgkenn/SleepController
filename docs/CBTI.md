# Advisory CBT-I — sleep-window guidance that never touches control

`sleepctl/cbti.py` + `GET /cbti/advice`. Sleep restriction therapy (SRT) and stimulus-control
guidance, computed and explained, **never enforced**.

## Why this is advisory-only, not a controller

The user's system is a thermal sleep *controller*; CBT-I is explicitly a side feature. The primary
complaint is maintenance insomnia (fragmented sleep, awakenings), for which SRT + stimulus control
are the first-line, highest-effect-size evidence-based interventions in the general literature —
larger than any thermal manipulation this system performs. But `cbti.py` must never behave like the
controller: it takes no actions, holds no state, touches no I/O, and gates nothing. It is a
pure-function module (stdlib + typing only — no numpy, no DB access, no imports from the rest of
`sleepctl`) that computes one number (a recommended time-in-bed / bedtime) and shows its work in
plain language, exactly like a clinician's reasoning would read, so the user (an anaesthesia
trainee and a clinician himself) can evaluate and override it. As of this writing `CBTIConfig` is
**not wired into `sleepctl/config.py`** — the dashboard calls `recommend_sleep_window` directly with
a default `CBTIConfig()`; the module is deliberately self-contained so it can be adopted by the
engine later without churn.

## Shift-safety is load-bearing, not cosmetic

The user works rotating shifts and on-call nights. Classic sleep restriction *increases* daytime
sleepiness during the titration phase — that's the mechanism, it raises sleep pressure. Deliberately
inducing sleepiness in someone about to administer anaesthesia is not a tradeoff this module is
allowed to make quietly. Two independent refusals guard against it:

- **`upcoming_high_stakes=True`** (the caller flags an upcoming on-call/night-shift duty, derived
  from the configured shift plan — see `SHORT_SLEEP.md`/`SLEEP_DEPRIVATION.md`): compression is
  withheld outright and the module holds the current time-in-bed, with a safety note explaining why.
- **Already sleep-deprived guard.** If the last few eligible nights averaged under
  `severe_short_sleep_min` (default 300 min / 5.0 h) of total sleep, compression is refused even if
  efficiency looks poor — the problem there is insufficient sleep *opportunity*, not excess
  time-in-bed, and restricting further would compound the deficit rather than treat it.

Both guards fire regardless of what the efficiency math alone would recommend.

## How the recommendation is computed

- **Sleep-efficiency band drives compress/hold/expand.** `efficiency = total_sleep_min /
  time_in_bed_min` over a rolling window (default 14 nights, `min_nights_required = 7` before
  advising anything other than a low-confidence hold — CBT-I titration is conventionally judged on
  ~1–2 weeks of sleep-diary data, not one night). Below `compress_below_efficiency` (0.85) →
  compress; above `expand_above_efficiency` (0.90) → expand; in between → hold.
- **One step at a time.** Adjustments move by `step_min` (default 15 min), matching standard
  CBT-I protocols' 15–20 min weekly increments — no overshoot.
- **A hard floor.** `min_time_in_bed_min` (default 330 min / 5.5 h) is the conventional CBT-I
  minimum: below this, further restriction has diminishing therapeutic return and materially raises
  next-day impairment risk. No compression is ever recommended below it, and the floor is enforced
  as a final defensive clamp regardless of which branch computed the new value.
- **Ineligible nights are excluded from the estimate.** Naps and deliberately short/work nights
  (`night_type` in `{nap, short_night, work_night, ...}`) don't count toward the rolling
  efficiency/sleep-time estimate — they aren't representative of the user's normal sleep ability and
  would bias it.
- **Compression leaves a buffer.** The new time-in-bed target is `max(floor, mean_total_sleep +
  buffer_min)` (buffer default 15 min) — not literally equal to measured sleep time, leaving room to
  fall asleep and for night-to-night variance.
- **A recommended bedtime**, if `required_wake_time` is supplied, by subtracting the recommended
  time-in-bed from the fixed wake time.

## Stimulus-control tips

`stimulus_control_tips()` returns *only* the specific, evidence-based tips the recent data actually
supports — never generic sleep-hygiene filler:

- **Long or frequent awakenings → "get out of bed."** If duration data is available and ≥30% of
  logged awakenings run ≥20 min, or (when only counts are available) awakenings average ≥2/night,
  the classic stimulus-control instruction is surfaced: get up after ~20 min of being awake in bed
  rather than lying there, to keep the bed associated with sleeping.
- **Variable bedtime → anchor a consistent wake time.** If logged bedtimes have a standard deviation
  ≥45 min over the window, the tip to anchor a fixed wake (and as fixed a bedtime as the schedule
  allows) is surfaced.

## Surfacing

`GET /cbti/advice` returns the `SleepWindowAdvice` (direction, recommended time-in-bed, rationale,
confidence, safety notes, recommended bedtime) plus the stimulus-control tips. Shown on the
dashboard's Tonight page (`CBTIAdviceCard`) — read-only, advisory, exactly as designed.

## Related docs

- `SHORT_SLEEP.md`, `SLEEP_DEPRIVATION.md` — the shift-aware planning this module's
  `upcoming_high_stakes` guard reads from.
- `AWAKENING_PREVENTION.md`, `CONTROL_LAW.md` — the thermal side of the same "staying asleep"
  problem this module addresses behaviorally instead.
