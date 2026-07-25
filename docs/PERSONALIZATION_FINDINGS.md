# Personalizing the wearable sleep-stager: a measured negative result

**Question.** The controller only ever has to work for ONE person, so per-person adaptation looked
more promising than better population features. But that person will never have polysomnography
labels. Which adaptation mechanisms pay off *without* labels — and what is the ceiling *with* them?

**Answer: none of them, and the ceiling is flat.** This is worth writing down because it is
counterintuitive and it saved us from building machinery that measurably makes things worse.

## Setup

31 subjects (PhysioNet `sleep-accel`), one night each. For each held-out subject: train on the other
30, split that subject's night chronologically, adapt on the FIRST half, evaluate on the SECOND half
(so every arm is compared on identical test data).

## Results (mean over 31 held-out subjects)

| arm | mechanism | labels needed | 4-class κ | Δ vs A | wake κ | Δ vs A |
|---|---|---|---|---|---|---|
| A | population baseline | none | 0.321 | — | 0.209 | — |
| B | per-subject feature normalization | none | 0.324 | +0.003 | 0.226 | +0.017 |
| C0 / C | unsupervised prior + transition (Baum-Welch) | none | 0.307 / 0.283 | −0.014 / −0.038 | 0.206 / 0.210 | −0.003 / +0.001 |
| Cprior | Saerens/EM class-prior correction | none | 0.094 | **−0.226** | 0.120 | −0.090 |
| Cprior_hmm | prior correction + HMM | none | 0.071 | **−0.249** | 0.034 | −0.175 |
| Dthr / Dq50 / D20 | self-training on confident pseudo-labels | none | 0.314 / 0.305 | −0.007 / −0.016 | 0.215 / 0.186 | +0.006 / −0.023 |
| CD | prior adaptation + self-training | none | 0.294 | −0.027 | 0.171 | −0.038 |
| E | weak-label calibration (total sleep time + awakening count) | 2 self-reported scalars | 0.283 | −0.038 | 0.090 | **−0.119** |
| F10 → F100 | supervised fine-tuning (CEILING, unachievable) | true PSG labels | 0.290 → 0.325 | −0.030 → **+0.004** | 0.187 → 0.171 | −0.022 → −0.038 |

(κ values are internal to this experiment's pipeline and are NOT comparable to the shipped model's
leave-subjects-out numbers; only the within-table deltas are meaningful.)

## What this means

1. **Per-subject normalization (B) is the only non-harmful adaptation** — and it is already
   implemented in the shipped feature pipeline. Its gain is real but marginal (+0.003 / +0.017),
   below our +0.03 "meaningful" bar.
2. **Do not build unsupervised prior/transition adaptation or self-training.** Measured neutral to
   harmful. Self-training in particular drifts toward its own confident mistakes.
3. **Do not build weak-label calibration off the morning check-in.** This was the mechanism that
   looked most promising in principle — the dashboard already collects total sleep time and
   awakening count — but forcing predictions to match those two scalars *badly* damaged wake
   detection (−0.119 κ), which is the user's number-one concern.
4. **The supervised ceiling is essentially flat (+0.004).** Even ~238 minutes of that person's true
   labels barely beat the population model. Thirty subjects already span the relevant physiological
   variation; one person's half-night adds almost nothing on top.

## Important caveat — a confound that partly explains the failures

Each subject has only ONE night, so "personalization" had to be simulated by splitting within the
night. But sleep architecture is systematically non-stationary across a night: **deep sleep is
front-loaded and REM is back-loaded**. So adapting on the first half and testing on the second half
is partly a *distribution shift*, not a clean personalization signal.

This very likely explains why the class-prior arms failed so catastrophically: they estimate class
priors from a deep-heavy first half and apply them to a REM-heavy second half, which is close to the
worst thing you could do. `Cprior` failing is therefore **not** proof that prior adaptation is
useless in production, where adaptation would happen *across nights* rather than within one.

What this experiment CAN support:
- Label-free adaptation offers no reliable within-night win. (Robust.)
- The ceiling is flat, so headroom from personalizing on a *single* night is small. (Robust.)

What it CANNOT settle:
- Whether adaptation across MANY of the user's own nights helps. That needs multi-night-per-subject
  data, which `sleep-accel` does not have (one night each).

### How to settle it: BIDSleep

A dataset survey (see below) turned up **BIDSleep**
(<https://physionet.org/content/bidsleep-dataset/1.0.0/>) — open access (ODC-BY, verified by direct
download, no credentials), **47 healthy adults, 253 nights, up to 7 nights per subject**, Apple Watch
HR + accelerometer synced to a Dreem-2 EEG headband, 30 s epochs, AASM stages with
*expert-corrected* labels.

Multiple nights per subject is exactly the missing ingredient: adaptation can be fitted on a
subject's EARLIER nights and tested on their LATER ones, which is the real deployment scenario and
removes the within-night deep-early/REM-late confound that sank the arms above.

Feasibility: the full dataset is ~28 GB (infeasible at our ~150 KB/s), but the bulk is
`motion.csv` (~100 MB/night). `hr.csv` (0.2 Hz) and `labels.mat` are tiny, so **HR + labels for all
253 nights is cheap to fetch** — enough for a proper across-nights personalization test on the
HR-only path we actually ship.

## Dataset survey (why we are not simply getting better data)

No open dataset satisfies the full requirement — beat-to-beat intervals **and** wrist/arm
accelerometer **and** expert PSG stages **and** a healthy population.

| dataset | access | population | signals | verdict |
|---|---|---|---|---|
| **DREAMT** | **CREDENTIALED** (HTTP 403 confirmed) | clinical: mean BMI 33.7, 68/100 obese, 23 severe OSA | E4 BVP 64 Hz + ACC + derived IBI + PSG | reject — inaccessible *and* the OSA-HRV confound that already distorted our slpdb experiment |
| **BIDSleep** | open (verified) | 47 healthy adults | Apple Watch HR + ACC ~50 Hz, expert AASM labels, **253 nights / up to 7 per subject** | best available; no IBI, but uniquely enables the across-nights test |
| **MMASH** | open | 22 healthy young males | real RR intervals + accel | no PSG stages (actigraphy-derived sleep only) — HRV reference only |
| **CinC 2018** | open | clinical | ECG only, no PPG/accel, ~267 GB | reject — wrong modality, infeasible size |

### CORRECTION — that conclusion was wrong

An earlier revision of this document concluded that "true beat-to-beat intervals paired with expert
stage labels in a healthy population do not exist in any reachable open corpus." **That is false.**
The first survey was PhysioNet-centric; widening it to general research repositories found several,
the best by a wide margin being:

| dataset | access | population | cardiac signal | labels | fetchability |
|---|---|---|---|---|---|
| **BOAS** (Bitbrain Open Access Sleep, OpenNeuro `ds005555`) | **CC0, no login** (anonymous S3 verified) | **108 healthy adults / 128 nights**; ~91 with usable PPG | **PPG @ 256 Hz** — PSG pulse *and* a wearable **headband** PPG. No ECG. | AASM 30 s, **3 independent scorers + 4th tiebreaker**, ~85% inter-scorer agreement | per-subject EDF, ~155-240 MB each — cherry-pickable |
| **DOD-H** (Dreem, Zenodo) | open, MIT | 25 healthy | ECG @ 250 Hz | **5 independent scorers** — best label quality found | 21.9 GB single zip, but HTTP Range requests work, so per-record extraction is possible |
| **AAUWSS** (Aalborg, Zenodo) | open, CC-BY-4.0 | 13 healthy (ESS <10) | ECG 200 Hz **+ Empatica PPG 64 Hz + 3-axis accel**, with **pre-extracted IBI** | AASM, single scorer | one 6.57 GB zip |

**BOAS is the primary target.** It is the largest open *healthy* cohort with beat-derivable cardiac
data and consensus expert labels, and — unlike every ECG source — its signal is **PPG, the same
optical modality as the Verity**. That matters concretely: the literature (van Gilst 2020) shows
ECG-trained staging algorithms *lose* accuracy when applied to PPG, so training on PPG avoids a
transfer penalty we would otherwise eat.

Still genuinely unusable: **MASS** and **Wearanize+** (both require signed data-use agreements),
and **WildPPG** / multi-site PPG sets (no sleep-stage labels at all).

Lesson worth keeping: the first survey's negative result was an artefact of searching one catalogue.
"Does not exist" claims need a wider search than "I checked the obvious place."

## Consequent plan

- Keep per-subject normalization; build none of the other adaptation machinery.
- Keep persisting the user's own raw data (RR intervals, actigraphy counts, dense HR — see
  `rr_intervals` / `actigraphy` / `sensor_samples`), which is what a proper across-nights test would
  need and which cannot be recovered retroactively.
- Revisit personalization only once enough of the user's own nights exist to evaluate across nights
  rather than within one.
- Effort is better spent on data quality (frozen-HR / not-worn guards) and on the controller's own
  multi-signal detectors than on chasing per-person model adaptation.
