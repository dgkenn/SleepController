# `sleepctl doctor` — data + learning + config health check

`sleepctl doctor` answers one question: **is the DATA and ML side of sleepctl healthy?**

It is deliberately the *data-side* complement of a live-runtime doctor (device/daemon/
telemetry health). It never touches a daemon or a live device — it only reads the SQLite
dataset (`Repository`) and the static `AppConfig`, via the pure engine module
`sleepctl.diagnostics.data_diagnostics(repo, cfg)`.

## Usage

```bash
python -m sleepctl.cli doctor --db sleepctl.db
python -m sleepctl.cli doctor --db sleepctl.db --json
```

- `--db PATH` — path to the sleepctl SQLite database (default `sleepctl.db`).
- `--json` — emit the full structured report instead of the human-readable text report.

Exit code is `1` when the overall verdict is `DEGRADED`, `0` otherwise (`HEALTHY` or
`NEEDS_DATA` are not treated as failures — they're informational).

## What it checks

| id | title | what it looks at |
|---|---|---|
| `db` | Database schema | key tables (`nightly_summaries`, `raw_samples`, `decisions`, `actions`) exist and are queryable; reports row counts |
| `data_volume` | Data volume | how many nights are logged; flags `info` when there are too few nights for the report (or the ML gate, `ml.min_nights`) to mean much |
| `data_completeness` | Data completeness | whether recent nights have `wake_events` / `deep_min` / `sleep_efficiency` / `avg_hrv` populated; flags a stale gap if no night has logged in >2 days |
| `learner_maturity` | Learner maturity | reuses `sleepctl.learning.coordinator.learning_ledger` to summarize how many learners are still on `preset` vs `learned`/`measured`, and which are data-starved |
| `calibration` | Personal calibration | whether the thermal calibration / comfort profile / resting baseline (from the in-bed self-test) have been measured |
| `setpoints_sane` | Setpoint sanity | the latest learned `SetpointProfile` is within the valid knob bounds (`sleepctl.ml.actions.KNOB_BOUNDS`); `fail`s if out of range |
| `config_sane` | Config sanity | key tunables (`max_step_f`, `wake_window_min`, temperature bounds, level bounds) are within physically sane ranges |
| `outcome_trend` | Outcome trend | whether `outcome_score` / `wake_events` are trending better, flat, or worse over recent nights (informational) |

Every check is individually sandboxed: a missing table, an empty database, or an unexpected
schema degrades that one check to an `"info"` result with an explanatory detail — it never
raises and never sinks the rest of the report. This makes it safe to run against a
brand-new, even empty, database.

Each check reports one of four statuses: `ok`, `warn`, `fail`, `info`. The overall
**verdict** is:

- `NEEDS_DATA` — fewer than a handful of nights logged (and nothing has actively failed).
- `DEGRADED` — one or more checks are `fail` or `warn`.
- `HEALTHY` — enough nights logged and every check passing.

## Live-runtime section

If a dashboard API is running locally, `doctor` also makes a best-effort
`GET http://localhost:8000/diag?token=$DIAG_TOKEN&format=json` (stdlib `urllib`, ~1.5s
timeout) and prints its live-health verdict under a `LIVE RUNTIME` heading first. If the
token is missing or the endpoint isn't reachable, that section just notes
`(dashboard API not reachable — data checks only)` and the rest of the report (the data/
learning checks below) still runs normally — `doctor` never depends on a live server.

## Example

```
== sleepctl doctor ==

LIVE RUNTIME
  (dashboard API not reachable — data checks only (URLError))

Data/learning health: DEGRADED — warning(s): Data completeness, Learner maturity

[OK]   Database schema — all key tables present and queryable (nightly_summaries=3, raw_samples=1260, decisions=1260, actions=3)
[INFO] Data volume — 3 night(s) logged; the ML gate needs >= 14 clean nights before it acts (config ml.min_nights=14).  (fix: keep logging nights — 11 more before ML engages (rule-based policy runs meanwhile))
[WARN] Data completeness — stale — no night logged in 7 days (last: 2026-06-25)  (fix: check the sensor feed / adapter — gaps or a stale gap starve the nightly learners of usable rows)
[WARN] Learner maturity — 9 learner(s) tracked: 6 preset, 1 learned, 1 measured. Data-starved (still preset, low maturity): onset.warm_nudge, maintenance.settle_nudge, wake.ramp_temp, wake.window_min, wake.thermal_wake, architecture.deepening_enabled.  (fix: most learners need roughly 12-30 nights of history to move off their preset defaults — keep logging nights)
[INFO] Personal calibration — missing measured calibration: thermal_calibration, comfort_profile, resting_baseline  (fix: run the on-bed self-test / comfort sweep (see scripts/in_bed_calibration.py) to measure these directly instead of relying on config defaults)
[OK]   Setpoint sanity — setpoint v2 (source=policy) is within the valid knob bounds.
[OK]   Config sanity — max_step_f=2.0 wake_window_min=30 neutral=70.0F deep_bias=66.0F wake_ramp=74.0F — all within sane ranges.
[INFO] Outcome trend — only 3 scored night(s) recently — too few to trend.
```
