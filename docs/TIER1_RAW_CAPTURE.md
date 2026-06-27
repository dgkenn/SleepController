# Tier 1 raw capture — feasibility verdict (researched June 2026)

**Question:** can we get sub-minute physiology from the Pod 2 by *passively* capturing its raw
sensor upload, **without rooting the device**, in a way that's fully reversible?

**Verdict: NO-GO without rooting.** Do not plan around it. Stay on the Tier 0 cloud path
(~60s) with the predictive/prophylactic prevention layer; that is the ceiling for a non-rooting
setup, and we already maximize it (15s polling of the ~30s device stream; minute-resolution
vitals are a hard floor — see below).

## Why (evidence)

The Pod uploads raw sensor batches to **`raw-api-upload.8slp.net:1337`** (a daemon "Frank"
batches piezo/capacitance/temperature and uploads). Community projects: opensleep, free-sleep,
ZeroSleep, ninesleep.

1. **Pinning status is genuinely unverified.** No public source documents whether port 1337 is
   plain TCP or TLS, or whether the Pod pins the cert. Any "it's not pinned" claim is unproven.
   (The well-documented Eight Sleep cert-pinning is on the *mobile-app → cloud* path —
   `auth/client/app-api.8slp.net` — a different path from the device upload.)
2. **Every documented redirect requires root.** The only known intercept is editing the Pod's
   on-device `/etc/hosts` to repoint `raw-api-upload.8slp.net` — which already presupposes a
   rooted Pod. A pure network-layer DNS redirect (your router answers for that host) is feasible
   *only* if the transport is unpinned plain TCP; if it's TLS-with-pinning the listener's cert is
   rejected and you get nothing. Which case holds is exactly the unverified fact.
3. **The batch wire format is undocumented.** No one has published a decode of the 1337 batches
   (protobuf? CBOR? custom binary?). Even with the bytes you'd be reverse-engineering a format
   from scratch.
4. **The only *proven* raw path is rooting** (SD-card patch per ZeroSleep/ninesleep, or UART/JTAG)
   and reading Frank's local Unix socket / a local REST API (free-sleep `:…/api/metrics/vitals`,
   ninesleep `:8000`). That is explicitly out of scope (no rooting).
5. **Even rooted, derived vitals are still ~60s.** The Pod firmware computes HR/HRV/RR per minute
   (free-sleep inserts vitals once every 60s). Only the **raw piezo waveform (~500 Hz)** is faster
   — and reaching it requires root + USART. So rooting buys faster *movement/BCG waveform*, not
   faster ready-made vitals.

## What this means for the product

- **~60s vitals is the floor everywhere** for a non-rooting setup. The faster-data dream does not
  survive the "no rooting" constraint.
- The genuine, unreachable-here win would be **sub-second movement/restlessness from the raw piezo
  waveform** (a strong immediate arousal precursor) — but only via rooting, which is ruled out.
- Therefore the awakening-prevention design correctly relies on **prediction on slow data**
  (`docs/AWAKENING_PREVENTION.md`), not faster reaction.

## Status of the code

`sleepctl/adapters/raw_capture.py` stays a **gated stub** (raises `NotImplementedError`). It is
left in place behind the `PodSensorSource` interface so that *if* the pinning/format questions are
ever resolved by the community for a non-root path, it can be implemented with zero controller
changes. Until then it is intentionally inert. No DNS redirect, listener, or decoder is shipped,
because none can be validated without the unverified facts above and a live device.
