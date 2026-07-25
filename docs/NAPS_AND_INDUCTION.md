# Naps and on-demand induction

`sleepctl/controller/nap.py` (strategy selection) + `sleepctl/controller/induction.py` (the
warm→cool onset cascade) + the daemon's `_start_nap`/`_start_induce` (`dashboard/daemon/run_daemon.py`,
mirrored in `live_daemon.py`). Covers both the "help me fall asleep" button and nap sessions.

## Nap strategy selection (shipped)

Naps live or die by duration because of sleep inertia (Brooks & Lack 2006,
[doi:10.1093/sleep/29.6.831](https://doi.org/10.1093/sleep/29.6.831); a ~10-min nap was the most
recuperative, a 30-min nap caused inertia because it reaches slow-wave sleep and wakes the napper
out of it). `nap_strategy(window_min, now_hour, cfg)` picks one of three strategies from the
available window:

| Strategy | Window | Behavior |
|---|---|---|
| **POWER** | ≤ `nap_power_max_min` (25 min) | Stay light (`keep_light=True` → the maintenance routine refuses `DEEP_BIAS_COOL` and holds `STABILIZE` instead — see `test_power_nap_keeps_light_no_deep_cooling`), hard-cap the wake. Minimal grogginess. |
| **CYCLE** | ≥ `nap_cycle_min_min` (60 min) | Allow one full NREM–REM cycle; target ~`nap_cycle_target_min` (90 min); smart-wake catches a light-sleep moment near the target rather than a hard cutoff. |
| **TRAP** | 25–60 min | The inertia danger zone — wakes you out of deep sleep, worst grogginess. Advises shortening to ~20 or extending to ~90; if the window is forced, keeps the bed light and caps at the power-nap duration. |

Late-day naps (`now_hour >= nap_late_hour`, default 16) are flagged with a heads-up that napping
this late can make it harder to fall asleep that night. After a CYCLE (or forced TRAP) nap, an
`inertia_buffer_min` (default 20 min) is advised before anything safety-critical.

## The on-demand induction cascade (shipped)

`InductionRoutine.step()` drives a **warm → cool** cascade timed from when induction actually
starts, not from time-in-bed generally:

1. **Warm-pulse opener** (`ONSET_WARM`, for `induction_warm_pulse_min`, halved on a short
   `DAMAGE_CONTROL` night): cutaneous warming measurably speeds sleep onset (Raymann/Van Someren) —
   this is a deliberate choice; the cascade **never opens cold** (`test_induction_never_opens_cold`
   pins this as a regression guard — an early revision of the design opened with a cold blast, which
   fights the physiology it's trying to invoke).
2. **Consolidating cool** (`INDUCTION_COOL`) once the warm pulse window elapses.

"Help me fall asleep" and both nap modes **force** this cascade to start immediately
(`SleepController.set_session`): they jump the state machine straight from `IDLE`/`CALIBRATION` into
`INDUCTION` rather than waiting for the Eight Sleep cloud's `presence` flag, because Pod presence is
derived from an already-open sleep *session*, which only exists retroactively after onset — a
presence-gated induction would never fire pre-sleep. Every press (including a repeat press mid-cascade)
**restarts the cascade clock**, so cold-settle/warm-pulse timing is always relative to *this* request.

## How a nap session is scheduled today (button-press-anchored)

`_start_nap(duration_min, wake_time)` computes the nap's deadline **at the moment the user presses
the button**: either `now + duration_min`, or a fixed wall-clock `wake_time`. `nap_strategy` is then
evaluated against that window, and `required_wake_time` is set to the deadline so smart-wake can
still catch a light-sleep moment inside it. This means a "20-minute nap" today means **20 minutes
from the button press** — which includes however long it actually takes to fall asleep, not 20
minutes of measured sleep. On a night with a slow onset, a POWER nap can end with only a few minutes
of real sleep; on a fast onset, a CYCLE nap's target can land in the wrong part of the sleep cycle
relative to when sleep actually started.

`tests/test_onset_nap.py` currently exercises the induction cascade's warm→cool phase logic and the
`nap_strategy` selection function in isolation (duration → POWER/CYCLE/TRAP), but does **not** yet
exercise anchoring the nap deadline to a *measured* onset — confirmed by reading `_start_nap`, which
computes `deadline` purely from wall-clock inputs, with no reference to
`SleepController._sleep_onset_time` (the accurately back-dated onset time the maintenance/architecture
code already tracks — see `sleepctl/controller/sleep_onset.py`).

## Planned (not yet implemented as of this writing) — onset-anchored nap dosing

The intended design, described here so it isn't lost, and to be verified against the code before
treating it as shipped:

- **Anchor the nap "dose" to measured sleep onset, not button-press time.** A "20-minute nap" should
  mean 20 minutes of *sleep*, using the same accurately back-dated onset event
  (`SleepOnsetDetector`/`_sleep_onset_time`) the rest of the controller already relies on for sleep
  architecture accounting.
- **An onset-grace cap.** Bound how long the system waits for onset to actually occur before giving
  up and falling back to something like today's button-press-anchored behavior — a nap opportunity is
  finite, so open-ended waiting for onset isn't safe either.
- **Re-plan the strategy once onset is known.** `nap_strategy`'s POWER/CYCLE/TRAP choice is a function
  of the *available* window; once real onset time (and therefore the real remaining window) is known,
  the strategy should be re-evaluated rather than locked in at button-press time — e.g. a nap
  requested as CYCLE-length that took 15 minutes to fall asleep may need to re-target POWER to avoid
  landing in the trap zone before the deadline.
- **Deliberately avoid a realized sleep window landing in the ~25–60 min trap zone.** This is the
  existing trap-zone logic in `nap_strategy`, but applied against the *realized* (onset-anchored)
  sleep duration rather than only the requested window — the goal is to never let re-planning
  accidentally produce a nap that wakes the user out of slow-wave sleep.

If, by the time this doc is read, the code implements some but not all of the above, treat the
"shipped" sections above as authoritative and this section as the remaining gap — don't assume this
list describes current behavior.

## Related docs

- `SLEEP_DEPRIVATION.md` / `SHORT_SLEEP.md` — the prophylactic and catch-up naps the shift planner
  schedules around this same `nap_strategy` engine.
- `WAKE_SCIENCE.md` — the smart-wake mechanics (light-sleep targeting, dawn light, vibration rhythm)
  that both nap modes and the post-induction night use to actually wake the user.
