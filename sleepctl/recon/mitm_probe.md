# Tier 1 — Non-invasive raw capture (network spike)

**Goal:** obtain the Pod 2's own raw/high-resolution sensor stream **without modifying the
device at all.** This tier never roots, reflashes, or opens the hub. It cannot brick the
Pod and is instantly reversible — remove one DNS override and the Pod resumes normal cloud
operation.

> This is a reconnaissance/spike protocol, not runtime code. The runtime consumes whatever
> it produces via `sleepctl/adapters/raw_capture.py` (`RawCaptureSource`).

## Why this is possible

The Pod's firmware ("Frank") uploads its **raw sensor stream** to
`raw-api-upload.8slp.net`. If we can observe or redirect that upload, we get the raw piezo/
HR/breath data the Pod already emits — at higher resolution than the cloud `intervals` API
exposes — purely at the network layer.

## Setup

1. **Controlled gateway.** Put the Pod's hub behind a network gateway you control
   (Raspberry Pi acting as router/AP, or a router with custom DNS). All hub traffic must
   route through it.
2. **DNS redirect.** Point `raw-api-upload.8slp.net` at a local capture server (e.g. via
   the gateway's DNS / Pi-hole local record). The capture server terminates TLS with a CA
   you control and appends decoded frames to a local JSONL queue that `RawCaptureSource`
   reads.
3. **TLS interception.** Run `mitmproxy` (or PCAPdroid for mobile-side capture) to inspect
   the upload. Install the proxy CA where needed.

## GO / NO-GO — certificate pinning

The decisive question is whether the raw-upload connection is **certificate-pinned**.

- **NO-GO (pinned):** the hub refuses the intercepting CA → we cannot read the payload.
  Fall back to Tier 0 (cloud minute-level data). Even here we still learn upload cadence
  and volume. **Do not escalate to Tier 2 unless raw data is genuinely necessary.**
- **GO (not pinned):** capture the payload, then reverse-engineer its framing into
  per-sample fields (HR, HRV, breath, movement, stage). Validate decoded values against
  the Tier 0 `intervals` numbers for the same night. Feed decoded frames into the JSONL
  queue → the controller consumes them with **zero code change.**

## Guarantees

- **No device modification.** Stock firmware untouched; the hub just talks to the network.
- **Fully reversible.** Delete the DNS record → normal operation restored immediately.
- **No bricking risk.** Nothing on the device is altered.

## Decoder status

The decoder for the proprietary upload payload is a **TODO that depends on the pinning
result**. `RawCaptureSource.capabilities()` reports `blocked_by: tls_certificate_pinning`
until the GO/NO-GO test is run.
