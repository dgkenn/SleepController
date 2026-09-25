# Training the HR / HR+motion stager on BIDSleep

The shipped HR-only and HR+motion stagers (`wake_hr` / `stage4_hr`, `wake_hrmotion` /
`stage4_hrmotion`, `hmm.json`) used to be trained on PhysioNet sleep-accel alone: 31 Apple
Watch nights with PSG labels. BIDSleep adds 253 more Apple Watch nights from 47 people
(3-7 nights each, free-living, EEG-headband staging reviewed by a sleep expert), so the
models now train on BIDSleep **plus** sleep-accel.

## Attribution

BIDSleep Apple Watch Dataset, v1.0.1, PhysioNet.
<https://physionet.org/content/bidsleep-dataset/1.0.1/>, DOI
[10.13026/rees-1092](https://doi.org/10.13026/rees-1092).
Open Data Commons Attribution License v1.0 (ODC-By 1.0). Open access: no login, no DUA.
Please also cite PhysioNet (Goldberger et al., *Circulation* 101(23), 2000).

sleep-accel (Walch et al. 2019, PhysioNet, DOI 10.13026/hmhs-py35, ODC-By 1.0) is the
second training corpus, as before.

Only code, the trained weight JSONs and the CV report are committed. The raw and reduced
data stay in a scratch folder.

## 1. Download and reduce (about 1.5 h on a throttled link, ~7 GB peak disk)

The dataset is 27.9 GB of CSV, nearly all of it 50 Hz accelerometer. PhysioNet serves
about 150 KB/s per connection, so there are two routes:

```
# A. per night: download one night's 3 files, reduce, delete (peak ~jobs x 250 MB)
python3 scripts/bidsleep_reduce.py --out $SCRATCH/bidsleep/reduced --no-motion --jobs 16   # HR + labels, 2 min
python3 scripts/bidsleep_reduce.py --out $SCRATCH/bidsleep/reduced --skip-existing --jobs 20

# B. faster: one 6.35 GB project ZIP over parallel range requests, then reduce from it
python3 scripts/bidsleep_reduce.py --download-zip $SCRATCH/bidsleep/raw/bidsleep.zip --zip-connections 20
python3 scripts/bidsleep_reduce.py --out $SCRATCH/bidsleep/reduced --from-zip $SCRATCH/bidsleep/raw/bidsleep.zip --skip-existing --jobs 3
rm $SCRATCH/bidsleep/raw/bidsleep.zip*

python3 scripts/bidsleep_reduce.py --out $SCRATCH/bidsleep/reduced --verify
```

Both routes can be resumed. Every file is checked against the published `SHA256SUMS.txt`.
The zip download took 75 min at about 1.5 MB/s; route A would have taken about 4 h.

Each night becomes its own recording `Bidslab00_n1`, in the formats the trainer already
reads (`<ID>_heartrate.txt`, `<ID>_labeled_sleep.txt`, `activity/<ID>_activity.txt`), plus
`<ID>_meta.json`:

* **Time base.** Everything is written relative to `recStart`. `recStart` is a US/Eastern
  wall-clock string; the script converts it with `zoneinfo`, and falls back to the US DST
  rule if the host has no tz database. The check that the conversion is right: HR starts a
  median of 9 s from `recStart`, both in EDT and in EST nights. One night (Bidslab14_n4)
  starts 2 h late and is flagged in its meta.
* **Labels.** `expert_label` is used when it has any scored epoch, otherwise `dreem_label`.
  All 253 nights have expert labels; Dreem and expert agree on 82% of epochs. Codes are
  mapped 0/1/2/3/4 -> 0/1/2/3/5 (REM), and 5 (unknown) -> -1 (unscored).
* **Motion.** The accelerometer is in g (median magnitude 1.000). The sample rate is
  33-65 Hz (median 50). Counts come from `scripts/polar_pmd.actigraphy_counts` over 30 s
  epochs on the label grid. PIM is a per-epoch *sum* over samples, so it depends on the
  sample rate. The scale-free motion features (ranks and robust z-scores within the night
  so far) do not.

sleep-accel: `python3 scripts/fetch_sleep_accel.py` then
`python3 scripts/reduce_motion_activity.py` (see those scripts).

## 2. Train and compare (about 1 h on 4 cores)

```
python3 scripts/train_bidsleep.py --data-dir $SCRATCH/bidsleep/reduced \
    --sleep-accel-dir $SCRATCH/sleep_accel --cache-dir $SCRATCH/bidsleep/cache --write
```

* **Bundled models first.** The script scores the bundled weights on every BIDSleep night.
  The sleep-accel models have never seen those subjects, so every night is held out.
* **Cross-validation.** 5-fold GroupKFold, grouped by **subject**: a person's nights are
  never split between train and test. The HMM counts transitions within each night, not
  across a subject's nights. The heads are fit on every 2nd epoch (`--train-stride`), and
  every epoch is scored.
* **Variants.** BIDSleep alone, and BIDSleep + sleep-accel. sleep-accel subjects join the
  folds, so the combined model is also scored on held-out sleep-accel subjects. The
  BIDSleep-only model is also fit on all of BIDSleep and scored on sleep-accel as an
  external test.
* **Model grid.** `rf_d12` beat `rf_d12_l150`, `et_d14_l100` and `rf_d16_l150` on the
  unsmoothed 4-class kappa (0.411 vs 0.393 / 0.350 / 0.396). Pass
  `--candidates rf_d12,rf_d12_l150,...` to search again.
* **Smoothing.** Tuned by the same CV: temper, prior and wake head.
* **Export gate.** `--write` exports only if the new models beat the bundled ones on
  held-out subjects: smoothed and raw kappa, smoothed deep recall, sparse-HR kappa, no
  wake-kappa loss > 0.05, and no REM-recall loss > 0.10; for HR+motion, kappa and deep
  recall. `--force` overrides the gate.
* **Output files.** The five weight files keep their names, so
  `SleepStager.load` needs no change. The CV report is `cv_report_bidsleep.json`.

## 3. Results (held-out BIDSleep subjects, 253 nights, 211k epochs)

"raw" is the per-epoch argmax. "HMM" is the shipped online forward filter (20 epochs,
temper 0.35), which is what the controller sees. Truth is deep 21.1% and REM 29.3% of
sleep; wake is 10.8% of epochs. deep/REM MAE and bias are per-night minutes.

HR-only, dense HR (~1 sample / 5 s):

| model | | 4-class κ | wake P / R | deep P / R | REM P / R | deep % of sleep | REM % of sleep | deep MAE (bias) | REM MAE (bias) |
|---|---|---|---|---|---|---|---|---|---|
| bundled (sleep-accel) | raw | 0.368 | 0.39 / 0.77 | 0.66 / 0.56 | 0.53 / 0.46 | 20.5 | 28.9 | 35.8 (-11.4) | 45.0 (-14.0) |
| bundled (sleep-accel) | HMM | 0.361 | 0.74 / 0.55 | 0.74 / 0.41 | 0.58 / 0.38 | 11.4 | 18.7 | 41.4 (-34.8) | 54.6 (-37.0) |
| BIDSleep | HMM | 0.453 | 0.81 / 0.61 | 0.64 / 0.71 | 0.55 / 0.54 | 22.6 | 28.4 | 29.9 (+8.2) | 46.1 (-0.1) |
| **BIDSleep + sleep-accel (shipped)** | raw | 0.436 | 0.83 / 0.63 | 0.61 / 0.76 | 0.47 / 0.73 | 25.5 | 44.3 | 33.6 (+19.1) | 68.5 (+60.8) |
| **BIDSleep + sleep-accel (shipped)** | HMM | **0.454** | 0.83 / 0.60 | 0.65 / 0.70 | 0.54 / 0.56 | 22.2 | 29.2 | 30.7 (+6.9) | 46.2 (+3.5) |

HR-only, sparse HR (1 sample / min), HMM: bundled κ 0.335 (deep R 0.43, REM R 0.42),
shipped κ 0.418 (deep R 0.72, REM R 0.47).

HR + motion (the live default when the Verity streams accelerometer):

| model | | 4-class κ | wake P / R | deep P / R | REM P / R | deep % | REM % | deep MAE (bias) | REM MAE (bias) |
|---|---|---|---|---|---|---|---|---|---|
| bundled (sleep-accel, absolute + scale-free counts) | HMM | 0.316 | 0.57 / 0.67 | 0.73 / 0.33 | 0.63 / 0.27 | 9.9 | 13.1 | 46.4 (-42.7) | 70.4 (-61.3) |
| BIDSleep + sleep-accel, absolute + scale-free | HMM | 0.476 | 0.80 / 0.65 | 0.65 / 0.71 | 0.58 / 0.55 | 22.5 | 27.2 | 28.3 (+7.1) | 48.1 (-5.3) |
| **BIDSleep + sleep-accel, scale-free only (shipped)** | HMM | **0.471** | 0.81 / 0.63 | 0.65 / 0.71 | 0.57 / 0.55 | 22.6 | 27.4 | 29.6 (+7.8) | 46.6 (-4.4) |

The scale-free motion block ships even though it scores 0.005 lower. The script picks it
whenever it is within 0.01 of the absolute-count model. The reason: watch sample rates vary
(33-65 Hz) and the live counts arrive per BLE batch from an upper-arm sensor, so absolute
PIM does not transfer between devices, while within-night ranks do. The motion data covers
all 253 BIDSleep nights; 29 of the 31 sleep-accel subjects had reduced motion when this
run started.

On held-out **sleep-accel** subjects, which the bundled model was trained on, the combined
model scores κ 0.436 HMM / 0.385 raw. The bundled model's own sleep-accel CV was
0.436 / 0.395, so nothing is lost there. The BIDSleep-only model, fit on BIDSleep and tested
on sleep-accel as an external corpus, scores 0.424 / 0.373.

Every export-gate check passed, so the five weight files were replaced. The previous
sleep-accel-only weights stay in git history.

## 4. On the user's own nights (replay harness)

`staging_audit/replay_new.py` (fixed clock) was run over the three exported nights. It
replays the whole live path: HR+motion stager, autonomic rescoring, deep corroboration,
hypnogram constraint and stage hold. "final" is what the controller would have acted on.
"model" counts only the ticks the learned stager itself produced, before the
post-processing. Deep and REM are % of sleep; wake is % of ticks.

| night | weights | wake % | light % | deep % | REM % | model-only deep % | model-only REM % |
|---|---|---|---|---|---|---|---|
| 2026-09-19 | bundled | 16.2 | 62.8 | 4.2 | 33.0 | 8.9 | 53.4 |
| 2026-09-19 | BIDSleep | 31.9 | 61.6 | 10.4 | 28.0 | 19.7 | 39.2 |
| 2026-09-20 | bundled | 7.7 | 56.5 | 5.6 | 37.9 | 14.0 | 41.0 |
| 2026-09-20 | BIDSleep | 13.5 | 49.4 | 7.3 | 43.3 | 19.3 | 43.7 |
| 2026-09-21 | bundled | 8.4 | 69.7 | 3.0 | 27.2 | 7.5 | 41.5 |
| 2026-09-21 | BIDSleep | 14.2 | 74.8 | 10.7 | 14.5 | 20.3 | 19.3 |

* **Deep.** The stager's own deep share goes from 7.5-14% to 19-20%, in line with the
  corpus. The controller-facing share only reaches 7-11%, so the rest of the gap comes from
  the post-processing downstream of the model, not from the weights.
* **REM.** REM falls on two nights. 2026-09-20 stays at about 43%.
* **Wake.** The extra wake sits almost entirely in the first hour. The new model does not
  call sustained sleep until 74 / 28 / 18 min after bed entry; the bundled model did so at
  0 / 0 / 15 min. The recorded onset events were 84 and 102 min after bed entry on
  2026-09-19 and 2026-09-20, so the later calls fit the recorded onsets better.
