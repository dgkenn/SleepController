# Comprehensive ML feature design + in-night architecture steering ("nudge me deeper")

A full brainstorm of (a) every feature the learners should consume for **onset, maintenance, wake,
and architecture-adherence**, and (b) the new capability the user asked for: an **in-night
controller that compares the realized sleep trajectory to the ideal and actively nudges** — e.g.
when you're in light sleep but *should* be in deep, steer you deeper. Written to be exhaustive and
honest about what's feasible with the Pod.

> Sources are from **PubMed**; DOIs linked inline. The deep-sleep-enhancement evidence is the spine
> of the steering design.

---

## 0. Principles & honest constraints (read first)

The bed is a **slow, weak, single-channel actuator** (thermal) sampled at **~60 s** cloud
resolution. That bounds everything:

- **You cannot *force* a stage — only bias its probability.** Thermal nudges shift transition
  likelihoods over minutes; they don't switch stages on command.
- **The #1 priority is not causing an awakening.** A "deepen" maneuver that wakes you is
  net-negative. Every steering action must be **vetoed by the awakening-risk model** and slew-bounded.
- **Stage labels are noisy at 60 s.** Smooth over a window; never chase a single minute's label.
- **Confounders dominate architecture** more than the bed can fix (alcohol fragments late sleep;
  illness/stress cut deep). The system must attribute and down-weight these, not fight them.
- **Causation needs A/B.** Whether "cool-to-deepen" actually deepens *you* is only knowable by
  n-of-1 testing — correlation in observational nights is confounded.
- **True slow-oscillation enhancement needs EEG** (below). The Pod has none, and the user needs
  silence, so the Pod-native deepening lever is **thermal probability-shifting**, not neurostim.

---

## 1. Exhaustive feature taxonomy (what each learner should see)

Today the ML uses a **nightly** row (setpoint knobs + a few context flags + outcomes) — see
`ml/dataset.py`, `ml/features.py`. That's thin. The full set, by phase:

### A. Sleep ONSET (predict & shorten latency)
| Feature | Why | Source |
|---|---|---|
| lights-out time vs **habitual bedtime** (circadian phase proxy) | onset is hardest in the wake-maintenance zone | derived |
| **sleep pressure**: hours awake, prior-night TST, rolling debt | high pressure → fast onset | derived |
| pre-bed **timing** (not just flags): caffeine last-dose, alcohol, exercise, last meal, hot bath, screen/blue-light | all shift latency | check-in / context |
| stress (subjective + HRV at lights-out) | arousal delays onset | survey + sensor |
| bed temp + room temp at lights-out; the **onset warm nudge applied** | warm-then-cool speeds onset (Raymann/Van Someren; Ichiba 2020) | sensor + action |
| first-20-min HR/HRV/RR/movement trajectory | the physiological descent into sleep | sensor |
| ambient: room temp, outdoor temp/season, light, noise | environment gates onset | weather + sensor |
| day-of-week, illness, (menstrual phase if tracked) | systematic shifts | context |
→ **Outcome:** sleep-onset latency, sleep persistence. *(Onset learner exists for the thermal knob;
this expands its feature view.)*

### B. Sleep MAINTENANCE (predict & prevent awakenings)
| Feature | Why | Source |
|---|---|---|
| time-since-onset, **cycle number & position** | awakening risk + stage expectation vary by cycle | `SleepCyclePredictor` |
| stage-transition history, **micro-arousal density** | precursors of full awakenings | sensor (coarse at 60 s) |
| **autonomic precursors**: HR slope, HRV LF/HF shift, RR change/irregularity, movement bursts (incl. sub-minute phone BCG) | precede cortical arousal | `SleepWakeClassifier` |
| **thermal trajectory**: bed temp, *variability*, rate-of-change, commanded level, room-temp drift, weather | swings & heat fragment sleep | sensor + action |
| position / presence | position changes precede arousals | sensor |
| antecedents: alcohol (late fragmentation), late meal, stress, noise | confounders to attribute | context |
| the control actions: **settle nudges, pre-cools + their lead times** | close the action→outcome loop | action ledger |
→ **Outcome:** wake events, WASO, awakening clusters, recurring wake-times. *(Wake-risk assessor +
lead-time learner exist; expand features + per-person calibration.)*

### C. WAKE-UP (predict & minimize grogginess)
| Feature | Why | Source |
|---|---|---|
| stage at wake, P(wake), cycle position, min-to-deadline | inertia is worst from deep | classifier + predictor |
| sleep debt, total sleep so far, time-in-each-stage | debt vs inertia trade | derived |
| the **wake maneuver**: window, ramp temp, light dose, vibration pulse, cold-snap | the levers being learned | action |
| circadian: wake vs habitual, **core-temp-trough proximity** | inertia worst near the trough | derived |
| pre-wake physiology trajectory | readiness to surface | sensor |
→ **Outcome:** grogginess, minutes-early, forced, daytime energy, time-to-alert. *(wake_tuning +
thermal_wake exist; expand features.)*

### D. ARCHITECTURE-ADHERENCE (the new, real-time set — powers steering)
| Feature | Why |
|---|---|
| **deep-min so far vs ideal-by-now** (deficit/surplus) | are we on the deep curve? |
| **REM-min so far vs ideal-by-now** | are we on the REM curve? |
| **current stage vs expected stage** for this cycle/time | the instantaneous deviation |
| deep-bout length, time-since-last-deep, **SWS pressure** (declines across the night) | how steerable deep still is |
| fragmentation & efficiency so far | context for how hard to push |
| each maneuver applied + the **immediate stage response** | the per-person response model |
→ **Outcome:** end-of-night architecture match to the *learned* ideal; per-maneuver stage response.

---

## 2. The in-night architecture-steering controller (the headline)

A new fast-loop layer **inside Maintenance** that does what the user described.

### The ideal trajectory
Define the **ideal cumulative stage curve** as a function of time-since-onset and cycle number,
parameterized by the (learned, per-person) night targets:
- **Deep is front-loaded** — most SWS belongs in cycles 1–2; the bed's leverage is highest early.
- **REM is back-loaded** — REM grows in the last third; leverage is later.

Each tick the controller computes the **deviation**: "by this point you should have ~X min deep;
you have Y; you're currently LIGHT when the ideal curve wants DEEP."

### The evidence-based nudges (thermal, Pod-native)
According to PubMed, the thermal routes to **more SWS**:
- **Lower core temperature → more deep.** Eight Sleep's own Autopilot RCT: cooler offset → more
  deep sleep. So *cool the bed* when deep-deficient early. (Primary lever for a hot sleeper.)
- **Distal skin warming → physiological heat loss → core drop → deeper sleep & more delta.**
  Periocular/distal warming increased delta activity and stage-2/SWS in the first half by mimicking
  the pre-sleep heat-loss state (Ichiba 2020, [DOI](https://doi.org/10.1038/s41598-020-77192-x)).
  A *brief, comfort-capped* warm pulse can trigger the vasodilation that drops core — counter-
  intuitive, and delicate for a hot sleeper, so it's an A/B-gated option, not the default.
- **Stability + low variability** protects ongoing deep (swings fragment it).
- **Darkness/quiet** (Hue already off at night).

REM-deficient late → a **small warm bias** (warmth promotes REM; Autopilot RCT).

### "Nudge me lighter" — the asymmetric corollary

The default stance is **maximize deep**: deep sleep is the recovery priority *and* it is
**homeostatically self-limiting** (SWS pressure discharges and the body exits deep on its own), so
there is rarely a reason to actively pull you *out* of deep. "Nudge lighter" is therefore **not the
mirror image of "nudge deeper"** — it's a much narrower, more tightly-gated maneuver, used only in
two legitimate cases:

1. **Pre-wake (already shipped).** Easing you toward light sleep for a low-inertia wake is exactly
   what the smart-wake **warm ramp** does in the wake window. That *is* the "nudge lighter" maneuver,
   and it's already evidence-grounded and learned (`thermal_wake`).
2. **Late-night REM unblock (optional, A/B-gated, default OFF).** *Only* in the back third, *only*
   when deep is already at/above the learned target **and** REM is below it, a **small warm bias**
   may let the natural deep→REM transition happen at a cycle boundary. The evidence for actively
   lightening mid-night is weak, so this is opt-in and must prove itself per person via n-of-1.

**The asymmetry, concretely:**

| | Nudge deeper | Nudge lighter |
|---|---|---|
| Mechanism | cool (lower core) | warm (raise core) |
| When | early/mid, deep-deficient | back-third only, deep-surplus *and* REM-deficient; or pre-wake |
| Frequency | the workhorse maintenance maneuver | rare / mostly subsumed by the wake ramp |
| Default | on (bounded) | **off** except the pre-wake ramp |
| Hard rule | never wake you | **never reduce total deep below the learned floor**; never trade away recovery |

So: we build "nudge deeper" as the primary in-night steerer, and treat "nudge lighter" as the
pre-wake ramp (done) plus an **optional, off-by-default, back-third-only** REM-unblock that only
ships if A/B shows it actually helps — never at the cost of your deep total.

### The decision rule (bounded, maintenance-first)
```
if in LIGHT and deep-deficit early in the night and awakening-risk LOW:
    DEEPEN  → bias cooler (toward deep target), tighten stability, hold quiet/dark
             (optional A/B: a brief distal warm pulse to trigger heat loss)
elif REM-deficit in the back third and risk LOW:
    bias slightly warm (REM)
else:
    hold / normal maintenance
ALWAYS: veto by the awakening-risk model · slew ≤ max_step · variability cap · comfort clamp
```

### Make every maneuver LEARNABLE
This is the key to "exhaustive + correct": each steering action is logged as
`(deviation state, maneuver, magnitude) → stage response over the next N min + did it cause an
awakening`. From that:
- a **deepening-response model** learns *P(transition deeper | state, maneuver)* — i.e. **does
  cool-to-deepen actually work for YOU, and how much**;
- **n-of-1 A/B** (reusing the experiments engine) tests deepen-on vs deepen-off nights so the effect
  is *causal*, not correlational;
- the magnitude is **explored** (jitter) and converges per-person, exactly like the onset/wake
  learners.

So the system starts from evidence (cool→deep), then *learns whether and how much it can actually
steer your architecture*, and stops pushing if it can't (do-no-harm).

---

## 3. The ML architecture to support all of this

1. **Feature store, 3 layers** (extend what exists):
   - *per-minute raw window* — already in `raw_samples`;
   - **per-segment / per-cycle aggregates** — NEW table (stage durations, transitions, thermal
     trajectory per cycle, maneuvers + responses);
   - *nightly* `FeatureRow` — **expand** with circadian timing, thermal-trajectory shape, action
     features, per-phase summaries; + context/survey.
2. **Per-phase models** (each uncertainty-gated, do-no-harm fallback):
   - onset-latency model · awakening-risk model (exists, expand) · **deepening-response model
     (NEW)** · wake-grogginess model (exists, expand) · **architecture-adherence model** (predicts
     end-night deep/REM from the trajectory so the controller can act early).
3. **Two timescales**: a **real-time steering policy** (this controller, fast loop) and the existing
   **nightly setpoint learner** (slow loop) — the fast loop acts within bounds the slow loop sets.
4. **Reward shaping per phase**: onset latency; wake events (dominant); grogginess; and an
   **architecture-match-to-learned-ideal** term — all already maintenance-dominant.
5. **Exploration + causality**: n-of-1 A/B per maneuver; revealed-preference; confounder
   down-weighting; K-night delayed attribution.
6. **Uncertainty gating**: act only when the response model is confident *for this person*; else
   hold (the conservative rule policy).

---

## 4. Modalities beyond thermal (exhaustive, with feasibility)

| Modality | Deepening evidence | Feasible with the Pod? |
|---|---|---|
| **Thermal** (cool / distal-warm pulse) | Autopilot RCT; Ichiba 2020 | ✅ native — the primary lever |
| **Darkness / quiet** | environmental | ✅ (Hue off at night) |
| **Closed-loop acoustic slow-oscillation stim** | the gold standard — in-phase auditory clicks boost slow oscillations, spindles & memory (Ngo 2013, [DOI](https://doi.org/10.1016/j.neuron.2013.03.006)); home wearable EEG version (Bressler 2023, [DOI](https://doi.org/10.1088/1741-2552/acfb3b)) | ❌ needs **EEG phase-locking** + audio → conflicts with the Pod (no EEG) and silence. *Future optional EEG-headband integration.* Note spindle-targeting alone didn't replicate memory gains (Ngo 2018, [DOI](https://doi.org/10.1016/j.jneumeth.2018.09.006)). |
| **Vibration entrainment** | no evidence for deepening; arousal risk | ✗ skip |
| **Behavioral (upstream)** | the *biggest* deep-sleep levers: exercise, no late alcohol, cool room, consistent schedule | ✅ surface as **coaching**, not control |

The honest headline: the Pod can *bias* you deeper thermally and the system can *learn* how well
that works for you — but the dramatic SWS-boosting in the literature is **acoustic + EEG**, which is
a separate (silent-incompatible) hardware path.

---

## 5. Phased plan

- **Phase 1 (buildable now, data already available):**
  1. Expand the nightly `FeatureRow` (circadian timing, thermal-trajectory shape, action features).
  2. Add the **per-cycle segment layer** + the architecture-adherence features.
  3. ✅ **SHIPPED — the in-night DEEPEN maneuver.** `controller/architecture.py` builds tonight's
     ideal cumulative deep/REM curve (front-loaded deep via exponent <1, back-loaded REM via >1)
     from the **personalized** per-night targets (the unified `plan_night` → `set_night_targets`),
     compares it to the realized architecture the controller accrues each tick, and — when you're
     **light-but-behind-the-deep-curve, early in the night, with awakening-risk LOW** — drives the
     bed to the deep setpoint (`DEEP_BIAS_COOL`) to bias you deeper. Awakening-risk is the veto
     (it reuses the existing pre-empt signal, so it never fights a brewing arousal); slew /
     variability / comfort clamps still bound every move. Each maneuver is **edge-logged to the
     `steer_events` ledger** and resolved (`deepened?` / `caused_wake?` over a 20-min horizon) →
     the supervised signal for the Phase-2 deepening-response learner + n-of-1 A/B. The asymmetric
     "nudge lighter" corollary is config-gated **off** (`steer_rem_unblock_enabled`), exactly as
     designed. Surfaced live on Tonight via the `steering` block of `/predictive/preemption`.
  4. ✅ **SHIPPED — reconciliation into one favorable-state controller.** The deepen maneuver is
     now one move of a single in-night controller whose job is to **keep you in the most favorable
     state available**: it **ACQUIRES** a deeper state when you're behind the curve, and **DEFENDS**
     the deep / back-half-REM state you're already in (hold cool + stable, never trade a live deep
     bout away). It is reconciled with the other two in-night thermal maneuvers by a strict,
     test-pinned precedence so they never fight over the bed:
     **(1) wake-prevention** (rising risk / precursor / micro-arousal → settle; never deepen into
     a brewing disturbance) **> (2) wake-up handoff** (within the smart-wake window + ~a cycle of
     the deadline the steerer stands down so the wake-up ramp can lift you from light sleep — no
     deepening into sleep-inertia) **> (3) acquire/defend** the favorable state **> (4) hold**.
     Pinned by `tests/test_architecture_steering.py`; see `docs/CONTROL_LAW.md` §5.
- **Phase 2:** the per-phase models (onset, awakening-risk, deepening-response, grogginess) on the
  expanded features; uncertainty-gated; n-of-1 for each maneuver.
- **Phase 3 (research / optional hardware):** Tier-1 raw capture for finer arousal/micro-event
  detection; EEG-headband acoustic closed-loop SWS enhancement (separate silent-incompatible path).

---

## TL;DR

Feed each phase its full causal feature set (circadian timing, physiology trajectories, thermal
shape, the actions taken — not just nightly means); add a **per-cycle "are we on the ideal deep/REM
curve?" signal**; and put a **bounded, awakening-risk-vetoed in-night controller** on top that, when
you're lighter than ideal, biases the bed to deepen you — starting from the evidence (cool → deep;
distal-warm → heat-loss → deep) and then **learning, per person and via A/B, whether and how much it
can actually move your architecture**, never at the cost of waking you.
