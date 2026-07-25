# sleepctl

A personalized, closed-loop **sleep-optimization controller for the Eight Sleep Pod 2**.
It drives bed temperature from the Pod's own sensors plus Google Calendar context, follows
a **Sense → Decide → Act → Learn** loop, and improves its policy over days and weeks.

Built for a hot sleeper whose main problem is **staying asleep**: awakenings are treated
as the top-priority error signal, thermal changes are small and gradual, and the whole
system is conservative and explainable.

> ⚠️ **Not a medical device.** `sleepctl` is a comfort/automation tool. It does not
> diagnose, treat, or give medical advice. It avoids risky interventions and is
> deliberately conservative. Use at your own discretion.

See **[DESIGN.md](DESIGN.md)** for the full architecture, state machine, control rules,
learning algorithm, data schema, and failure-mode analysis, and **[docs/README.md](docs/README.md)**
for the full documentation index (control law, sensing/hardware, ML, operations, research findings —
grouped and one-line-described so you can find the right doc in seconds).

## How it controls / reads the Pod (and the no-brick promise)

There is no official Eight Sleep API, so device access is staged across data tiers behind
one common interface. A hard requirement drives this: **no chance of bricking the device**
unless a step is 100% reversible, necessary, and minimal.

- **Tier 0 — cloud `intervals`** (`EightSleepCloudSource`, via the `pyEight` OAuth2
  library): minute-level HR/HRV/breath/movement/stage and `-100..100` temperature control.
  **Zero device contact, cannot brick anything.** Physiology (HR/HRV/breath/stage) requires an
  active Autopilot membership; without it, use phone-BCG sensor path for movement/HR instead.
- **Tier 1 — non-invasive raw capture** (`RawCaptureSource`): redirect the Pod's own raw
  upload (`raw-api-upload.8slp.net`) to a local capture server. **No device modification,
  fully reversible.** Go/no-go = TLS cert pinning. See
  [`sleepctl/recon/mitm_probe.md`](sleepctl/recon/mitm_probe.md).
- **Tier 2 — on-device root** (`LocalFrankSource`, gated stub): highest fidelity, but a
  **last resort, triple-gated** on necessity → *proven* reversibility (full byte-for-byte
  microSD image + verified restore-to-stock before any change) → minimality. See
  [`sleepctl/recon/pod2_teardown.md`](sleepctl/recon/pod2_teardown.md).

The controller consumes whichever tier is active without any code change.

**Independent of all three tiers: a Polar Verity Sense wearable.** A separate, zero-Pod-risk sensing
path streams heart rate, beat-to-beat RR intervals (→ HRV), and the armband's own accelerometer
(→ actigraphy) over Polar's PMD Bluetooth LE service, and can *drive the controller entirely on its
own* — a `state_estimator` overlay derives a coarse sleep stage from the wearable when the Pod's own
stage is unavailable (no Autopilot membership, sensors not reporting), so onset detection, arousal
detection, and wake-risk pre-emption all still work off the wearable alone. See
**[docs/VERITY_RESEARCH.md](docs/VERITY_RESEARCH.md)** and **[deploy/VERITY_SENSOR.md](deploy/VERITY_SENSOR.md)**.

## Install

```bash
pip install -e .            # core (PyYAML only)
pip install -e ".[eightsleep]"   # + pyEight for live Pod control
pip install -e ".[google]"       # + Google Calendar client
pip install -e ".[dev]"          # + pytest
```

Requires Python ≥ 3.11.

## Quickstart (no hardware)

Run synthetic nights through the full controller loop and write the dataset:

```bash
python -m sleepctl.cli replay                 # in-memory DB, prints per-night summary
python -m sleepctl.cli replay --db sleep.db   # persist the dataset
python -m sleepctl.cli report --db sleep.db   # show baselines + recent nights

# drive the live DAEMON offline against the simulator (no device, no creds):
python -m sleepctl.cli run --simulate --wake 07:00
```

Run the tests:

```bash
pytest tests/
```

## Live run (real Pod 2)

The live daemon talks to the Pod through the async
[`pyEight`](https://github.com/lukas-clarke/pyEight) OAuth2 library. That fork is **not
pip-installable** (it ships no `setup.py`), so install its deps via the extra and put the
`pyeight` package on your `PYTHONPATH`:

```bash
pip install -e ".[eightsleep]"                         # aiohttp, httpx, python-dateutil
git clone https://github.com/lukas-clarke/pyEight.git  # the async OAuth2 fork
export PYTHONPATH="$PWD/pyEight:$PYTHONPATH"            # makes `import pyeight` work
```

Then, **with your Pod 2 (start here the first time you connect a unit):**

```bash
# 1. store credentials once (~/.config/sleepctl/credentials.json, mode 0600; or set
#    EIGHTSLEEP_EMAIL / EIGHTSLEEP_PASSWORD / EIGHTSLEEP_TIMEZONE / EIGHTSLEEP_SIDE)
python -m sleepctl.cli auth --test

# 2. PROBE your specific Pod 2 (read-only): does it cool? which fields/commands work?
python -m sleepctl.cli calibrate

# 3. dry-run a night: reads sensors + logs every decision, sends NO commands
python -m sleepctl.cli run --dry-run --wake 07:00

# 4. go live: the daemon controls bed temperature in a closed loop
python -m sleepctl.cli run --wake 07:00
```

**Pod 2 notes.** The lukas-clarke fork is validated mostly on Pod 3/4, so run `calibrate`
first on a Pod 2: it reports whether the device advertises **active cooling** (a hot
sleeper needs this), and exactly which biometric fields and control commands your unit
exposes — warning on anything missing. The adapter reads every field defensively, so a
Pod 2 that reports fewer fields degrades gracefully instead of crashing (verified by the
`pyeight` integration test).

Safety: thermal changes are slew- and variability-limited (≤2 °F/step), stale data is held
on, `--dry-run` is always available for a read-only shakedown, and `calibrate` never writes.
`--wake HH:MM` supplies the required wake time (v1); Google Calendar is scaffolded for later.

**Composite (effective) temperature.** Comfort is controlled as a *blend*: your **covered
body** feels the Pod's bed-surface temperature, while your **exposed skin** (head/face) feels
the room air. The controller targets an effective temperature
`composite = a·bed + (1−a)·ambient` and runs a gentle feedback loop that nudges the Pod's
water temperature until the blend hits target — so a **cold room makes the bed run warmer**
to compensate (and vice-versa), and it **self-calibrates** to how much your body heats the
bed. Exposed-skin ambient comes from the Pod's **bedroom** sensor, with **outdoor weather**
(free [Open-Meteo](https://open-meteo.com), no API key, default **Boston, MA**) only as a
*fallback* when the Pod reports no room temp. Tune the blend with `composite_bed_weight`;
override location with `--lat/--lon`; disable the weather fallback with `--no-weather`.

## CLI subcommands

| Command     | What it does |
|-------------|--------------|
| `replay`    | Drive synthetic nights (normal / short_sleep / clustered_awakenings) through the controller offline |
| `report`    | Print rolling baselines + recent nightly summaries from the dataset |
| `night-report` | Explainable nightly intelligence report (what happened, why, what was learned); `--json` |
| `run`       | Live closed-loop daemon. Flags: `--dry-run`, `--wake HH:MM`, `--poll-seconds`, `--side`, `--simulate`, `--max-ticks`, `--db` |
| `auth`      | Store Eight Sleep credentials (0600 file or env vars); `--test` verifies the connection |
| `calibrate` | Read-only probe of the live Pod (capabilities, current level, bed/room temp, biometrics) |
| `export`    | Dump the ML-ready joined feature table (`--format csv|parquet`) |
| `train`     | Refit the ML models and propose (or `--apply`) the next setpoint |
| `checkin`   | Log subjective morning data (`--quality/--grogginess/--performance`, 0–10) |
| `recalibrate` | Monthly re-anchor + ML status report |
| `backtest`  | Prove the closed loop beats no-control on a response-aware model (`--nights`, `--scenario`, `--seed`) |
| `doctor`    | Data + learning + config health check — `[OK]/[WARN]/[FAIL]/[INFO]` report, `--json`; see **[docs/DIAGNOSTICS_CLI.md](docs/DIAGNOSTICS_CLI.md)** |
| `repair`    | Safe, idempotent self-repair actions (stuck commands, stale daemon heartbeat, stuck device, stale watchdog alert); `--json` |
| `backup`    | Consistent rotating DB backup, safe while a daemon/API has the file open (`--keep`) |

## Self-learning (ML)

The controller tailors itself over time on **two timescales**, all interpretable and pure-Python
(`pip install -e ".[ml]"` only adds numpy/pandas for speed + parquet).

**Slow loop — the nightly setpoint learner.** An action-value learner scores each night with a
**maintenance-dominant reward** and, once it has enough clean nights and confidence, auto-applies
the **smallest effective** setpoint change (cooling, REM warmth, or the body-vs-skin blend weight),
else falls back to the conservative rule policy ("do no harm"). Confounded nights (illness, travel,
alcohol, short-sleep) are excluded; rewards are attributed across the nights an action actually
produced; every change stays within slew/variability/55–110 °F limits.

**What "perfect" means for you, per night.** `plan_night` designs tonight's ideal architecture from
*all* the inputs — wake time / sleep opportunity (the **mode**), sleep **debt**, your **learned**
felt-recovery deep/REM levels (from the morning survey), and **stress** (which defends deep) —
bounded to the evidence prior. The dashboard plan, the score, the policy, and the in-night steerer
all chase the same numbers.

**Per-phase learners**, each constraint-aware (normal / short / recovery) and self-exploring:
- **Onset** — the warm nudge that gets *you* to sleep fastest (from measured latency).
- **Maintenance** — the settle-nudge sign + pre-cool **lead-times** (from prevention outcomes), and
  a **personalized awakening-precursor** model that learns the sensor trajectory before *your*
  awakenings (HR/HRV drift, breathing irregularity, **tossing/turning bursts**, bed warming) to
  pre-empt earlier and more accurately.
- **Wake** — the smart-alarm window + lift bar + thermal wake ramp (from morning grogginess).
- **In-night steering** — a favorable-state controller that **acquires** a deeper state when you're
  behind the ideal curve and **defends** the deep/REM state you're in, reconciled with
  wake-prevention and the wake-up ramp by a strict precedence. Whether cool-to-deepen actually works
  for you is learned by a **causal n-of-1 A/B** (randomized control nights) with a do-no-harm gate
  that disables it if it doesn't beat your natural base rate or ever raises your awakening rate. A
  **wake-causation audit** checks every adjustment against the base wake rate (netting out "you'd
  have woken anyway") and never blames a confounded reactive maneuver.

**Personal n-of-1 thermal dose-response trial (disabled by default).** Separate from the learners
above: a randomized, block-balanced trial of the maintenance-temperature offset itself — including
*warming* arms, motivated by a published result that mild warming can suppress awakenings in some
sleepers — to find *this* user's personal optimum rather than assume the population hot-sleeper
default. Opt-in because it changes what the bed does overnight. See
**[docs/THERMAL_DOSE_RESPONSE.md](docs/THERMAL_DOSE_RESPONSE.md)**.

**Advisory-only CBT-I sleep-window guidance.** A side feature that never touches control: sleep
restriction / stimulus-control recommendations, shift-aware (refuses to recommend restriction before
an on-call night or when already sleep-deprived). See **[docs/CBTI.md](docs/CBTI.md)**.

Typical cadence: `train` weekly, `recalibrate` monthly, `checkin` each morning. See
**[DESIGN.md](DESIGN.md)**, **[docs/CONTROL_LAW.md](docs/CONTROL_LAW.md)**,
**[docs/SELF_LEARNING.md](docs/SELF_LEARNING.md)**, and
**[docs/ARCHITECTURE_STEERING.md](docs/ARCHITECTURE_STEERING.md)**.

## Dashboard (iPhone-first PWA)

A self-hosted FastAPI + Next.js dashboard (`dashboard/`) is the command center: live status over
SSE, Tonight controls (temp/mode/wake/Emergency-Stop), **Help-me-fall-asleep** + **Nap** sessions,
analytics, and a Learning page showing every phase converge (now including the thermal dose-response
trial and CBT-I advice). A decoupled control **daemon** owns the device and exchanges a command queue
+ runtime snapshot with the API (race-free; the daemon's safety net always wraps overrides). A
token-gated `/diag*` HTTP surface lets a remote operator (including a fresh Claude session with no
repo checkout) diagnose and self-repair a live deployment — see
**[docs/CLAUDE_REMOTE_OPS.md](docs/CLAUDE_REMOTE_OPS.md)**. See **[docs/DASHBOARD.md](docs/DASHBOARD.md)**
(the original design doc — some newer endpoints aren't covered there, see the note at its top) and
**[deploy/](deploy/)** (Docker, Windows home server, Oracle Cloud, Codespaces).

## Project layout

```
sleepctl/
  models.py            # shared dataclasses + enums (the frozen contract)
  config.py            # UserProfile / Benchmarks / Tunables / AppConfig, CONTROL_PRIORITY
  benchmarks.py        # literature targets + the per-mode "perfect sleep" index, debt/shortfall
  adapters/            # base ABCs + eightsleep_cloud (Tier 0), calendar (Google), simulator,
                       #   bcg/wearable (phone fusion), hue (dawn light), weather, credentials,
                       #   raw_capture (Tier 1), local_frank (Tier 2 gated)
  controller/          # state_machine, controller, thermal, wake_detection, induction,
                       #   maintenance, smart_wake + wake_orchestrator, sleep_onset, arousal,
                       #   precursor (predictive pre-emption), wake_risk, sleep_plan (per-night
                       #   ideal), architecture (in-night "nudge me deeper" steering), nap
  learning/            # baselines, policy (tiered), response curves, perfect_weights +
                       #   ideal_architecture (learned levels), onset_tuning, settle, thermal_wake,
                       #   wake_tuning, lead_time, deepening (causal A/B), wake_causation (audit +
                       #   personalized awakening precursors)
  ml/                  # action-value learner: features, model (ridge), reward, select, recommend,
                       #   confounders, phenotype, preference, wake_profile, efficacy_trial +
                       #   thermal_trial (n-of-1 randomized trials), sleep_staging/ (wearable stager)
  storage/             # SQLite schema (3 layers + ledgers, incl. steer_events) + repository
  loop/                # runtime (tick/replay), cycle (shared decide/log), live, nightly (learn)
  recon/               # non-invasive network spike + gated teardown reference
  cbti.py, shift_manager.py, gym_advisor.py, night_report.py  # advisory-only side features
  cli.py
dashboard/             # FastAPI api/ + Next.js web/ (iPhone PWA) + control daemon/
deploy/                # Docker compose, Windows home server, Oracle Cloud, Codespaces, live bring-up,
                       #   VERITY_SENSOR.md (wearable setup)
docs/                  # see docs/README.md for the full index (control law, sensing/hardware, ML,
                       #   operations, research findings)
scripts/               # verify_live_pod.py (round-trip device verifier), in_bed_calibration, setup,
                       #   polar_pmd.py + verity_forwarder.py (Verity Sense codec/forwarder)
tests/                 # unit + end-to-end tests (engine + dashboard API)
DESIGN.md              # full design spec
```

## Status

The full system is implemented and tested (`pytest`: **943 engine + 678 API** green, verified
2026-07-25): the controller, the two-timescale learning loop (nightly setpoint learner + per-phase +
in-night steering with causal A/B), the wearable sensing path (Polar Verity Sense), the n-of-1
thermal dose-response trial, CBT-I advisory guidance, storage, simulator, offline runtime, **live
device wiring** (`run`/`auth`/`calibrate` + the async live dashboard daemon), and the **iPhone
dashboard**. A round-trip verifier (`scripts/verify_live_pod.py`) drives every UI control and
confirms the bed responded by reading the Pod's own state back from the cloud (validated end-to-end
on the simulator: 0 failed). The Tier 1/2 raw-data paths remain scaffolded behind the common adapter
interface (Tier 0 cloud is the live path).
