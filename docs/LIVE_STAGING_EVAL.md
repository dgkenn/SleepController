# The live staging path against EEG: what each post-processing step does

`cv_report_bidsleep.json` scores the stager's own HMM output. The controller reports that
output after five more steps: the actigraphy wake override, the beat-interval rescorer, deep
corroboration, the hypnogram constraint and the stage hold, all timed by the onset detector.
`scripts/eval_live_staging.py` replays held-out nights through exactly those calls, one 30 s
tick at a time, and scores every step against the EEG labels.

## Data and attribution

BIDSleep Apple Watch Dataset, v1.0.1, PhysioNet,
<https://physionet.org/content/bidsleep-dataset/1.0.1/>, DOI
[10.13026/rees-1092](https://doi.org/10.13026/rees-1092). Open Data Commons Attribution
License v1.0 (ODC-By 1.0). sleep-accel (Walch et al. 2019, DOI 10.13026/hmhs-py35, ODC-By 1.0)
joins every fold's training set. Only aggregate held-out metrics are recorded here; the data,
fold weights and per-night replays stay in the scratch folder.

## Method

* **Held out by subject.** Five GroupKFold folds over the 47 BIDSleep subjects. Each fold's
  HR and HR+motion (scale-free) heads and HMM are trained exactly like the shipped ones
  (`rf_d12`, stride 2, natural wake head, shipped temper and window) on the other subjects plus
  sleep-accel. Every one of the 253 nights is then replayed with the fold that never saw its
  subject.
* **The live calls, at the live rate.** Per 30 s tick, as `SleepController.decide` does it: the
  latest HR and the movement index of the latest actigraphy batch on the frame, the trailing
  45 min of HR and batch counts as the dense histories, `estimate_sleep_stage` ->
  `constrain` -> `_hold_stage` -> `hypnogram.observe`, then `SleepOnsetDetector.evaluate` on the
  adopted stage, with the 60-frame buffer and the controller's own `_sleep_baseline`. Bed entry
  is the recording start. Each tick is scored against the 30 s epoch it closes.
* **Tune, then confirm.** Settings were chosen on folds 0-2 (158 nights, 29 subjects) and
  checked on folds 3-4 (95 nights, 18 subjects). Intervals are a subject-level bootstrap.
* **Fidelity limits.** BIDSleep is a wrist watch: HR about every 5 s, and actigraphy per 30 s
  epoch, split here into seven ~4.3 s batches at the epoch's mean absolute deviation (the Verity
  sends about seven per 30 s). A wrist moves more than an upper arm, so the actigraphy wake
  override fires more here than on the band. There are no beat intervals, so **the rescorer is
  not scored**: it never fires on these nights.

## Step by step (all 253 nights, 211k epochs)

Truth: deep 20.8% and REM 29.1% of sleep; wake 11.0% of epochs. Cumulative, in live order.
"Shipped" is the post-processing before this change, "now" is the new defaults.

| step | κ | wake P / R | deep P / R | REM P / R | deep % | REM % | deep MAE (bias), min | REM MAE (bias), min |
|---|---|---|---|---|---|---|---|---|
| stager ticks alone | 0.407 | 0.64 / 0.42 | 0.65 / 0.66 | 0.57 / 0.51 | 20.6 | 25.5 | 31.3 (+1.6) | 45.4 (-10.9) |
| + actigraphy wake | 0.394 | 0.44 / 0.53 | 0.66 / 0.65 | 0.58 / 0.47 | 21.2 | 24.9 | 30.8 (-1.6) | 47.3 (-19.6) |
| + deep corroboration | 0.378 | 0.44 / 0.53 | 0.57 / 0.66 | 0.58 / 0.47 | 24.9 | 24.9 | 31.4 (+12.1) | 47.3 (-19.6) |
| + hypnogram constraint | 0.355 | 0.44 / 0.53 | 0.64 / 0.57 | 0.59 / 0.39 | 19.4 | 20.0 | 30.1 (-8.0) | 55.0 (-37.4) |
| + stage hold = **shipped** | 0.350 | 0.44 / 0.53 | 0.65 / 0.56 | 0.58 / 0.37 | 18.8 | 19.4 | 30.5 (-10.2) | 56.0 (-39.4) |
| **now** | **0.383** | 0.44 / 0.53 | 0.68 / 0.61 | 0.58 / 0.45 | 19.4 | 23.4 | 30.5 (-7.9) | 50.0 (-25.0) |

Leave one step out of "shipped": without actigraphy wake κ 0.373 (wake P / R 0.64 / 0.42),
without corroboration 0.354, without the hypnogram constraint 0.378, without the hold 0.355.

## What erased deep and REM

* **The hypnogram's "before sleep onset" rule** (the largest). REM and deep were refused until
  the onset DETECTOR confirmed, and on a heart-rate-and-motion feed it confirms late (often via
  its 30-minute fallback) and back-dates; everything scored in between was relabelled light.
  That is first-cycle N3: 2,498 EEG-deep ticks relabelled, 2,415 of which the stager had called
  deep. **Fix:** `provisional_onset_min = 10`. A run of adopted sleep held 10 min stands in
  for onset until the detector confirms, and the deep/REM latency floors count from its start
  (REM still waits 35 min, a run broken by AWAKE starts again). Deep recall +0.049
  [+0.029, +0.073], κ +0.014.
* **The re-entry rule after every AWAKE tick.** A movement burst reads AWAKE for about three
  ticks (the 60 s actigraphy window), and each one then barred REM and deep for 5 min of light:
  4,507 EEG-REM ticks relabelled. **Fix:** an awakening must be held 2 min
  (`reentry_min_awake_min`), or recur within 5 min of the last awake tick
  (`reentry_recurrent_awake_min`), which still catches the 2026-08-30 REM/AWAKE oscillation the
  rule was written for. The recurrence clause costs 0.01-0.02 REM recall; it stays as the
  safeguard. REM recall 0.37 -> 0.45.
* **Deep corroboration** adds deep rather than erasing it, but 7% of its upgrades were EEG deep
  (58% light, 26% REM, 9% wake). It was written for a stager that never called deep; the
  retrained one calls it at about the EEG rate. **Fix:** `deep_corroboration = False`.
* **Kept:** the deep re-entry rules (they relabel more light, REM and wake than deep, and
  2 or 5 min instead of 3 changes nothing), the stage hold (κ -0.005, but it stops the
  deep/light flapping that feeds `stage_regression` wake votes; 3 ticks is worse), the latency
  floors and the actigraphy wake override (below).

## Before / after on held-out subjects

| | nights | κ | deep P / R | REM P / R | deep % (truth) | REM % (truth) | wake P / R |
|---|---|---|---|---|---|---|---|
| tune, shipped | 158 | 0.368 | 0.67 / 0.57 | 0.63 / 0.39 | 18.6 (20.9) | 18.7 (28.9) | 0.44 / 0.54 |
| tune, now | 158 | 0.404 | 0.71 / 0.62 | 0.62 / 0.46 | 19.1 | 22.7 | 0.44 / 0.54 |
| confirm, shipped | 95 | 0.318 | 0.61 / 0.56 | 0.51 / 0.35 | 19.2 (20.6) | 20.6 (29.3) | 0.43 / 0.50 |
| confirm, now | 95 | 0.347 | 0.64 / 0.60 | 0.51 / 0.42 | 20.1 | 24.5 | 0.43 / 0.50 |

Confirm half, subject bootstrap, now minus shipped: κ +0.028 [+0.018, +0.037], deep recall
+0.046 [+0.017, +0.081], deep precision +0.022 [+0.009, +0.036], REM precision -0.000
[-0.014, +0.012]. Wake labels are identical: none of the changes touches an AWAKE label, so
wake recall and precision cannot move.

## What is left

* **Actigraphy wake override.** Only 37% of the ticks it forces to AWAKE are EEG wake here, and
  dropping it would raise κ by 0.023 and wake precision 0.44 -> 0.64, but wake recall would
  fall 0.53 -> 0.42. Wake recall drives pre-emption and the smart alarm, and wrist motion
  overstates how often an upper-arm band would fire, so it stays as it is.
* **The stager on live inputs** scores κ 0.407 against 0.47 in the offline CV. The live
  features see a 45-min history (training normalised over the night so far), a clock that
  starts at the detector's onset rather than the EEG's, and a nominal 8-hour night. That gap is
  in the model inputs, not in the post-processing.
* **The beat-interval rescorer** needs an EEG corpus with beat intervals before it can be
  scored.

## Reproduce

```
python3 scripts/eval_live_staging.py train-folds --data-dir $SCRATCH/bidsleep/reduced \
    --sleep-accel-dir $SCRATCH/sleep_accel --cache-dir $SCRATCH/bidsleep/cache \
    --folds-dir $SCRATCH/live_eval/folds                                   # ~20 min, 4 cores
python3 scripts/eval_live_staging.py replay --data-dir $SCRATCH/bidsleep/reduced \
    --folds-dir $SCRATCH/live_eval/folds --out-dir $SCRATCH/live_eval/runs  # ~80 min first run
python3 scripts/eval_live_staging.py report --out-dir $SCRATCH/live_eval/runs \
    --boot current,shipped-corroboration,prov10,awake2
```

The first replay caches every stager call per night, so further variants take seconds a night.
