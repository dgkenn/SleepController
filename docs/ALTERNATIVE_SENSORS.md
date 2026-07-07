# Alternative physiology capture, independent of Eight Sleep's paywalled cloud (researched July 2026)

**Question:** the Pod 2 Pro has no Autopilot subscription, so `docs/TIER1_RAW_CAPTURE.md` (own raw
sensor upload) and `docs/PASSIVE_CAPTURE.md`/`sleepctl/recon/*` (network intercept) are the
Eight-Sleep-specific paths, and both dead-end without rooting (which Pod 2 hardware doesn't
support at all — see `sleepctl/recon/pod2_teardown.md`). This doc surveys **non-Eight-Sleep**
capture methods that could feed `sleepctl/adapters/wearable.py` (`RealtimeWearableSource`) or a
new `PodSensorSource` implementation instead.

## 1. Under-mattress BCG mats (no wearable, closest fit to the Pod's own use case)

### Emfit QS — recommended
- **Data:** HR + breathing rate + movement every 4s, sleep-stage classification every 30s, HRV
  (RMSSD) every 3 min, plus bed-presence/restlessness events. Validated against ECG in a peer-
  reviewed study (JMIR Biomed Eng 2020).
- **Access:** Emfit Cloud API — a documented push API (Emfit POSTs each finished sleep period to
  your URL) and a pull API (poll live HR/RR/movement every 30s). Requires requesting a free
  developer/API key from Emfit; **no separate subscription fee for API access** beyond owning the
  device.
- **Subscription to read your own data: NO.** All analytics/HRV/trends are included with the
  device purchase; no forced recurring fee (unlike Whoop).
- **Price (2026):** QS+ACTIVE ≈ **$299 one-time** (shop-us.emfit.com); nothing ongoing beyond
  electricity.
- **Integration difficulty: EASY.** JSON over HTTP(S); an unofficial Node client
  (`samuelmr/emfit-qs`) and an npm package already exist as reference implementations.
- Sources: https://emfit.com/sleep-research/emfit-qs-cloud-api/ ,
  https://shop-us.emfit.com/products/emfit-qs ,
  https://healthcarediscovery.ai/emfit-qs-under-mattress-hrv-recovery-tracker/ ,
  https://biomedeng.jmir.org/2020/1/e16620

### Withings Sleep Analyzer — recommended (cheaper alternative)
- **Data:** HR, respiration rate, sleep stages, snoring, apnea/hypopnea indicators.
- **Access:** Withings public developer API (`developer.withings.com`) — standard free OAuth2
  app registration, `Sleep v2 Getsummary` endpoint returns the nightly data. This is the same
  free API long used by quantified-self hobbyists.
- **Subscription: NO** for the data itself — sleep stages, HR, snoring, breathing, apnea
  indicators are included with the device and the free API. "Withings+" (~in-app subscription)
  gates *coaching/programs*, not raw metric access.
- **Price (2026):** ~**$130–$165** one-time (varies by retailer/region).
- **Integration difficulty: EASY-MEDIUM.** Clean OAuth2 REST API; reference notebook exists
  (wearipedia). Caveat: only tracks one side of the bed per mat (buy two for a couple).
- Sources: https://developer.withings.com/api-reference/ ,
  https://wearipedia.readthedocs.io/en/latest/notebooks/withings_sleep.html ,
  https://www.tomsguide.com/wellness/sleep-tech/withings-sleep-analyzer-review

### Others
- **Google Nest Hub (Soli radar) Sleep Sensing:** still a free "preview" as of early 2026 (Google
  has repeatedly delayed folding it into paid Fitbit/Google Health Premium). **No public API or
  export exists at all** — data is locked inside the Google Home/Fitbit app with no documented
  way to extract it. Dead end for a self-hosted controller regardless of price.
  https://9to5google.com/2024/01/02/nest-hub-sleep-sensing-2024/
- **Sleep Number / Tempur smart beds:** no evidence of an accessible personal API; these are
  effectively closed ecosystems like Eight Sleep. Not investigated further (no promising lead
  found).
- **DIY load-cell/piezo mat + Raspberry Pi:** technically proven in research literature
  (piezoelectric ceramic / PVDF film mats under a mattress recover HR+RR via BCG — e.g. MDPI
  Sensors 2019 "Detection of Sleep Biosignals Using an Intelligent Mattress"), but no
  consolidated, maintained open-source project was found bundling sensor + ADC + decode +
  sleep-staging. **Integration difficulty: HARD** (signal processing from scratch, no vendor
  support, sleep-stage classification would need your own ML model). Only worth it if the goal is
  the DIY exercise itself.
  https://www.mdpi.com/1424-8220/19/18/3843 , https://pmc.ncbi.nlm.nih.gov/articles/PMC6767279/

## 2. Wearables — which let you read your OWN data without a mandatory subscription

| Device | Data | Access path | Subscription to read own data | Price | Difficulty |
|---|---|---|---|---|---|
| **Apple Watch → HealthKit** | HR, HRV (SDNN), respiratory rate, Apple's sleep stages | No Apple cloud/REST API exists — HealthKit is on-device only. Third-party apps (Health Auto Export, Health Export Pro/Kit) run on the iPhone and POST JSON to your own server. | **NO** (Apple side is free); the export app itself is a small one-time/annual fee (~$10–$50/yr, not Apple's) | Free if you own the Watch | Medium (needs an always-on iPhone automation, not a direct pull API) |
| **Withings (also a wearable line, not just the mat)** | Same as above | Public OAuth2 API | NO | varies | Easy |
| **Polar H10 chest strap** | HR + beat-to-beat RR intervals (real HRV) via standard BLE GATT Heart Rate Service (0x180D/0x2A37) | Direct BLE, already implemented in this repo (`sleepctl/adapters/wearable.py::BLEHeartRateSource`) | **NO** — no account/cloud needed at all | ~$90 one-time | **Easy** (code already exists) — comfort caveat: most people won't wear a chest strap all night, though Sleep-as-Android users do report using it for full sleep tracking |
| **Oura (Gen 3 / Ring 4)** | HR, HRV, sleep stages, temperature | OAuth2 API (personal access tokens deprecated in 2026) | **YES for Gen3/Ring4** — API access now requires an active Oura Membership ($5.99/mo or $69.99/yr); Gen 2 rings are grandfathered with free API access | Ring $349+ | Easy API, but paywalled |
| **Fitbit** | HR, sleep stages (30s granularity via API v1.2) | Fitbit Web API (OAuth2) — **being sunset Sept 2026**, migrating to Google Health API / Google OAuth2 with "Restricted" scopes requiring a privacy/security review even for personal single-user apps | **NO** for raw data itself (sleep stages/HR not Premium-gated); Premium only adds in-app coaching/accuracy tuning | Varies | Medium — API churn risk right now (mid-migration) |
| **Garmin** | HR, HRV (some models), sleep stages | **No public personal developer API** — Garmin's official "Health API" is business-only (approval required). Community reverse-engineered libraries (`python-garminconnect`, `garmin-givemydata`) scrape the Garmin Connect web/mobile API; broke in March 2026 due to new Cloudflare protections, newer tools work around it via a headless Playwright browser | NO subscription, but **fragile/unofficial** | Varies | Hard (unofficial, actively fought by Garmin) |
| **Whoop** | HR, HRV, sleep stages, recovery | Official developer API, free to register | **YES** — Whoop is subscription-only hardware; the strap does nothing without an active membership ($199–$359/yr) | N/A (bundled) | Easy API, but **subscription mandatory**; notable workaround: unofficial open-source app "Noop" reads the strap directly over BLE, bypassing the Whoop app/cloud entirely (mirrors the Polar-strap approach) |

Sources: https://support.ouraring.com/hc/en-us/articles/4415266939155-The-Oura-API ,
https://cloud.ouraring.com/docs/authentication ,
https://dev.fitbit.com/build/reference/web-api/sleep/get-sleep-log-list/ ,
https://developer.garmin.com/gc-developer-program/health-api/ ,
https://github.com/nrvim/garmin-givemydata ,
https://developer.whoop.com/api/ ,
https://www.techradar.com/health-fitness/fitness-trackers/this-looks-awesome-theres-now-an-unofficial-open-source-app-for-reading-whoop-data-that-doesnt-need-a-subscription ,
https://healthexportkit.lunium.io/ , https://help.healthyapps.dev/en/health-auto-export/automations/rest-api/

## 3. Intercepting the Pod's own raw upload — reconfirmed dead end for Pod 2 (July 2026)

No change from `docs/TIER1_RAW_CAPTURE.md`. Fresh search of the community projects (free-sleep,
opensleep, ninesleep, ZeroSleep) as of mid-2026 still shows:
- **free-sleep README states plainly: Pod 1 and Pod 2 = "NOT COMPATIBLE"**; only Pod 3 (with/
  without SD), Pod 4, Pod 5 are supported. https://github.com/throwaway31265/free-sleep
- **opensleep is scoped explicitly to Pod 3** ("Open source firmware for the Eight Sleep Pod 3");
  its author notes only briefly exploring an `/etc/hosts` redirect of
  `raw-api-upload.8slp.net:1337` to capture sensor data — and that redirect is performed *from
  inside an already-rooted Pod*, i.e. it presupposes root, it is not a network-only technique.
  https://liamsnow.com/projects/opensleep/
- **No community source (2025–2026) states one way or the other whether port 1337 is TLS-pinned.**
  This remains genuinely unverified, exactly as this repo's own recon docs already concluded —
  nothing new to revise.
- Since **no rooting method exists for Pod 2 hardware at all** (different SoC than the
  Variscite-based Pod 3+, per `sleepctl/recon/pod2_teardown.md`), the pinning question is close to
  moot for this user regardless: even a plaintext confirmation wouldn't yield a legal/safe way to
  redirect traffic without the on-device `/etc/hosts` edit that only a rooted Pod allows.

**Verdict unchanged: Tier 1/2 raw capture is a non-starter for a Pod 2 without rooting, and Pod 2
cannot be rooted.** Stay on Tier 0 cloud (blocked here by no Autopilot subscription) or replace the
Pod as a sensor entirely with one of the options above.

## Ranked recommendation (real sleep-stage + HR/HRV, minimal recurring cost)

1. **Emfit QS ($299 one-time)** — the closest drop-in replacement for what the Pod's own sensors
   would have given you: contactless, under-mattress, HR/HRV/RR/sleep-stage/movement, a real
   documented API, zero forced subscription, easy JSON integration. Best overall pick.
2. **Withings Sleep Analyzer (~$130–165 one-time)** — cheaper, same modality (contactless
   under-mattress), free official OAuth2 API, adds snore/apnea detection; slightly coarser update
   cadence than Emfit and only covers one side of the bed per unit.
3. **Polar H10 ($90 one-time) for wearable fusion, or Apple Watch (if already owned) via a local
   HealthKit-export app** — not as comfortable for all-night wear (H10) and not a cloud pull-API
   (Watch requires an on-device export app), but genuinely free/no-subscription and already wired
   into this repo's `BLEHeartRateSource` / `FusedPodSensorSource` for the fast-movement/HR overlay
   use case described in `docs/PASSIVE_CAPTURE.md`.

Runner-up note: if the user already owns a Fitbit, its API is free for raw HR/sleep-stage data
today, but is mid-migration to Google Health API (Sept 2026 cutover) — usable now, some near-term
integration churn expected.
