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
learning algorithm, data schema, and failure-mode analysis.

## How it controls / reads the Pod (and the no-brick promise)

There is no official Eight Sleep API, so device access is staged across data tiers behind
one common interface. A hard requirement drives this: **no chance of bricking the device**
unless a step is 100% reversible, necessary, and minimal.

- **Tier 0 — cloud `intervals`** (`EightSleepCloudSource`, via the `pyEight` OAuth2
  library): minute-level HR/HRV/breath/movement/stage and `-100..100` temperature control.
  Always available, **zero device contact, cannot brick anything.** Ships today.
- **Tier 1 — non-invasive raw capture** (`RawCaptureSource`): redirect the Pod's own raw
  upload (`raw-api-upload.8slp.net`) to a local capture server. **No device modification,
  fully reversible.** Go/no-go = TLS cert pinning. See
  [`sleepctl/recon/mitm_probe.md`](sleepctl/recon/mitm_probe.md).
- **Tier 2 — on-device root** (`LocalFrankSource`, gated stub): highest fidelity, but a
  **last resort, triple-gated** on necessity → *proven* reversibility (full byte-for-byte
  microSD image + verified restore-to-stock before any change) → minimality. See
  [`sleepctl/recon/pod2_teardown.md`](sleepctl/recon/pod2_teardown.md).

The controller consumes whichever tier is active without any code change.

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

The live daemon talks to the Pod through the async [`pyEight`](https://github.com/lukas-clarke/pyEight)
OAuth2 library (`pip install -e ".[eightsleep]"`).

```bash
# 1. store credentials once (written to ~/.config/sleepctl/credentials.json, mode 0600;
#    or set EIGHTSLEEP_EMAIL / EIGHTSLEEP_PASSWORD / EIGHTSLEEP_TIMEZONE / EIGHTSLEEP_SIDE)
python -m sleepctl.cli auth --test

# 2. read-only probe of the device (no commands sent)
python -m sleepctl.cli calibrate

# 3. dry-run for a night: reads sensors + logs every decision, sends NO temperature commands
python -m sleepctl.cli run --dry-run --wake 07:00

# 4. go live: the daemon controls bed temperature in a closed loop
python -m sleepctl.cli run --wake 07:00
```

Safety: thermal changes are slew- and variability-limited (≤2 °F/step), stale data is held
on, `--dry-run` is always available for a read-only shakedown, and `calibrate` never writes.
`--wake HH:MM` supplies the required wake time (v1); Google Calendar is scaffolded for later.

## CLI subcommands

| Command     | What it does |
|-------------|--------------|
| `replay`    | Drive synthetic nights (normal / short_sleep / clustered_awakenings) through the controller offline |
| `report`    | Print rolling baselines + recent nightly summaries from the dataset |
| `run`       | Live closed-loop daemon. Flags: `--dry-run`, `--wake HH:MM`, `--poll-seconds`, `--side`, `--simulate`, `--max-ticks`, `--db` |
| `auth`      | Store Eight Sleep credentials (0600 file or env vars); `--test` verifies the connection |
| `calibrate` | Read-only probe of the live Pod (capabilities, current level, bed/room temp, biometrics) |

## Project layout

```
sleepctl/
  models.py            # shared dataclasses + enums (the frozen contract)
  config.py            # UserProfile / Benchmarks / Tunables / AppConfig, CONTROL_PRIORITY
  adapters/            # base ABCs + eightsleep_cloud, calendar (Google), simulator,
                       #   raw_capture (Tier 1), local_frank (Tier 2 gated)
  controller/          # state_machine, controller, thermal, wake_detection,
                       #   induction, maintenance, smart_wake
  learning/            # baselines (7/14-day median+MAD), policy (tiered), response curves
  storage/             # SQLite schema (3 layers + ledgers) + repository
  loop/                # runtime (tick/replay) + nightly (learn cycle)
  recon/               # non-invasive network spike + gated teardown reference
  cli.py
tests/                 # unit + end-to-end tests
DESIGN.md              # full design spec
```

## Status

The controller, learning loop, storage, simulator, and offline runtime are implemented and
tested (`pytest`: green). Live device wiring (`run`/`auth`/`calibrate`) and the Tier 1/2
data paths are scaffolded behind the common adapter interface and documented in `recon/`.
