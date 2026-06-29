# Optimizing Sleep Under Deprivation (Residents / Shift Work) — Design & Evidence

**Goal:** for a trainee with an erratic call schedule and chronic, unavoidable sleep loss, get the
most *functional* sleep out of a bad situation — not just one good night, but a strategy *across*
the rotation. This is the research behind the cross-shift planner (`sleepctl/shift_manager.py`)
and the improvements this pass added.

All findings are from **PubMed**; DOIs are linked inline.

## The core facts

- **Sleep debt is real, cumulative, and not repaid in one night.** Chronic restriction produces
  dose-dependent neurobehavioral deficits that accumulate, and recovery spans **multiple** nights
  (Van Dongen et al. 2003, [DOI](https://doi.org/10.1093/sleep/26.2.117)).
- **You can bank sleep ahead of a known hard stretch — and it works.** In a controlled trial,
  one week of **extended time-in-bed (~10 h)** before a 7-night restriction left subjects with
  **fewer PVT lapses during the restriction** *and* **faster recovery afterward** vs. habitual
  sleepers (Rupp, Wesensten, Bliese, Balkin 2009,
  [DOI](https://doi.org/10.1093/sleep/32.3.311)). Banking is the highest-leverage *proactive*
  move a resident has before a night block. (Reviews concur: Ebben 2017/2020,
  [DOI](https://doi.org/10.1016/j.jsmc.2017.03.020); the military-surgery review explicitly
  recommends prophylactic banking + napping, Parker & Parker 2016,
  [DOI](https://doi.org/10.1136/jramc-2016-000640).)
- **Naps are a frontline countermeasure — prophylactic *and* recuperative.** A nap before sleep
  loss (prophylactic) or after it (recuperative) both help cognition; the benefit and the
  inertia cost depend on duration and timing (Ficca et al. 2009 review,
  [DOI](https://doi.org/10.1016/j.smrv.2009.09.005)). A nighttime nap on shift can reach **deep
  sleep** and suppresses on-shift sleepiness (Takeyama et al. 2005,
  [DOI](https://doi.org/10.2486/indhealth.43.24)).
- **Anchor sleep stabilizes the clock.** Holding a **fixed core sleep period at the same
  clock-time** across rotating shifts keeps circadian phase from scattering (Takeyama 2005, *ibid*).
- **Split schedules: you perform better than you feel.** Splitting sleep lowers night-time
  cognitive impairment vs. a single consolidated block, but people **feel sleepier** for much of
  the wake period — so set expectations honestly (Zhou et al. 2015,
  [DOI](https://doi.org/10.1016/j.aap.2015.10.027)).

## How this maps to the product

The cross-shift planner already encoded debt tracking, a day-of prophylactic nap, post-call
recovery + drowsy-driving safety, and anchor sleep. This pass closed the biggest evidence gap and
made the whole subsystem usable:

### 1. Proactive sleep banking (Rupp 2009) — **new**

The day-of prophylactic nap is the last mile; **banking is the days-ahead play** that the planner
was missing. When a night shift is on the horizon but past the immediate nap window (≈16–72 h
out, i.e. you still have whole nights to bank), the planner now:

- emits a **banking prescription** — "extend to ~9–10 h in bed for the next *N* nights before your
  night block; it cuts on-shift lapses and you'll recover faster afterward," and
- **raises tonight's target toward ~9.5 h** so the controller actually drives the extended sleep.

### 2. Activating the dormant subsystem — **new**

The planner was being fed an **empty shift list**, so its banking / prophylactic-nap / anchor logic
never fired, and its output wasn't shown anywhere. Now:

- a **manual next-shift hint** (`/shift/config`: enabled + next-shift datetime + kind) feeds the
  planner until a calendar integration lands, and
- a **Shift & Sleep-Debt card** on the dashboard surfaces debt band, tonight's target, the banking
  prescription, naps, the anchor window, and safety warnings — with a control to set the upcoming
  night shift.

### 3. Reinforced (already present)

- **Recovery is multi-night**, and the post-call state is treated as the highest-priority safety
  mode (recovery nap + explicit drowsy-driving warning).
- **Nap durations** follow the same inverted-U the nap engine uses — power (~10–20 min) or full
  cycle (~90 min), avoiding the ~30–60 min SWS-inertia trap (Brooks & Lack 2006,
  [DOI](https://doi.org/10.1093/sleep/29.6.831)).
- **Anchor sleep** for variable schedules.

## Deliberate non-changes

- **No pharmacology in the loop.** The literature lists modafinil/caffeine for alertness; the
  product surfaces a caffeine *timing* nudge at wake (see `WAKE_SCIENCE.md`) but does not
  prescribe wake-promoting drugs — that's a clinician's call.
- **No automatic schedule scraping yet.** The shift hint is manual on purpose; a calendar/ICS feed
  is the natural follow-up that makes banking fully hands-off.
- **Banking is gated to night blocks**, not day shifts — extending TIB indefinitely isn't the goal;
  banking is specifically the pre-loading move before known restriction.

## Where the changes live

- `sleepctl/shift_manager.py` — `ShiftPlan.banking` + the banking prescription / target bump.
- `dashboard/api/app/services.py` — `shift_config` (settings_kv) + `shift_plan_view` (on-demand).
- `dashboard/api/app/main.py` — `/shift/plan`, `/shift/config`.
- `dashboard/daemon/live_daemon.py` — feeds the configured shift into the cached plan.
- `dashboard/web/components/ShiftCard.tsx` + `lib/api.ts` + `app/page.tsx` — surfacing + the
  next-shift control.
