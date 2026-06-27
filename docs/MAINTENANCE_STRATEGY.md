# Sleep‑Maintenance Strategy — Predictive Pre‑emption + Forensics/n‑of‑1

The user's #1 problem is **staying asleep**. This document is the evidence‑grounded design
rationale for the two features that attack it: **(1) predictive awakening pre‑emption** and
**(2) per‑awakening forensics + an n‑of‑1 learning engine**. Evidence is from PubMed; DOIs are
linked. These features are advisory aids, not a medical device.

## Feature 1 — Predictive awakening pre‑emption

### Why prediction is possible
**Autonomic activation precedes cortical awakening.** HRV spectral energy rises in the ~5 min
*before* a cortical arousal — "autonomous activation precedes cortical arousal" (Busek 2005,
[PMID 16163654](https://pubmed.ncbi.nlm.nih.gov/16163654/)); the canonical sequence is
cortical → autonomic‑cardiac → motor, HR leading the overt event (Kato 2001,
[DOI 10.1177/00220345010800101501](https://doi.org/10.1177/00220345010800101501)).
→ The *final* arousal is too fast to catch; the **rising arousal pressure over minutes** is the
target. Hence `PrecursorDetector` fits **trends over a window**, weighting **HRV‑energy decay
highest**, then HR creep, restlessness, bed warming, breathing irregularity.

### Awakenings ride an oscillation (CAP)
Awakenings cluster in high‑CAP‑rate windows of NREM instability (Zucconi 1995,
[PMID 7797629](https://pubmed.ncbi.nlm.nih.gov/7797629/)). → A **sleep‑instability index**
(micro‑movement‑burst density) scales pre‑emption: lower the trigger threshold in unstable
windows; stay hands‑off in stable deep sleep.

### The thermal actuator — and the direction tension
- Cutaneous **warming** suppresses nocturnal awakenings and deepens sleep (+0.4 °C skin, core
  unchanged — Raymann 2008, [DOI 10.1093/brain/awm315](https://doi.org/10.1093/brain/awm315);
  onset −26% — [DOI 10.1152/ajpregu.00492.2004](https://doi.org/10.1152/ajpregu.00492.2004)).
- But distal **cooling drives alertness** (Fronczek 2008,
  [DOI 10.1093/sleep/31.2.233](https://doi.org/10.1093/sleep/31.2.233)), and over‑cooling
  blunts REM/SWS (Cerri 2005, [DOI 10.1093/sleep/28.6.694](https://doi.org/10.1093/sleep/28.6.694)).
- Thermal preference also shifts across the night with circadian phase (Vellei 2021,
  [DOI 10.1080/23328940.2021.1976004](https://doi.org/10.1080/23328940.2021.1976004)).

→ **Resolution:** the settle nudge is a small, comfort‑bounded, **signed, per‑phenotype
learnable** move. Default cools (hot sleeper) but `learn_settle_nudge()` flips toward warm if
cooling consistently fails to prevent *this* user's awakenings (revealed preference from the
pre‑cool efficacy ledger).

### Detection feasibility + the hard constraint
BCG (the Pod's sensor) classifies sleep‑wake at ~95% (Ahmed 2022,
[DOI 10.1109/EMBC48229.2022.9871831](https://doi.org/10.1109/EMBC48229.2022.9871831)), so the
signal exists — but the cloud delivers **minute‑resolution data with multi‑minute lag**, and
the bed has a thermal response lag. Actionable pre‑emption therefore requires
`lead ≥ data_lag + thermal_lag`; the win is the **predictable, clustered** awakenings
(recurring times, post‑warm‑drift, the 3:30–5:30 nadir), not every random one.

### Implemented (feature 1)
Evidence‑weighted precursors (HRV‑led) · instability‑gain threshold · signed learnable settle
nudge (`ThermalController.set_settle_nudge`, `learning/settle.py`, wired in the live daemon).

## Feature 2 — Forensics + n‑of‑1

### n‑of‑1 done rigorously
Aggregated n‑of‑1 trials match/beat parallel & crossover RCTs in power per subject, **but
carryover and selection bias inflate type‑I error if unmodeled** (Blackston 2019,
[DOI 10.3390/healthcare7040137](https://doi.org/10.3390/healthcare7040137)); the valid design
is **multiple crossover cycles with washout** (Vrinten 2015,
[DOI 10.1136/bmjopen-2015-007863](https://doi.org/10.1136/bmjopen-2015-007863); den Hollander
2023, [DOI 10.1016/j.conctc.2023.101233](https://doi.org/10.1016/j.conctc.2023.101233)).
→ The engine uses a **counterbalanced multi‑cycle schedule with washout nights** and a
**paired within‑cycle analysis** (each cycle its own control) with a **95% credible interval
that must exclude 0** — controlling slow drift and the serial autocorrelation of nightly data.

### Evidence‑grounded attribution
- **Alcohol → second‑half fragmentation** (increased WASO, no REM rebound — Chan 2013,
  [DOI 10.1111/acer.12141](https://doi.org/10.1111/acer.12141)) → weighted higher for late‑night
  awakenings.
- **Thermal** (warm bed / hot room) → architecture/REM effects (Cerri 2005, above).
- **Physiological hyperarousal** — sleep‑maintenance insomnia is a hyperarousal disorder
  (Kaplan 2022, [DOI 10.1016/j.brainresbull.2022.05.006](https://doi.org/10.1016/j.brainresbull.2022.05.006))
  → an HR surge with no thermal trigger flags a behavioral (CBT‑i) target the bed can't fix.

### The closed loop
Forensics' dominant cause → a one‑click n‑of‑1 (`suggest_experiment`) → the winning arm feeds
feature 1's setpoint / settle direction. Predict → prevent → learn.

## Limitations
Cloud latency caps lead time; everything is per‑phenotype (presets are evidence‑backed priors
that sharpen as nights accumulate); the hyperarousal framing is advisory and routes to
behavioral suggestions, not diagnosis.

*Sources retrieved from PubMed; please retain the DOI links above as attribution.*
