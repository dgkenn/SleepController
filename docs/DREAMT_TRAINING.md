# Training the beat-interval sleep stager on DREAMT

The shipped stager sees only heart rate and motion (PhysioNet sleep-accel, Apple Watch,
kappa 0.44). The Verity Sense also streams beat-to-beat intervals, and the staging
literature on those intervals reaches kappa 0.6-0.75. Training that model needs beat
intervals recorded alongside polysomnography. DREAMT (PhysioNet, v2.2.0) is 100 such
nights: an Empatica E4 on the wrist (PPG, IBI, accelerometer) next to a clinical PSG.

DREAMT is **credentialed** data under the PhysioNet Restricted Health Data Use Agreement:
the raw files and anything reduced from them stay on your machine and are never committed
or published. Only the trained weight files (`*.json` under `weights/`) go into the repo.

## 0. The automatic route (nothing to run)

Once access is live, the box does all of this by itself: `scripts/dreamt_pipeline.py`, launched
by the watchdog at most once a day, at any hour, at below-normal priority (never two at once)
until a model is installed. To stop a run, end the `python ... dreamt_pipeline.py` process in
Task Manager; the next run skips every participant already reduced.

- It authenticates with the physionet.org entry already in your `.netrc` (it never reads out,
  prints or asks for the password) and stops with a status if there is none or if the file
  server answers 403.
- A DREAMT ZIP already in Downloads (or on D:) is read in place instead of downloading.
- Otherwise it reads the project ZIP on physionet.org in place, over HTTP range requests:
  only the `data_64Hz` members' compressed bytes cross the network (a fraction of the ~15 GB
  unpacked), nothing raw is written to disk, and each member's CRC-32 is checked as it streams.
  A dropped connection resumes at the same byte. If the server won't do range reads it falls
  back to downloading each `data_64Hz` CSV into `D:\sleepctl-cache\dreamt` (outside the repo),
  checking it against `SHA256SUMS.txt`, reducing it and deleting it.
- Three participants are reduced at once (`--workers N` or `SLEEPCTL_DREAMT_WORKERS` to
  change it). Each worker streams one night and holds ~35 MB, so three add ~100 MB of RAM
  while overlapping network waits and parsing. An 8-hour night reduces in ~4 s of CPU.
- It trains, and installs the four HRV weight files into `.run\staging_weights` (local,
  git-ignored) only when the held-out 4-class kappa clears 0.45 and beats the bundled model.
  The daemon picks them up at its next restart.
- Progress, counts and scores (never data) appear in the health snapshot under `dreamt`.

The manual steps below do the same by hand.

## 1. Get access

1. Log in to PhysioNet, open <https://physionet.org/content/dreamt/2.2.0/> and complete the
   credentialing steps it lists (sign the DUA; CITI training if asked). Until the project
   page shows the file list, every download answers `403 Forbidden`.
2. Check that the login itself works in a browser first. A wrong password is reported by
   the site as "Please enter a correct username and password"; the file endpoints only
   ever say 403.

## 2. Download only what the trainer needs (~15 GB)

Only the 64 Hz wearable folder and the participant table are used. On the Windows box
(PowerShell, with wget from Git for Windows or `winget install wget`):

```
cd D:\dreamt
wget -r -N -c -np -nH --cut-dirs=3 --user <physionet-username> --ask-password -A "*.csv" https://physionet.org/files/dreamt/2.2.0/data_64Hz/
wget -N --user <physionet-username> --ask-password https://physionet.org/files/dreamt/2.2.0/participant_info.csv
```

That leaves `D:\dreamt\data_64Hz\S002_whole_df.csv ...` (one ~150 MB CSV per participant).
The AWS route on the project page works the same once your AWS account is linked.

### Or: the ZIP, without unpacking it

The project page's "Download the ZIP file" (20.3 GB) also works, and needs no wget: download
it in the browser on the box, then point the reducer AT the ZIP. It streams each participant's
64 Hz CSV straight out of the archive, so the 113.7 GB unpacked copy never touches the disk:

```
python scripts\dreamt_reduce.py --data-dir D:\Downloads\dreamt-2.2.0.zip --out D:\dreamt\reduced
```

## 3. Reduce (streams each file; a few seconds per participant)

```
python scripts\dreamt_reduce.py --data-dir D:\dreamt --out D:\dreamt\reduced
python scripts\dreamt_reduce.py --out D:\dreamt\reduced --verify
```

Per participant this writes `<ID>_heartrate.txt`, `<ID>_labeled_sleep.txt`,
`<ID>_ibi.txt` and `activity\<ID>_activity.txt` in the formats the trainer already reads.
`--limit 5` reduces a handful first; `--skip-existing` resumes.

## 4. Train (participant-grouped cross-validation; 30-90 minutes)

```
python scripts\train_dreamt.py --data-dir D:\dreamt\reduced --out sleepctl\ml\sleep_staging\weights --cache-dir D:\dreamt\cache
```

`--quick` runs a two-model grid for a smoke test. The run prints, per variant, the
unsmoothed and HMM-smoothed 4-class kappa, wake kappa / precision / recall, and the
per-night deep / onset / WASO errors, then writes:

| file | what |
| --- | --- |
| `weights/wake_hrv.json`, `weights/stage4_hrv.json` | HR + beat intervals + scale-free motion (the live path) |
| `weights/wake_hrvonly.json`, `weights/stage4_hrvonly.json` | HR + beat intervals (no motion) |
| `weights/hmm_dreamt.json` | transitions and priors estimated on DREAMT (the shipped `hmm.json` is untouched) |
| `cv_report_dreamt.json` | the scores, next to `cv_report.json` |

Commit the five weight files and the report. Nothing else from the run belongs in git.

## 5. What happens on the box

`sleepctl.ml.sleep_staging.infer.SleepStager.load` picks the new files up when they exist.
`predict(..., ibi_samples=...)` scores the HRV variant whenever at least 200 beat intervals
fall in the trailing ten minutes (the PPI stream really flowing), with motion when the
accelerometer is streaming too, and falls back to today's HR / HR+motion models otherwise.
Each estimate records which variant produced it (`StageEstimate.variant`), and the night
export's staging plausibility audit (`staging_consistency`) says whether the new calls hold
up against breathing, heart rate and movement.

## Why the wrist corpus transfers to an upper-arm band

Every HRV feature is computed from intervals, which do not depend on where the band sits.
Motion is used only through scale-free features (each night's own distribution), because
absolute counts from a 32 Hz wrist E4 and a 52 Hz upper-arm Verity are not comparable.
Heart-rate features are already normalised per night.

## Larger sources later

BDSP's Human Sleep Project (26,200 PSGs with ECG) is the next step for the interval model:
ECG-derived intervals with sleep-lab labels at a scale DREAMT cannot offer. It needs a
BDSP account with a linked AWS account, a signed DUA and an S3 access point; the reducer
would read EDF rather than CSV, and the same trainer applies.
