# Training the beat-interval sleep stager on MESA

DREAMT gives the beat-interval (HRV) model 100 nights. MESA Sleep, from the Multi-Ethnic Study
of Atherosclerosis, has 2,056 overnight home polysomnographies scored by one central
reading centre. Every recording includes a finger **pulse-oximeter plethysmogram** (the
`Pleth` channel), and most participants also wore a wrist Actiwatch that week. It is the
standard corpus for PPG-based sleep staging. PPG is what the Verity Sense measures, so beat
intervals taken from the MESA pleth are the closest match to the live signal of any public
dataset.

MESA is distributed by the NSRR (sleepdata.org) under a **data use agreement**. The raw files,
and anything reduced from them, stay on your machine and are never committed or published.
Only the trained weight files go into the repo.

## 1. What you do (once)

1. **Request access.** Sign in at <https://sleepdata.org> (create an account if you don't
   have one), open <https://sleepdata.org/datasets/mesa> and click **Request Data Access**.
   The request form asks for your details, a short description of the intended use (for
   example: "training a personal sleep-stage classifier from pulse-oximeter beat intervals;
   non-commercial; data stays on one machine"), and your signature on the data use agreement.
   The NSRR reviews it. Approval takes days to weeks and arrives by email. Neither Claude nor
   the box can submit or sign this for you.
2. **Copy your token.** Once you are approved, open <https://sleepdata.org/token> (signed in)
   and copy the token shown there. The same token works for every dataset you are approved
   for, and it does not expire unless you reset it.
3. **Put the token on the box.** Store it in a file under your user profile, not in the repo.
   In PowerShell on the box:

   ```
   notepad $HOME\nsrr_token.txt
   ```

   Paste the token as the only line, then save and close. The pipeline also accepts
   `%USERPROFILE%\.nsrr_token`, `D:\sleepctl-cache\nsrr_token.txt`, a path in
   `SLEEPCTL_NSRR_TOKEN_FILE`, or the token itself in `SLEEPCTL_NSRR_TOKEN` / `NSRR_TOKEN`.
   `deploy\.env` works for the variable because it is git-ignored, but the file keeps the
   token out of every other process's environment. Don't paste the token into a command
   line, because PowerShell saves command history.

That is all. The watchdog (once `Ensure-MesaModel`, below, is wired in) notices the token
and starts the pipeline by itself.

To stop using it: delete the token file, or reset the token at sleepdata.org/token.

## 2. What the box then does (nothing to run)

`scripts/mesa_pipeline.py`, launched by the watchdog at most once a day at below-normal
priority, and never while it or the DREAMT pipeline is already running:

- **Checks access, and stops with a status if something is missing.** It verifies that the
  token exists, that NSRR accepts it (`/api/v1/account/profile.json`), that the file list has
  the documented layout, and that one annotation file actually downloads (not a sign-in
  page). Each failure is written to `.run\mesa.status.json` with an explanation (`stage:
  blocked` or `failed`), and the pipeline tries again the next day.
- **Never exposes the token.** The token travels in NSRR's download URLs, so every message
  that could quote a URL is scrubbed before it reaches the status file or the logs. The
  status holds only counts, stages and scores.
- **Picks a subset.** `--limit` defaults to 200 records, spread evenly over the corpus (see
  the sizes below). Set `SLEEPCTL_MESA_LIMIT` in `deploy\.env` to change it without editing
  the watchdog; 0 takes all 2,056.
- **Streams one record per worker.** For each record it downloads the EDF (resumable, and
  checked against the MD5 that the NSRR file API publishes) plus the staging XML and the
  actigraphy CSV into `D:\sleepctl-cache\mesa\raw`. It reduces them with
  `scripts/mesa_reduce.py`, then **deletes** the raw files before the worker takes the next
  record. Three workers run at once (`--workers N` or `SLEEPCTL_MESA_WORKERS` changes this),
  so at most about three EDFs are on disk at any time. A worker skips a download if it would
  leave less than 3 GB free.
- **Resumes.** A record whose `<ID>_ibi.txt` exists is done. A record that can never be used
  (no pleth channel, too few scored epochs, too few clean beats) goes into `skipped.json` and
  is not downloaded again. A transient failure is retried once per run.
- **Trains** with the existing `scripts/train_dreamt.py` trainer, which uses the same
  participant-grouped cross-validation and HMM smoothing. It writes the four HRV weight files
  plus `cv_report_mesa.json` into the work folder.
- **Installs only a better model.** The weights are copied to `.run\staging_weights`
  (local, git-ignored), where the stager prefers them, only if the held-out, HMM-smoothed
  4-class kappa clears **every** current model:
  - `dreamt_pipeline._better_than_bundled`: the 0.45 floor, plus the shipped HR / HR+motion
    models' kappa from `cv_report.json` and `cv_report_bidsleep.json`;
  - a model already installed in `.run\staging_weights` (for example DREAMT's), with its
    score taken from that pipeline's status file. If the score of an installed model is
    unknown, the pipeline does not overwrite it.
- **Finishes.** It writes `.run\mesa.done`, so the watchdog does not start the pipeline again,
  whether or not the model was installed. Delete that file to force a retrain.

The work folder is `D:\sleepctl-cache\mesa` (or `%LOCALAPPDATA%\sleepctl-cache\mesa` when
there is no D:). The pipeline refuses to use a work folder inside the repository.

### Expected sizes and times

| | default 200 | `--limit 300` | all 2,056 |
| --- | --- | --- | --- |
| EDF download (~190 MB per record) | ~37 GB | ~56 GB | ~385 GB |
| XML annotations + actigraphy CSVs | ~0.4 GB | ~0.6 GB | ~3-5 GB |
| `--beats rpoints` instead of EDFs (ECG R-point CSVs, ~1-2 MB each) | ~0.3 GB | ~0.5 GB | ~3 GB |
| peak raw disk use (3 workers) | ~0.6-1 GB | ~0.6-1 GB | ~0.6-1 GB |
| reduced output (kept) | ~0.25 GB | ~0.4 GB | ~2.5 GB |
| download at 10 MB/s (80 Mbit/s) | ~1 h | ~1.6 h | ~11 h |
| download at 3 MB/s | ~3.5 h | ~5 h | ~36 h |
| reduce CPU per record (numpy, 5-min chunks, <100 MB RAM per worker) | ~5-10 s | | |
| trainer RAM (~16 KB per epoch, ~1,200 epochs per night) | ~4 GB | ~6 GB | ~40 GB (does not fit) |
| featurising (~60 ms per epoch per core, 2 jobs) | ~2 h | ~3 h | |
| plus cross-validation and fits | ~1-2 h | ~2-3 h | |

The trainer keeps every epoch's features in memory as a Python dict of ~240 floats, about
16 KB per epoch (measured on a synthetic reduced night). A MESA night has ~1,100-1,300
epochs, so the default of 200 nights needs ~4 GB. Use `--limit 300` or more only with 8 GB
or more free. The whole corpus would need a streaming trainer, which does not exist yet.

The totals in the table are the NSRR's published figure for the corpus (385 GB for 2,056
EDFs). The dry run prints the exact byte count from the file list (`transfer_gb` in the
status):

```
python scripts\mesa_pipeline.py --dry-run
```

## 3. The same by hand

With a local copy in the NSRR layout (for example from `nsrr download mesa/polysomnography`
using the nsrr gem, which prompts for the same token):

```
python scripts\mesa_reduce.py --data-dir D:\nsrr\mesa --out D:\mesa\reduced --skip-existing --delete-edf
python scripts\mesa_reduce.py --out D:\mesa\reduced --verify
python scripts\train_dreamt.py --data-dir D:\mesa\reduced --out D:\mesa\weights --cache-dir D:\mesa\cache
```

## 4. How the reduction works

`scripts/mesa_reduce.py` writes, per record, the same four files as `dreamt_reduce.py`
(`<ID>_heartrate.txt`, `<ID>_labeled_sleep.txt`, `<ID>_ibi.txt`,
`activity\<ID>_activity.txt`), so the trainer and `sleepctl.ml.sleep_staging.dataset` read
them unchanged. `<ID>` is the NSRR record name, for example `mesa-sleep-0001`.

- **EDF.** A minimal reader in pure Python and numpy handles plain EDF and EDF+C. It reads
  the pleth channel record by record in 5-minute chunks, so a 10-hour 256 Hz night is never
  in memory all at once. A truncated file is read as far as it goes. BDF and non-EDF content
  (for example an HTML sign-in page) are rejected as layout errors.
- **Beats.** The detector finds systolic peaks with the Elgendi two-moving-average method:
  it band-passes the pleth (~0.5-8 Hz, as a difference of moving averages), clips it and
  squares it, then compares a 111 ms average against a 667 ms average. Peaks get sub-sample
  parabolic refinement and a 270 ms refractory period.
- **Artifact rejection.** A 2 s window is dropped if it is flat, clipped at the rails, or
  more than 2.5x (or less than 0.15x) the segment's typical amplitude. A beat is dropped if
  its shape correlates below 0.8 with the segment's median pulse, or if its amplitude falls
  outside 0.35-3.5x the local median. An interval is kept only between two accepted
  neighbouring beats, only if it is 300-2000 ms, and only if it is within 25% of the median
  of its time-local neighbours (this catches missed and ectopic beats). On synthetic PPG
  (45-110 bpm, noise, flat-line and movement segments, an ectopic beat) the tests find every
  beat within 60 ms, with a median interval error under 5 ms and nothing wrong kept.
- **Heart rate** is sampled at 1 Hz: 60000 / the mean clean interval over the trailing 5 s.
- **Stages** come from the NSRR XML (`EventType` `Stages|Stages`; an event can span several
  epochs). If that file is missing, the Profusion XML (`<SleepStages>`) is used instead.
  Wake -> 0, N1 -> 1, N2 -> 2, N3 and N4 -> 3, REM -> 5, unscored and movement -> -1.
- **Actigraphy** is aligned with `overlap/mesa-actigraphy-psg-overlap.csv`: the `line` at PSG
  start is epoch 0, corrected by any `linetime` vs `starttime_psg` offset. Off-wrist epochs are
  dropped. Actiwatch counts are not accelerometer counts, so they go into `pim` (and `pmax`)
  and only the trainer's scale-free motion features, which rank each night's own
  distribution, can use them.

## 5. NSRR mechanics this relies on

From the sleepdata.org API documentation
(<https://github.com/nsrr/sleepdata.org/wiki/api-v1-datasets>) and the nsrr gem source:

| call | URL |
| --- | --- |
| is the token valid | `GET https://sleepdata.org/api/v1/account/profile.json?auth_token=T` returns `{"authenticated": true, ...}` |
| list a folder (immediate children only) | `GET https://sleepdata.org/api/v1/datasets/mesa/files.json?path=P&auth_token=T` returns `file_name`, `full_path`, `is_file`, `file_size`, `file_checksum_md5` |
| download a file | `GET https://sleepdata.org/datasets/mesa/files/a/T/m/<client>/<full_path>` |

Files used: `polysomnography/edfs/mesa-sleep-NNNN.edf`,
`polysomnography/annotations-events-nsrr/mesa-sleep-NNNN-nsrr.xml` (or
`annotations-events-profusion/...-profusion.xml`), `actigraphy/mesa-sleep-NNNN.csv`,
`overlap/mesa-actigraphy-psg-overlap.csv`, and optionally
`polysomnography/annotations-rpoints/mesa-sleep-NNNN-rpoint.csv` (`--beats rpoints`). None of
this could be checked from here without an approved account, so the pipeline treats any
difference as a reported status rather than a crash.

**TLS.** In September 2026, sleepdata.org sent only its leaf certificate, without the Sectigo
intermediate. Browsers and Windows fetch the missing intermediate themselves, but Python's
default verifier does not. The pipeline tries three things in order: the default verifier,
then `truststore` (the operating system's verifier, installed by `Ensure-MesaModel`), then a
CA bundle made of certifi's roots plus the intermediate named in the server certificate's
AIA field. Verification is never turned off. If all three fail, the status says so.

## 6. Watchdog wiring (to add to `scripts/windows-watchdog.ps1`)

Define this next to `Ensure-DreamtModel`, and call `Ensure-MesaModel` on the line after
`Ensure-DreamtModel` in the supervise loop:

```powershell
function Ensure-MesaModel {
    # Hands-off MESA staging model (scripts\mesa_pipeline.py): check NSRR access with the user's
    # own token, stream + reduce + train, install only if it beats every shipped and installed
    # model. Nothing happens until the user has put an NSRR token on the box. Started at most
    # once a day, at below-normal priority, never while it or the DREAMT pipeline is running
    # (both train in memory), and never again once a run has trained (.run\mesa.done). The
    # pipeline never prints or logs the token, keeps the data OUTSIDE the repo, and reports
    # progress to .run\mesa.status.json as counts and scores only.
    if (Test-Path (Join-Path $run "mesa.done")) { return }
    $script = Join-Path $Root "scripts\mesa_pipeline.py"
    if (-not (Test-Path $script)) { return }
    # The scheduled task may not run under the user's profile; the repo lives in it, so look
    # for the token file there too (only its existence is checked here, never its content).
    $prof = $null
    if ($Root -match '^([A-Za-z]:\\Users\\[^\\]+)\\') { $prof = $Matches[1] }
    $hasToken = [bool]($env:SLEEPCTL_NSRR_TOKEN -or $env:NSRR_TOKEN -or $env:SLEEPCTL_NSRR_TOKEN_FILE)
    foreach ($h in @($HOME, $env:USERPROFILE, $prof)) {
        if (-not $h) { continue }
        foreach ($n in @("nsrr_token.txt", ".nsrr_token", ".nsrr-token")) {
            if (Test-Path (Join-Path $h $n)) { $hasToken = $true }
        }
    }
    if (Test-Path "D:\sleepctl-cache\nsrr_token.txt") { $hasToken = $true }
    if (-not $hasToken) { return }
    try {
        $running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -like "*mesa_pipeline.py*" -or $_.CommandLine -like "*dreamt_pipeline.py*" }
        if ($running) { return }
    } catch { }
    $last = Join-Path $run "mesa.lastrun"
    if ((Test-Path $last) -and (((Get-Date) - (Get-Item $last).LastWriteTime).TotalHours -lt 24)) { return }
    $depMarker = Join-Path $run "mesa-deps.ok"
    if (-not (Test-Path $depMarker)) {
        & $py -c "import requests, numpy, sklearn, truststore" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Log "installing requests/numpy/scikit-learn/truststore for the MESA pipeline (one-time)"
            & $py -m pip install --quiet --disable-pip-version-check requests numpy scikit-learn truststore 2>&1 | Out-Null
            & $py -c "import requests, numpy, sklearn, truststore" 2>$null
        }
        if ($LASTEXITCODE -eq 0) { Set-Content -Path $depMarker -Value "ok" -Encoding ASCII }
        else { Log "WARN: MESA pipeline dependencies not importable yet; will retry"; return }
    }
    if ($prof) { $env:SLEEPCTL_NETRC_HOME = $prof }
    Set-Content -Path $last -Value (Get-Date -Format o) -Encoding ASCII
    Log "starting the MESA staging pipeline in the background (daily until it has trained)"
    try {
        $p = Start-Process -FilePath $py -ArgumentList @("`"$script`"") -WorkingDirectory $Root `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $run "mesa.log") `
            -RedirectStandardError (Join-Path $run "mesa.err.log")
        try { $p.PriorityClass = 'BelowNormal' } catch { }
    } catch {
        Log "WARN: could not start the MESA pipeline: $_"
    }
}
```

Two optional companions:

- In `Ensure-DreamtModel`, also treat a running `*mesa_pipeline.py*` as busy, so the two
  pipelines never train at the same time:
  `Where-Object { $_.CommandLine -like "*dreamt_pipeline.py*" -or $_.CommandLine -like "*mesa_pipeline.py*" }`.
- The health snapshot publishes only `dreamt.status.json` today
  (`dashboard/api/app/health_snapshot.py`, `_dreamt_block`). A `mesa` block that reads
  `.run\mesa.status.json` and keeps `stage, source, n_records, of, reduced, failed, skipped,
  transfer_gb, gb_downloaded, error, last_error, updated, started, finished, scores, verdict,
  participants` would make remote progress visible in the same way.
