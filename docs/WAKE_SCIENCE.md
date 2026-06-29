# The Perfect Wake-Up — Design & Evidence

**Goal:** wake the user (a hot-sleeping anesthesia resident who needs silence and often does
something safety-critical right after waking) at the *best* moment, as gently as possible, and
leave them as *alert* as possible — with no sound, no rooting, and using only the levers we
already drive (the Pod's thermal ramp + vibration, optional Hue dawn bulbs + a 10k-lux therapy
lamp on a smart plug). This is the research behind the wake subsystem and the concrete changes it
drove.

All findings below are from **PubMed**; DOIs are linked inline.

## The one fact that dominates: *when* you wake matters more than *how*

- Sleep inertia — the grogginess/decrement right after waking — is **worst when you are pulled
  out of slow-wave (deep) sleep**, least from stage 1/2 (light), REM in between, and is **worse
  near the core-temperature trough** (i.e. waking well before your habitual time). Absent major
  sleep loss it usually clears within **~30 min**. (Tassi & Muzet 2000,
  [DOI](https://doi.org/10.1053/smrv.2000.0098); aviation review Sauvet 2024,
  [DOI](https://doi.org/10.3357/AMHP.6343.2024).)

⇒ The single highest-leverage move is to **wake in light sleep, not deep** — which is exactly
what the orchestrator already does (P(wake) classifier + ultradian cycle prediction; it *waits*
for a predicted light-sleep ascent rather than forcing a deep-sleep wake, and only overrides at
the hard deadline). The research reinforces, and now *quantifies the cost of*, an early/forced
wake (see "Readiness" below).

## Light is the strongest alerting lever we have — and it has a dose

- **Dawn simulation works.** A 0→250 lx ramp over the 30 min *before* wake **plus holding ~250 lx
  for 20 min *after* wake** improved daytime cognition under sleep restriction, and **low
  performers (the sleep-deprived) benefited most** (Gabel 2014,
  [DOI](https://doi.org/10.1016/j.bbr.2014.12.043)). Ramping to ~100 lx before the alarm then
  250 lx after matched a bright-light box for mood (Danilenko 2015,
  [DOI](https://doi.org/10.1016/j.jad.2015.03.055)). Clinical light dosing is **2,500–10,000 lx
  for 30–60 min** (SAD review, AFP 2020) — which is precisely what a **10k-lux therapy lamp**
  delivers.
- **Color/timing matters for melatonin.** Saturated **red/long-wavelength** light through closed
  eyelids reduces inertia **without suppressing melatonin** (Figueiro 2019,
  [DOI](https://doi.org/10.2147/NSS.S195563)). So the pre-wake ramp should stay **warm/amber and
  dim** (won't punish you if you drift back), and the **bright, cool, melanopic-rich** dose should
  land **after** the eyes open.
- Home dusk/dawn light meaningfully affects sleep and well-being; sunrise alarms are the common
  delivery (Beute & Aries 2023, [DOI](https://doi.org/10.1016/j.smrv.2023.101865)).

⇒ **Change made:** the sunrise bulbs ramp **warm-dim → bright-cool** through the dawn window, and
— this is the new part — **both the dawn bulbs and the therapy lamp are now held bright for
`post_wake_light_min` (default 20 min) *after* you've surfaced**, then stood down. Previously the
lights snapped off at wake-confirmation, throwing away the most valuable, evidence-backed part of
the dose. The bed simultaneously **stops warming** during the post-wake hold (next section).

## Skin temperature gates sleep vs. alertness — so reverse it at wake

- Warm skin is **sleep-permissive**; the alerting direction is the natural core-temperature rise
  / a cool skin stimulus. Skin-temperature manipulations measurably shift vigilance (Te Lindert &
  Van Someren 2018, [DOI](https://doi.org/10.1016/B978-0-444-63912-7.00021-7)). The same group's
  work underpins our *induction* warm-nudge and our maintenance warming.

⇒ **Change made:** the pre-wake **warm** dawn ramp (which helps lift core temperature and acts as
an anticipatory cue) flips to **NEUTRAL/stop-warming** the moment you're confirmed up, so the bed
isn't sleep-permissive while the light is trying to alert you. (The per-person `thermal_wake`
learner still tunes whether a warmer or cooler wake leaves *you* least groggy — for a hot sleeper
this is individual.)

## Silence-compatible: rhythm beats a flat buzz

- The user requires **no sound**, so audio is off. But the auditory literature is still
  instructive: a **melodic/rhythmic** waking sound *reduces* perceived inertia, while a
  **neutral, constant** tone *increases* it (McFarlane 2020,
  [DOI](https://doi.org/10.1371/journal.pone.0215788); systematic review McFarlane 2020,
  [DOI](https://doi.org/10.3390/clockssleep2040031)).

⇒ **Change made:** the silent vibration ladder now carries a **`vibration_pulse`** rhythm
(`slow → medium → continuous`) instead of implying a flat buzz, applying the rhythmic principle to
haptics. (Audio remains disabled.)

## Anticipation reduces inertia — keep wakes gentle and predictable

- **Self-awakening** (anticipating the wake time) **eliminated the reaction-time slowing and
  discomfort** seen with forced awakening (Ikeda & Hayashi 2009,
  [DOI](https://doi.org/10.1016/j.biopsycho.2009.09.008)); habitual self-awakeners wake more
  comfortably (longitudinal, [DOI](https://doi.org/10.2147/NSS.S33861)).

⇒ Reinforces the design: a gentle, *anticipatory* escalation (thermal dawn → light → soft pulse →
stronger) primes a self-awakening-like surfacing far better than an abrupt alarm, and a
**consistent** wake time helps — so the orchestrator only shaves *within* a bounded window and
surfaces the planned wake time in the UI.

## Readiness: tell a resident the truth about the next 30 minutes

- Because inertia is worst from deep sleep and near the core-temp trough, an **early or
  high-sleep-debt wake predictably costs alertness** for up to ~30 min (Tassi & Muzet 2000,
  [DOI](https://doi.org/10.1053/smrv.2000.0098)).
- **Caffeine helps fast:** 100 mg at wake measurably shortened inertia, kicking in from **~10–20
  min** (Newman 2013, [DOI](https://doi.org/10.2466/29.22.25.PMS.116.1.280-293)).

⇒ **Change made:** `/wake/plan` now returns a **`readiness`** block — a buffer (15 min floor,
widened to 25–30 min when waking well before habitual time or in heavy debt) before anything
safety-critical, plus a **caffeine-timing** nudge (100 mg at wake, stronger on short/early wakes,
skip if heading back to bed). Surfaced in the dashboard's smart-alarm card. Behavioral guidance,
not device control.

## What did *not* change (and why)

- **No audio**, ever — the user needs silence; we apply the rhythm finding to vibration instead.
- **No aggressive cooling shock** at wake — the evidence supports *stopping warming* and a cool
  skin *stimulus*, not a cold blast; the per-person learner decides direction within safe bounds.
- **No new wake stage targeting beyond light sleep** — stage-at-wake is already the dominant lever
  and is already handled; the gains here are in the **post-wake** dose and **honest readiness
  guidance**, which were the gaps.

## Where the changes live

- `sleepctl/controller/wake_orchestrator.py` — `post_wake_light_min` hold (bright dose past wake,
  bed goes NEUTRAL), `vibration_pulse` rhythm, `post_wake` phase.
- `sleepctl/config.py` — `Tunables.post_wake_light_min`.
- `dashboard/api/app/services.py` — `wake_readiness()` (inertia buffer + caffeine) and the
  `dawn_light`/`readiness` blocks in `wake_plan`.
- `dashboard/web/components/GymCard.tsx` + `lib/api.ts` — surfacing.
- The Hue dawn bulbs + therapy lamp are driven from the orchestrator's `wake_action`
  (`light_level` ramp + `should_wake` therapy snap), so the post-wake hold drives the real lights
  automatically.
