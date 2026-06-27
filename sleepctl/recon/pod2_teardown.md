# Tier 2 — On-device root (REFERENCE ONLY, last resort)

**This procedure is NOT executed by default and is NOT implemented in runtime code.**
`sleepctl/adapters/local_frank.py` (`LocalFrankSource`) is a gated stub that raises
`NotImplementedError` until every gate below is satisfied. This document exists so the
option is understood and, if ever needed, performed safely and reversibly.

Tier 2 yields the highest-fidelity data (the on-device **Frank local API** `GET /variables`
plus a tap of the **STM32 sensor subsystem over USART**), but it requires rooting the Pod.

## ⚠️ Hardware compatibility — the Pod 2 / Pod Pro CANNOT be rooted (researched June 2026)

The community rooting projects **do not support the Pod 2 (Pod Pro)**. This is a hard
hardware limit, not a missing feature:

- **free-sleep** (the maintained project that consolidates ZeroSleep / OpenSleep /
  ninesleep) lists compatibility as **"Pod 1 & 2: ❌ NOT COMPATIBLE"** — supported models
  are **Pod 3** (Variscite-SOM Linux Hub, with/without microSD), **Pod 4**, and **Pod 5**.
  The first-hand Pod-3 write-ups concur verbatim: *"Pod 1 and Pod 2 are not compatible."*
- Every method below depends on the Pod 3+ Hub's **Variscite VAR-SOM-MX8M** Linux SoC
  whose **stock firmware lives on a microSD** that the daughterboard reloads on factory
  reset. The **Pod 2 / Pod Pro Hub uses a different, older compute design** for which **no
  published, validated, reversible root exists**.
- Because there is no validated method, there is also **no validated
  factory-reset-to-stock path** for a rooted Pod 2. The "impossible to brick … completely
  reversible" guarantee (see Reversibility, below) is **specific to Pod 3 (no-SD) / 4 / 5**
  and does **not** transfer to this hardware.

**Conclusion for this project:** on a Pod 2, Tier 2 is effectively unavailable and
`LocalFrankSource` remains a permanent stub. The controller runs fully on **Tier 0 (cloud
API)**, which is confirmed working — so the Tier 2 *necessity* gate is not met regardless.
The Pod-3 reference method below is retained only for the case of a future Pod 3/4/5; it
**does not describe the user's Pod 2**.

Sources: github.com/throwaway31265/free-sleep (+ INSTALLATION.md),
blog.adamschaal.com/posts/2025-12-16-rooting-eight-sleep, blopker.com/writing/04-zerosleep-1,
github.com/bobobo1618/ninesleep, github.com/LiamSnow/opensleep.

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

## Reference method (Pod 3+ ONLY — ninesleep / ZeroSleep / OpenSleep / free-sleep)

> Applies to **Pod 3 / 4 / 5**, not the Pod 2 (see compatibility note above). Two
> variants exist: (a) **microSD** — open the Hub, pull the SD, inject an SSH key into the
> rootfs; (b) **JTAG/UART U-Boot** — FTDI serial adapter on the JTAG header, interrupt
> boot at 921600 baud, edit the U-Boot env. SSH then listens on port **8822**.

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

## Restore-to-stock (Pod 3+ with microSD)

Reflash the verified byte-for-byte microSD image and factory-reset (hold the back button on
power-up until the light flashes green; the daughterboard reloads stock firmware from the
SD). Because the only change was one `authorized_keys` line on the SD rootfs, restoring the
original image returns the Pod to factory firmware. **Note:** an official OTA update will
also wipe the modification. This restore path is **not validated for the Pod 2**, which has
no supported root method to reverse in the first place.
