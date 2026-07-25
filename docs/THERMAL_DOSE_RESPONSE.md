# The n-of-1 thermal dose-response trial — what maintenance temperature actually works for YOU

`sleepctl/ml/thermal_trial.py` + `GET /thermal/dose-response`. **Disabled by default**
(`ThermalTrialConfig.enabled = False`) — unlike the efficacy micro-trial (`sleepctl/ml/efficacy_trial.py`,
which only toggles active-vs-sham *control*), this trial changes what temperature the bed actually
runs at overnight, so it must be explicitly opted into.

## The scientific motivation

The controller ships a population-default **cool** bias for a hot sleeper
(`Tunables.hot_sleeper_cool_bias_f`, `maintenance_settle_nudge_f`). But the user's primary
complaint is *staying* asleep, not falling asleep, and there is a specific, published,
counterintuitive result that argues against a cool default for exactly that problem: **Raymann,
Swaab & Van Someren 2008** (*Brain*, [DOI 10.1093/brain/awm315](https://doi.org/10.1093/brain/awm315))
found that a **+0.4 °C skin-temperature rise SUPPRESSED nocturnal wakefulness** and deepened sleep —
mild *warming*, not cooling, reduced awakenings in their population. (This is the same Raymann
finding that motivates the settle-nudge sign-learning in `AWAKENING_PREVENTION.md` and the
"nudge lighter" asymmetry in `ARCHITECTURE_STEERING.md`.)

We don't know which direction — or magnitude — works for *this* person, so instead of guessing the
system runs a randomized trial and lets his own data answer it. The default offset ladder
(`[-1.5, -0.75, 0.0, +0.4, +0.8]` °F around the learned neutral) deliberately spans **both**
directions: the cooling arms test the population hot-sleeper prior, the warming arms test Raymann.
The data — not either prior — gets to win.

## How the randomization works

- **What moves.** Only `SetpointProfile.neutral_f` is shifted, by the assigned (comfort-band-clamped,
  default ±2 °F) offset. `deep_bias_f` and `wake_ramp_f` are separate absolute anchors and are left
  alone — the trial isolates "what should the bed run at while trying to *stay* asleep," not
  deep-sleep or wake-ramp behavior. Every existing safety layer still runs downstream unchanged:
  the 55–110 °F device clamp, slew limiting, the variability cap, smart-wake.
- **Eligibility.** Only `NORMAL`, full-length, in-bed-for-the-night sessions are ever randomized —
  byte-for-byte the same gate as `efficacy_trial.is_eligible`. Short/work/recovery nights and
  nap/induction sessions always get the control offset (0.0): a night that's already constrained
  never also gets a thermal experiment sprung on it.
- **Deterministic assignment.** Every assignment is a pure function of the calendar date string + a
  night-type block key + config (SHA-256-seeded, never `random`/`datetime.now()`), so the whole
  schedule is reproducible and auditable from the database alone — see `assign_arm` /
  `_deterministic_permutation`.
- **Permuted-block randomization, stratified by night type.** This is the one respect in which this
  trial goes beyond `efficacy_trial`'s simple date-hash coin flip: this user is a shift worker, and
  night type (normal / short / recovery) is a huge confounder on wake_events, so naive randomization
  risks correlating an offset arm with a run of bad-schedule nights by chance. Instead the calendar
  is sliced into consecutive-day blocks sized to a pool (each non-control offset exactly once, plus
  enough control slots to hit the target `experimental_fraction`), each block is independently and
  deterministically shuffled, and each day takes the next pool slot in shuffle order. This guarantees
  **exact** arm balance within every full block for a given night-type stratum, not just approximate
  balance over a long run — important for an n-of-1 trial where "the long run" may never really
  arrive.
- **Fraction cap.** At most `cfg.experimental_fraction` (default 0.5, hard-capped at
  `MAX_EXPERIMENTAL_FRACTION` = 0.6) of eligible nights ever run a non-control offset.
- **Per-arm auto-stop.** Once an individual arm has ≥ `auto_stop_min_n` (default 6) resolved nights
  and is trending clearly worse than control on wake_events (mean difference ≥ `auto_stop_threshold`),
  *that arm only* is suspended — logged as a structured event — and every subsequent draw of it
  resolves to control until the trend clears. The other arms keep running.
- **Skipped on an efficacy-trial sham night.** Applied *after* the efficacy micro-trial in the daemon
  tick order, and deliberately skipped if tonight was assigned `sham` by that trial (or `HELD` by the
  older standing efficacy trial): a sham night is a neutral do-no-harm hold by definition, so layering
  an experimental temperature offset on top would both corrupt that arm's sham-ness and confound the
  two experiments with each other. When skipped, the daemon logs why, so the audit trail shows a
  night with no thermal arm was a deliberate skip, not a bug.

## The `confident` flag — refusing to name a winner on thin data

`analyze_dose_response` computes per-arm means, a Welch's-t pairwise comparison of each arm against
control (95% CI + p-value, pure-Python, no numpy dependency — the same house style as
`efficacy_trial.py`/`sleepctl.eval.efficacy`), and a monotonic-trend readout across the ladder
(does mean wake_events fall/rise consistently as the offset warms, or is the pattern scattered?).

Below `min_nights_before_verdict` (default 8) resolved nights in *any* arm being compared, the
`confident` flag is `False` and the `verdict` string says so explicitly ("Not enough data yet...")
rather than implying a result. This matters for an n-of-1 design specifically because the sample
size is small by construction (one person, calendar-limited nights) — a difference measured from a
handful of nights is noise, not an answer, and the module refuses to dress it up as one.

## Surfacing

`GET /thermal/dose-response` (read-only) returns the trial config, planned/eligible/resolved night
counts, and the full `analyze_dose_response` output. Surfaced on the dashboard's Learning page
(`ThermalDoseResponseCard`).

## Related docs

- `AWAKENING_PREVENTION.md` — the same Raymann warming-vs-cooling tension, applied to the
  *pre-emptive settle nudge* rather than a randomized maintenance offset.
- `ARCHITECTURE_STEERING.md` §"nudge lighter" — the asymmetric warm-to-lighten maneuver, gated off
  by default for the same reason this trial exists: the direction that helps *this* person isn't
  assumed, it's measured.
- `MAINTENANCE_STRATEGY.md` — the older, engine-level forensics + n-of-1 experiment engine
  (`sleepctl/experiments.py`, `suggest_experiment`). That system attributes *observed* awakenings to
  candidate causes and proposes ad-hoc experiments; this trial is a purpose-built, pre-registered,
  block-randomized ladder specifically for the maintenance offset. They are complementary, not
  duplicates.
- `sleepctl/ml/efficacy_trial.py` (undocumented as its own doc as of this writing — see the module
  docstring) — the sibling active-vs-sham trial this module mirrors architecturally and coordinates
  with at the daemon level.
