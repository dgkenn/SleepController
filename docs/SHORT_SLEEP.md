# Optimizing for Chronically Short Sleep (Early-Wake Regime) — Design & Evidence

**The user's dominant regime:** mostly day shifts with long hours and **very early wakes**, so
**total sleep time is chronically short** — not the occasional night shift. The whole system
should therefore treat "short sleep, waking near the core-temperature trough" as the *default*
and optimize every feature for **recovery and function per limited hour**, not for a full 8 h
night. This documents the audit of how each feature already handled short sleep, the science, and
the gaps this pass closed.

All findings are from **PubMed**; DOIs are linked inline.

## The science of short sleep

- **The body defends deep sleep and sacrifices REM under restriction.** In a 5 h restriction RCT,
  restricted nights had *less* light and REM sleep but proportionally *more* stage-3 deep sleep,
  and a strong multi-day need for recovery (Laharnar et al. 2019,
  [DOI](https://doi.org/10.1016/j.physbeh.2019.112794)). → On a short night, *help the body do what
  it's already doing*: maximize efficient deep sleep (cool bias) and accept the REM loss rather
  than chase it.
- **Recovery spans multiple nights**, and chronic restriction produces dose-dependent deficits a
  single long night doesn't repay (Van Dongen et al. 2003,
  [DOI](https://doi.org/10.1093/sleep/26.2.117)).
- **The only structural fix for a fixed early wake is an earlier bedtime.** If wake is pinned at
  04:30, sleep duration = bedtime → wake, minus onset latency and awakenings. Bedtime is the one
  lever the user controls.
- **A short power nap recovers alertness** without the SWS-inertia trap; relaxation/wind-down even
  boosts restorative slow-wave sleep in a nap (Simon & Mednick 2022,
  [DOI](https://doi.org/10.1111/jsr.13574)).
- **Early wakes maximize inertia** (waking near the core-temp trough; see `WAKE_SCIENCE.md`,
  Tassi & Muzet 2000).

## What the audit found already in place (good)

The controller was already strong at single-night short handling, and most of it is
**auto-detected from the wake time** (not just reactively from logged sleep):

- **Auto night-mode** (`sleep_plan.decide_mode`): sleep opportunity < ~6.5 h → `CONSTRAINED` →
  `DAMAGE_CONTROL` objective.
- **DAMAGE_CONTROL targets** (`benchmarks.targets_for`): de-weight *duration*, prioritize
  efficiency + fast onset + minimal awakenings + early deep; calmer thermal swings.
- **Shorter, more aggressive induction** on short nights (`induction.py`, 15 vs 30 min).
- **Narrowed, debt-adaptive smart-wake window** (`wake_orchestrator.choose_wake_window` +
  debt shrink) — protect every minute of a short night.
- **Rolling sleep debt** (`benchmarks.sleep_debt_min`) feeding recovery targets and the wake window.

## The gaps this pass closed

### 1. Inverse bedtime calculator — **new** (the headline lever)

Nothing computed *when to go to bed*. For a fixed early wake that's the entire ballgame.
`sleep_plan.bedtime_guidance()` now inverts the wake time: **be asleep by X (in bed by Y)** to
bank your need, and — using your habitual bedtime from recent nights — how much sleep you actually
get at your usual bedtime, the **structural shortfall**, and **how much earlier to turn in**.

> Example: 04:30 wake, 8 h need, habitual 23:00 → *"Be asleep by 20:30. At your usual 23:00 you
> only get ~5.3 h — about 2.7 h short. Moving lights-out ~162 min earlier is the highest-leverage
> fix."* Surfaced on the Tonight's-Plan card.

### 2. Chronic-shortfall awareness — **new**

Debt was tracked, but nothing distinguished *acute* debt (one bad night) from a *structural*
deficit (short every single day). `benchmarks.chronic_shortfall()` reports the trailing-average
total sleep, mean nightly shortfall, fraction of short nights, and an `is_chronic` flag (averaging
> ~1 h under need across enough nights). It powers the bedtime message, the catch-up nap, and the
gym lean.

### 3. Proactive catch-up nap — **new**

The shift planner only prescribed naps around shifts. Now, when you're **chronically short with no
shift in play**, it recommends a **20-min power nap** (early-mid afternoon, before ~16:00) —
recovers alertness without the SWS-inertia trap, and timed so it doesn't erode that night. Shown
in the Shift & Sleep-Debt card.

### 4. Gym advisor weighs the chronic pattern — **new**

The gym call factored this night's debt + projection but not the *chronic* pattern. A small
`chronic_short` term now nudges toward protecting sleep when you're structurally short — kept
gentle (weight 0.4) so it never vetoes the now-or-never workout on its own, consistent with your
"lean gym" preference.

## Deliberate non-changes

- **Sleep need stays 8 h (480 min) by default.** Making it user-configurable/learned is a sensible
  follow-up, but the guideline default is a safe anchor and the *shortfall* framing is what
  matters here.
- **We don't fight the REM loss** on short nights — the body protects deep sleep and sheds REM
  under restriction, so the controller leans into efficient deep sleep rather than chasing REM
  warmth on a short night (already the CONSTRAINED behavior).
- **No alarm/duration heroics** — the wake window is *narrowed* on short nights to protect sleep,
  not widened to chase a zero-inertia wake.

## Where the changes live

- `sleepctl/benchmarks.py` — `chronic_shortfall()`.
- `sleepctl/controller/sleep_plan.py` — `bedtime_guidance()`, `median_bedtime_clock()`,
  `BedtimeGuidance`, wired into `plan_night`/`SleepPlan`.
- `sleepctl/shift_manager.py` — chronic-short catch-up nap.
- `sleepctl/gym_advisor.py` — `chronic_short` term.
- `dashboard/web/components/SleepPlanCard.tsx` + `lib/api.ts` — bedtime surfacing.
