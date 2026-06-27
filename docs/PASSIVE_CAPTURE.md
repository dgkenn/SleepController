# Riskless paths to faster data

The Eight Sleep cloud floor is ~60s for vitals (a rooted Pod is no faster — its firmware derives
HR/HRV per minute; only the raw piezo waveform is faster, and that needs root). Device risk only
ever comes from touching the device. So there are exactly two **zero-device-risk** ways to chase
faster, arousal-relevant physiology:

## 1. Passive capture (free to *try*, only pays off if plaintext)
Observe — never redirect — the Pod's raw upload to `raw-api-upload.8slp.net:1337` via a managed-
switch port mirror or a passive tap, and decode it **only if it's plaintext**. The Pod is never
modified and keeps syncing to Eight Sleep normally; the worst case is "we learn it's encrypted
and stop." See `sleepctl/recon/passive_capture.md` for the runbook and the plaintext-vs-TLS
go/no-go, and `sleepctl/recon/frame_decoder.py` for the analyzer + BCG beat detector (run it for a
synthetic self-test). Pipeline: **capture → analyze → decode → feed a `PodSensorSource`** (the
gated `raw_capture.py` stub) with zero controller changes.

Honest caveat: pinning on `:1337` is unverified (likely TLS). This path is the risk-free way to
find out; if encrypted, it's a dead end short of rooting (out of scope).

## 2. Wearable fusion (GUARANTEED fast data, zero Pod dependency)
A separate sensor — a $20 BLE chest strap / wrist HR + accelerometer, or a bedside non-contact
radar — gives **sub-second movement and faster HR** with no risk to the Pod (it's a different
device). `sleepctl/adapters/wearable.py` provides `FusedPodSensorSource`, which overlays a fresher
wearable sample onto the Pod frame the controller already consumes — so the precursor / wake-risk
detectors see fast movement/HR with **no controller changes**, falling back to the Pod when the
wearable is stale or absent. A standard BLE Heart-Rate-Service reader (`BLEHeartRateSource`,
lazy-`bleak`) is included for real hardware; the simulated source backs the tests.

**Recommendation:** wearable fusion is the reliable win (movement is the highest-value fast
precursor, and it's guaranteed to work). Passive capture is a free side-bet worth running once
the Pod is back online — if `:1337` happens to be plaintext, it's a bonus raw-waveform source.
