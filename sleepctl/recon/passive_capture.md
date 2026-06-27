# Passive capture of the Pod raw stream — RISK-FREE, NON-ROOTING runbook

**Goal:** find out — at **zero risk to the Pod** — whether its raw sensor upload to
`raw-api-upload.8slp.net:1337` is plaintext (decodable, a win) or TLS (a dead end). The Pod is
**never touched, modified, or redirected**; it keeps talking to Eight Sleep normally. You only
observe a *copy* of its packets. Worst case: you learn it's encrypted and stop, having risked
nothing.

> This is strictly safer than a DNS redirect (which diverts the Pod's upload to your server).
> Passive capture does not interrupt the Pod's cloud sync at all.

## Why this carries no device risk
Device risk only comes from modifying the device (rooting, firmware, teardown). Passive packet
capture is pure network observation on **your** LAN gear. Nothing is installed on the Pod; there
is nothing to revert and nothing to brick.

## How to capture (pick one)
1. **Managed-switch port mirror / SPAN (best).** Put the Pod's hub on a switch port, mirror it to
   a port running a Raspberry Pi / laptop, and capture there. The Pod is unaware.
2. **Passive inline tap.** A cheap network tap between the hub and the router, feeding a capture box.
3. **LAN sniffing.** On a flat network, ARP-based observation from a box on the same segment.
   (Slightly less clean; the mirror/tap are preferred.)

Then capture only the upload host/port:
```
# resolve the host first (note the IP), then:
sudo tcpdump -i <mirror-iface> -s0 -w pod_raw.pcap 'host raw-api-upload.8slp.net or port 1337'
# let it run across a sleep session so batches accumulate
```

## The plaintext-vs-TLS GO/NO-GO
Open the capture and inspect the `:1337` payloads:
```
tshark -r pod_raw.pcap -Y 'tcp.port==1337' -T fields -e tcp.payload | head
```
- **TLS / ciphertext (NO-GO):** you'll see a TLS `ClientHello` (first bytes `16 03 01 …`) on the
  connection, and the application payload is high-entropy noise. Decode is impossible without the
  device's key (which needs root). **Stop here — you risked nothing.**
- **Plaintext-ish (GO):** the payload has visible structure / low entropy / readable framing.
  Extract the raw `:1337` application bytes to a file and hand them to the decoder:
```
# carve the raw payload bytes (one connection) to a blob, then:
python -c "from sleepctl.recon.frame_decoder import analyze; print(analyze(open('batch.bin','rb').read()).summary())"
```
`frame_decoder.analyze()` reports entropy (its own encrypted/plaintext call), a probable record
size, and a serialization guess. If it says *PLAINTEXT-ish*, proceed to decode (below).

## Decoding (only if plaintext)
The batch contains (from the Pod's device logs): **6 capacitance @ 2 Hz, 2 piezo/BCG @ ~500 Hz,
8 temperatures**. Use `frame_decoder.py`:
1. Find the framing (header/length/record size) with the structure heuristics.
2. Locate the **piezo channel** by its ~500 Hz cadence; cross-check decoded HR against the
   Tier-0 cloud HR at matching timestamps (free ground truth).
3. Run `heart_rate_from_bcg()` + `movement_index()` — the **sub-second movement index is the
   prize** (the fast arousal precursor the 60s cloud bins away).
4. Feed decoded samples into a `PodSensorSource` implementation (the existing `raw_capture.py`
   stub) → the controller consumes faster data with **zero controller changes**.

`python sleepctl/recon/frame_decoder.py` runs a synthetic self-test (no real capture needed) that
proves the analyzer + beat detector work end-to-end.

## Honest caveat
Pinning status of `:1337` is **unverified** (likely TLS, given the nonstandard port and that the
device-api uses DTLS). This runbook is the risk-free way to *find out*. If it's TLS, the only raw
path is rooting (out of scope) — and even then derived vitals are ~60s; only the raw waveform is
faster. If you want guaranteed fast data with no Pod dependency at all, use the wearable-fusion
path (`sleepctl/adapters/wearable.py`).
