# Tier 2 — On-device root (REFERENCE ONLY, last resort)

**This procedure is NOT executed by default and is NOT implemented in runtime code.**
`sleepctl/adapters/local_frank.py` (`LocalFrankSource`) is a gated stub that raises
`NotImplementedError` until every gate below is satisfied. This document exists so the
option is understood and, if ever needed, performed safely and reversibly.

Tier 2 yields the highest-fidelity data (the on-device **Frank local API** `GET /variables`
plus a tap of the **STM32 sensor subsystem over USART**), but it requires rooting the Pod.

## The triple-gate (all three must pass, in order)

### 1. Necessity
Tier 0 (cloud `intervals`) **and** Tier 1 (non-invasive raw capture) have both been built
and demonstrably **cannot** meet the granularity actually required. If Tier 1 succeeds,
Tier 2 is **not** done.

### 2. Reversibility — 100% recoverable, proven *before* any change
- Take a **complete byte-for-byte image of the microSD** (the rootfs lives there; the Pod
  loads it on factory reset).
- **Prove the restore works first:** reflash the *untouched* image and confirm a clean
  stock boot **before making any modification.**
- The root method edits only the SD rootfs — **no eFuse/OTP burning, no
  bootloader-of-no-return** — so with a verified image the firmware state is always
  restorable to factory.

### 3. Minimality
Make the **smallest possible change**: add a single `authorized_keys` entry, nothing else.
No firmware replacement, no disabling of stock services unless strictly required. Keep the
change trivially removable and document the removal.

## Reference method (mirrors Pod 3 projects: ninesleep / ZeroSleep / OpenSleep)

1. Image and verify-restore the microSD (gate 2) **first**.
2. Inject one SSH public key into the rootfs `authorized_keys` (gate 3).
3. Trigger factory reset so the modified rootfs loads; SSH in over LAN.
4. Read **Frank** locally (`GET /variables`) and tap the **STM32 USART** raw stream;
   expose them through `LocalFrankSource` (same `PodSensorSource` interface → no controller
   change).

## Honest residual risk

The **firmware** path is 100% reversible via the verified image. The one risk no software
backup covers is **physical teardown** of a liquid-cooled hub (ribbon/connector or
coolant-line damage). It is minimized with careful disassembly and reseat checks but
**cannot be reduced to literally zero** — which is exactly why the *necessity* gate must be
taken seriously and Tiers 0/1 exhausted first.

## Restore-to-stock

Reflash the verified byte-for-byte microSD image and factory-reset. Because the only change
was one `authorized_keys` line on the SD rootfs, restoring the original image returns the
Pod to factory firmware.
