# Preflight, readiness, and the silent-failure ledger

Three surfaces answer three different questions. They are easy to confuse, so this page states
which one to reach for and why they aren't merged.

| Question | Surface |
|---|---|
| *What is wrong right now?* | `GET /diag` — the runtime battery (`dashboard/api/app/diagnostics.py`) |
| *Is the data/learning side healthy?* | `sleepctl doctor` — the data battery (`sleepctl/diagnostics.py`) |
| ***Can I go to sleep and expect this to work tonight?*** | **`sleepctl preflight`** (`sleepctl/preflight.py`) |

## `sleepctl preflight` — GO / NO-GO

```bash
sleepctl preflight                 # sensor night (a silent Verity is blocking)
sleepctl preflight --no-sensor     # Pod-only night
sleepctl preflight --json          # for scripts; exit code 1 == NO_GO
```

Reachable remotely too, because standing at the machine is not the situation you need it in:

```
GET /diag/preflight?token=<DIAG_TOKEN>              # JSON
GET /diag/preflight?token=...&format=text           # the rendered report
GET /diag/preflight?token=...&sensor=0              # Pod-only night
```

and the verdict rides in the published health snapshot (`preflight` block on the `health`
branch) — the only window into the box from off-site. All three derive the verdict from the
**same** battery whose checks are reported beside it, so they can never disagree with each other.

### It runs itself once an evening

The preflight only helps if somebody runs it, and nobody remembers to run a health check before
bed. So the daemon calls `services.check_pre_bed_readiness` every tick; it self-gates to the two
hours before the night window opens (default 19:00–21:00), runs at most once per calendar day, and
pages the phone **only on NO_GO**.

The distinction from the existing nighttime failure pager matters: that one fires once you're in
bed, which is right for "the reservoir just ran dry" and useless for "the daemon died this
afternoon" — by then the night is already lost. This one fires while there is still time to fix
something. A GO_DEGRADED night still goes ahead and is deliberately not paged; alerting on a night
that will happen anyway is how you teach someone to ignore alerts.

Either way the evening verdict is written to the event log, so not paging is not the same as not
knowing.

Thirty checks across two batteries with three severities is not an answer to "can I rely on this
tonight", and the severities aren't tuned for that question anyway. The preflight re-reads the
**same** checks through that one lens and sorts them:

- **BLOCKING** — the controller cannot do its job. Fix before relying on it.
- **DEGRADED** — it will run, with a limb tied behind its back.
- **NOTES** — worth knowing, not blocking.

It owns **no checks of its own**; it is purely a policy statement about which existing check
matters for which purpose, so there is one place to change that opinion and no third battery to
keep in sync.

Two things it catches that nothing else does:

- **Dry run.** `SLEEPCTL_DRY_RUN=1` is reported as `info` by the runtime battery, and correctly so
  — nothing is broken. The controller decides all night and sends the bed *no commands at all*.
  Every check green, zero thermal control. This is the single case the preflight exists for.
- **A dead API.** The runtime battery's `api` check reasons "this function is running, therefore a
  request reached the API process, therefore the API is up". Sound inside `/diag`; a permanent
  false green when the battery is imported into a CLI process. The preflight probes the socket.

## The silent-failure ledger

The control loop wraps every optional subsystem so a failure degrades that feature instead of
killing the night. That is the right call, and it creates this system's worst failure mode: **a
subsystem can throw on every tick for eight hours while every indicator stays green** — because
nothing *is* broken. The loop is healthy. It just isn't doing the thing. A stage estimator failing
all night looks exactly like a healthy night with an unremarkable trace.

`sleepctl/degradation.py` records every swallowed failure, keyed by subsystem, persisted to the
`events` table so it survives a restart and reaches the published health snapshot. The daemon
routes sixteen control-path handlers through `_skip()` — including the wearable fusion and dense
history the Verity depends on, the learned-profile load, and the nightly close-out.

Writes are rate-limited by time **and** escalate on powers of ten. Time alone would record a
subsystem that failed 200 times in the first minute as "1x", under-reporting the severity to
exactly the remote reader who can't see the process.

Surfaced as the `degraded` check: `ok` when nothing was skipped, `info` for isolated skips, `warn`
when a subsystem crossed the notable threshold — with the name, the count, and the last error.

## The `calibration` check

Reports the three measurements that turn evidence priors into *your* physics:

1. **Thermal self-test** (`POST /diag/action/self-test`, in bed) — the bed's real heat/cool rates
   and lags. Sizes the pre-cool lead, the onset cascade, and the wake ramp. Without it they use
   generic presets.
2. **Comfort sweep** (`comfort_cal_start`, in bed) — your neutral °F. Every thermal intent is an
   offset from it, so a wrong neutral shifts the whole night's policy.
3. **Morning check-ins** (`sleepctl checkin`) — the ground truth `perfect_weights` and
   `ideal_architecture` personalize against.

Deliberately **`info`, never `warn`**: nothing is broken, and pinning the dashboard to DEGRADED for
weeks over an in-bed setup step teaches you to ignore the verdict. Readiness belongs in the
preflight, not the health gate.

## The `prevention_timing` check

See `sleepctl/learning/prevention_timing.py`. The pre-cool ledger records *whether* an awakening
happened anyway; this splits *why it failed*, because the two causes need opposite fixes:

- **Timing failure** — you woke before the bed had moved. A bigger nudge cannot fix an arrival that
  comes after the awakening. Fix: longer lead.
- **Dose failure** — the bed demonstrably arrived and you woke anyway. Fix: magnitude, or accept
  that this window isn't thermally preventable.

Conflated, the settle learner sees one low prevention rate and tunes magnitude forever against a
problem magnitude cannot fix.

Arrival is **measured** off the bed's own temperature trace (`raw_samples.bed_temp_f`), not
modelled — so it needs no calibration and reflects whatever the water loop is actually doing. That
also means "cooling commanded, bed never moved" comes back as `no_thermal_response`: a broken
actuator, reported as such, rather than a lead-time recommendation.

## Operational hardening worth knowing about

- **The offsite backup branch re-roots on every run.** Pruning to the newest 14 blobs only changed
  the working tree; every encrypted database ever pushed stayed reachable from earlier commits, so
  the remote grew by one full DB per day forever while the visible tree looked correctly bounded.
  These are artifacts, not source — `scripts/backup-encrypted.ps1` now force-pushes a single root
  commit. It also fails early, with the measured size, when the blob approaches GitHub's 100MB
  per-file limit; the Verity's row rate makes that a matter of months, not years.
- **The forwarder throttles repeated identical failures.** Nothing rotates `.run/verity.log`, and
  the forwarder POSTs every ~2s all night. A persistent failure would fill the disk that also holds
  the SQLite DB — and a full disk fails *writes* while deletes still succeed, so it presents as the
  controller mysteriously losing data rather than as a disk problem.
- **`doctor.ps1` reports the supervisor and power state.** Task state, last run/result, missed runs,
  last boot, and AC-vs-battery — because "everything stopped days ago" is usually the box going
  away, not a crash, and the logs simply end.
