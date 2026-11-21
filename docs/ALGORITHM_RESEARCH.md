# Ready-made algorithms for Apple Watch sleep data, and what we adopted

Survey date: November 2026. Goal: start from published, validated methods for each job the
data feeds, then let the personal learning loops tailor them. **Evidence decides priority**:
what has independent, peer-reviewed validation leads; what does not has to earn its place on
this sleeper's own nights.

## Evidence grades and what each controls

| grade | what | evidence | role here |
|---|---|---|---|
| A | Apple's sleep stages | independent PSG validations (kappa 0.53, Series 8; known deep under-call of ~43 min) | the lead reference rater; bias built into its prior |
| A | Cooler bed surface all night | randomized PSG trials: 3 centres, n=72, N3 +7.5 min, HR -2.4 bpm ([Herberger 2024](https://pubmed.ncbi.nlm.nih.gov/38409133/)); earlier crossover n=15 | **first** experiment in the program |
| A | Resting-HR deviation as an illness signal | [Mishra 2020, Nat Biomed Eng](https://www.nature.com/articles/s41551-020-00640-6); temperature and breathing-rate wearable studies | strain flag; flagged nights excluded |
| B | Warming at sleep onset | [Raymann 2005](https://pubmed.ncbi.nlm.nih.gov/15677527/) (onset), [Fronczek 2008](https://pmc.ncbi.nlm.nih.gov/articles/PMC2542970) (small, narcolepsy) | the existing onset program |
| B | Vagal HRV as a recovery marker | meta-analyses: HRV guidance improves vagal indices (SMD 0.50) with small performance effects ([Granero-Gallegos 2021](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8507742/)) | 20% of the recovery score |
| C | Cooling only in deep sleep / warming in REM | physiological rationale; no trials | later experiments, after the A-grade one |
| C | AutoSleep's stages | no published PSG validation; can re-use Apple's stages; reviewers report large disagreements with Apple on deep sleep | **reported, not counted**, until it agrees with Apple (kappa >= 0.4, below 0.95 = not a copy) on 3+ of your nights |
| C | OpenVitals stager | the author's own benchmark, not peer-reviewed | **reported, not counted**, until it agrees with Apple (kappa >= 0.4) on 3+ of your nights |
| C | Recovery-score weights | judgment, weighted toward what the A-grade trials moved and what is measured best | experiments' outcome; re-weightable |
| - | Warmer bed all night | contrary to the A-grade cooling trials | dropped from the program |

## 1. Sleep staging

| method | input | accuracy (4-class kappa vs PSG) | licence | live? | used here |
|---|---|---|---|---|---|
| Our stager (`sleepctl/ml/sleep_staging`) | HR (+ motion) | smoothed 0.45 on held-out subjects (BIDSLEEP_TRAINING.md) | ours | yes (causal) | live steering |
| [OpenVitals](https://github.com/Aditya-galaxy/openvitals) HR ensemble (5 TCN + 5 BiGRU) | HR only | 0.51 Walch held-out; 0.46 at 1 reading/min; 0.24 on a sleep-clinic cohort | MIT code, ODC-By data | no (whole night) | **morning rater** (`sleepctl/ml/openvitals`) |
| [Walch et al. 2019](https://academic.oup.com/sleep/article/42/12/zsz180/5549536), [code](https://github.com/ojwalch/sleep_accel) | HR + motion | ~0.5 wake/sleep, lower 3-class | open | per-epoch | training data already used |
| [Apple Watch 4-class open model (PMC12931632)](https://pmc.ncbi.nlm.nih.gov/articles/PMC12931632/) | instantaneous HR + accel | multi-night Apple Watch study | open source | - | candidate |
| [wrist-sleep-staging-public](https://github.com/lizxu-byte/wrist-sleep-staging-public) | HR + motion, quality gate | Walch LOSO | open | - | reference only |
| PPG deep models ([MADSOLSEN](https://github.com/MADSOLSEN/SleepStagePrediction), [SleepPPG-Net2](https://www.researchgate.net/publication/397136198_SleepPPG-Net2_deep_learning_generalization_for_sleep_staging_from_photoplethysmography)) | raw PPG waveform | higher | various | - | **not usable**: Apple exposes no raw PPG |
| Apple's own stages ([white paper, Oct 2025](https://www.apple.com/kr/health/pdf/Estimating_Sleep_Stages_from_Apple_Watch_Oct_2025.pdf)) | Apple's sensors | 0.53 (Series 8, [SLEEP Advances 2025](https://academic.oup.com/sleepadvances/article/6/2/zpaf021/8090472)) | closed | no | morning reference |

Findings that shaped the design:

- **Apple under-calls deep sleep.** Against lab PSG, Apple Watch Series 8 under-estimated deep
  by ~43 min and wake by ~7 min, and over-estimated light by ~45 min
  ([Sensors 2024, 35 adults](https://pmc.ncbi.nlm.nih.gov/articles/PMC11511193/)). Treating
  Apple as ground truth would teach the controller that bias. The consensus therefore starts
  Apple's confusion matrix from that finding (`wearable_consensus.PRIOR_MATRIX`), and adds
  OpenVitals as a PSG-trained, Apple-independent second rater.
- **Motion adds little for whole-night HR models** (OpenVitals: more HR training nights helped
  +0.05-0.07 kappa; motion did not), which supports Watch-only (HR) control.
- **Whole-night models beat causal ones**, but a bed has to be steered live. So: causal stager
  live, whole-night raters in the morning, and the morning consensus calibrates the live stager.

## 2. Overnight physiology and recovery

- **Resting heart rate:** lowest 5-minute mean while asleep (OpenVitals; staging errors move it
  < 1 bpm). Adopted in `learning/recovery.lowest_5min_hr`, with the window widened for Apple's
  sparse overnight readings.
- **HRV:** wrist HRV is good for "compared with my usual", not as an absolute (OpenVitals: wrist
  RMSSD reads ~28% high vs ECG but ranks well, rho 0.88). Apple stores SDNN from a few
  ~1-min readings a night; 6 readings put the nightly value within ~10%. Used only relative to
  the personal baseline.
- **HRV-guided readiness** ([Plews/Buchheit style](https://www.trainingpeaks.com/coach-blog/new-study-widens-hrv-evidence-for-more-athletes/)):
  a 7-day rolling ln-HRV against a band of mean ± 0.5 SD (the "smallest worthwhile change").
  Our recovery score uses robust z-scores against the previous 30 nights instead (same idea,
  nightly resolution); a 7-day trend band is a candidate addition.
- **Personal-baseline recovery score:** OpenVitals scores 0-100 with 50 = your normal over the
  previous 28 nights, the same design as `learning/recovery.recovery_score`.

## 3. Strain / illness flag

- [Apple Vitals](https://support.apple.com/120142): a 7-night typical range for overnight heart
  rate, breathing rate, blood oxygen, wrist temperature and sleep duration (HRV on newer
  watches); it alerts when **two or more** are outside the range.
- [Mishra et al. 2020, Nat Biomed Eng](https://www.nature.com/articles/s41551-020-00640-6):
  resting heart rate against a personal baseline (online CuSum) caught 63% of COVID-19 cases
  before symptoms.
- Adopted: `recovery.strain` flags a night when at least two of overnight HR (up), breathing
  rate (up), HRV (down), SpO2 (down) and wrist temperature (up) are 2+ robust SDs off your
  usual. Flagged nights are excluded from the baseline and from experiment comparisons, so a
  cold cannot decide which bed setting "won".

## 4. Personal thermal tuning

No published bandit or Bayesian-optimisation study of bed temperature was found. Adjacent work:
[a PMV-based sleep temperature controller](https://www.sciencedirect.com/science/article/abs/pii/S0360132324002622)
that personalises from feedback (84% satisfied vs 67% on a fixed curve),
[Bayesian meta-learning for personal thermal comfort](https://www.sciencedirect.com/science/article/abs/pii/S0360132323011563),
and [contextual preferential Bayesian optimisation for buildings](https://arxiv.org/pdf/2512.09481).
We keep the n-of-1 crossover program (counterbalanced arms, washout, cycle-paired intervals),
which matches the n-of-1 trial literature, and order it by the grades above: cooler baseline
(grade A) first, then the grade-C stage-timed variants; "warmer all night" is not run. Bayesian optimisation over continuous setpoints is
the natural upgrade once a few months of scored nights exist.
