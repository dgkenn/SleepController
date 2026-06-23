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
```

Run the tests:

```bash
pytest tests/
```

## CLI subcommands

| Command     | What it does |
|-------------|--------------|
| `replay`    | Drive synthetic nights (normal / short_sleep / clustered_awakenings) through the controller offline |
| `report`    | Print rolling baselines + recent nightly summaries from the dataset |
| `run`       | Live closed-loop daemon (requires a configured Pod adapter; wiring is the remaining integration step) |
| `auth`      | Authenticate to Eight Sleep / Google Calendar |
| `calibrate` | Probe Pod 2 capabilities + build the °F↔level calibration |

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
