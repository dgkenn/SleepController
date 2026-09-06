"""SleepController — the Decide step of Sense/Decide/Act/Learn.

Given the freshest sensor frame + schedule context + recent history, it advances the
state machine, picks a thermal intent via the matching routine, resolves a safe target
temperature/level, and returns a fully-explained ``Decision``. It never performs device
I/O or persistence — the runtime loop acts on the returned Decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.controller.architecture import ArchitectureSteering
from sleepctl.controller.data_quality import DataQuality, assess_data_quality
from sleepctl.controller.guardrail import DecisionGuardrail, GuardrailAssessment
from sleepctl.controller.induction import InductionRoutine
from sleepctl.controller.maintenance import MaintenanceRoutine, WakeRecoveryRoutine
from sleepctl.controller.arousal import ArousalDetector, ArousalLevel
from sleepctl.controller.bed_exit import BedExitDetector
from sleepctl.controller.hypnogram import (HypnogramConstraint, architecture_plausible,
                                           constrain)
from sleepctl.controller.precursor import PrecursorDetector
from sleepctl.controller.sleep_onset import SleepOnsetDetector
from sleepctl.controller.smart_wake import SmartWakeRoutine
from sleepctl.controller.state_estimator import estimate_sleep_stage
from sleepctl.controller.state_machine import SleepStateMachine
from sleepctl.controller.wake_risk import WakeRiskAssessor
from sleepctl.controller.thermal import ThermalController
from sleepctl.controller.thermal_latency import ThermalLatencyModel
from sleepctl.controller.calibration import fahrenheit_to_level
from sleepctl.controller.wake_detection import WakeDetector
from sleepctl.models import (
    ContextRecord,
    ControllerState,
    CorrectionAction,
    Decision,
    NightObjective,
    SensorFrame,
    SleepStage,
    ThermalIntent,
)


#: Enough of a night to judge a duty cycle by. Before this the ratio is dominated by whichever
#: way the first few ticks happened to go, and rate-limiting on that would silence the pre-cool
#: exactly when the most night is left to protect.
MIN_TICKS_FOR_DUTY_CYCLE = 60

#: A gap this long between architecture accruals means a different night. Comfortably
#: longer than any within-night sensor dropout, comfortably shorter than a day.
ARCHITECTURE_GAP_RESET_MIN = 180.0


class SleepController:
    def __init__(self, cfg: AppConfig, setpoints=None) -> None:
        self.cfg = cfg
        self.sm = SleepStateMachine(cfg)
        self.wake_detector = WakeDetector()
        self.onset_detector = SleepOnsetDetector(cfg)
        self.arousal_detector = ArousalDetector(cfg)
        # The counterpart to `_wearable_bed_entry`: on an account where presence is
        # permanently None, nothing could ever END a session on evidence.
        self.bed_exit_detector = BedExitDetector()
        # Structural plausibility on the hypnogram -- see controller/hypnogram.py.
        self.hypnogram = HypnogramConstraint()
        self.last_bed_exit = None
        self.last_bed_entry_block = None
        self.bed_exit_events: list = []
        self._recovered_physio_at = None
        self._preempt_ticks_maint = 0
        self._maint_ticks = 0
        self.last_preempt_duty = 0.0
        self.last_preempt_duty_capped = False
        self.precursor_detector = PrecursorDetector(cfg)
        # Proactive sleep-maintenance: a learned WakeProfile can be attached by the loop.
        self.wake_risk_assessor = WakeRiskAssessor(cfg)
        self.induction = InductionRoutine(cfg)
        self.maintenance = MaintenanceRoutine(cfg)
        self.wake_recovery = WakeRecoveryRoutine(cfg)
        self.smart_wake = SmartWakeRoutine(cfg)
        # In-night architecture steering ("nudge me deeper"): compares the realized deep/REM curve
        # to tonight's personalized ideal and biases the bed deeper when behind + risk is low.
        self.steering = ArchitectureSteering(cfg)
        # Tonight's personalized ideal architecture (set by the daemon from the SleepPlan); the
        # steerer targets these. None -> steering holds (no target to chase).
        self.night_targets = None
        self.est_sleep_min: Optional[float] = None
        # Accrued time-in-stage since onset (the realized architecture so far).
        self._arch_deep_min = 0.0
        self._arch_rem_min = 0.0
        self._arch_light_min = 0.0
        self._arch_last_ts: Optional[datetime] = None
        self._deepen_active = False         # edge-trigger for steer-event logging
        self.last_steer = None              # last SteerDecision (telemetry)
        self.pending_steer_event = None     # consumed + logged by the cycle
        # Deepening-response policy: whether to ACTUATE the deepen nudge tonight. On control
        # ('observe') nights this is False — the steerer still judges + logs a SHADOW event (the
        # n-of-1 control arm) but doesn't cool. Set nightly by the daemon from the learner.
        self.steer_actuate = True
        # Measured thermal effect-latency from the in-bed self-test (minutes for a cool/heat
        # command to fully land). None until the self-test runs. Floors the deepening-response
        # horizon (don't judge "did it deepen?" before the cool has taken effect) and lengthens the
        # wake warm-up runway so the bed is actually warm by the wake time.
        self.measured_cool_lag_min: Optional[float] = None
        self.measured_heat_lag_min: Optional[float] = None
        # Reach-time (traverse) model built from measured rates/lags; feeds the induction cascade's
        # phase sizing and (widening-only) the wake warm-up runway. None until the daemon supplies it.
        self.induction_latency: Optional[ThermalLatencyModel] = None
        # Measured resting-physiology baseline (quiet-and-awake in bed): {hr, hrv, rr, movement}.
        # Anchors the arousal/wake-risk baselines early in the night (see _sleep_baseline).
        self.resting_baseline: Optional[dict] = None
        # Multi-signal, escalating, inertia-minimizing wake orchestrator (uses the calibrated
        # sleep/wake classifier + the fused fast movement; never oversleeps the deadline).
        from sleepctl.controller.sleep_wake import SleepWakeClassifier
        from sleepctl.controller.wake_orchestrator import WakeConfig, WakeOrchestrator
        self.wake_orch = WakeOrchestrator(WakeConfig.from_tunables(cfg.tunables),
                                          classifier=SleepWakeClassifier(cfg))
        self.last_wake_action = None        # exposed for telemetry/dashboard
        self.wake_debt_min = 0.0            # cumulative sleep debt -> debt-adaptive wake strategy
        # The learnable setpoint profile (updated nightly by the learning loop / ML).
        self.thermal = ThermalController(cfg, profile=setpoints)

        self._bed_entry_time: Optional[datetime] = None
        #: Bed entry recovered from persisted data after a restart (see restore_bed_entry).
        #: Consumed the first time bed entry is established, then cleared.
        self._recovered_bed_entry: Optional[datetime] = None
        self._sleep_onset_time: Optional[datetime] = None  # accurate fall-asleep time
        # The onset cascade (cold-settle -> warm pulse -> cool) runs on a clock that starts when
        # INDUCTION begins -- NOT bed-entry. Pressing "help me fall asleep" after lying awake a
        # while must restart the cascade at phase 1, so we track induction entry separately.
        self._induction_entered_at: Optional[datetime] = None
        self._induction_restart = False  # set by set_session('induce') -> restart the cascade
        self._last_target_f: float = cfg.tunables.neutral_temp_f
        # Session mode: "night" | "induce" | "nap_power" | "nap_cycle". Power naps keep the bed
        # light so slow-wave sleep (and its grogginess on waking) doesn't set in.
        self.session_mode = "night"
        self.session_keep_light = False
        self.last_wake_event = None
        self.last_onset_event = None
        self.last_arousal = None          # last ArousalAssessment
        self._stage_estimated = False     # was frame.stage derived from vitals this tick (no Pod stage)
        self._stage_held = None           # stage hysteresis: currently adopted estimate
        self._stage_pending = None        # candidate stage awaiting persistence
        self._stage_pending_n = 0
        self._stage_hold_suppressed = 0   # ticks whose stage flip the hysteresis absorbed
        #: Set by the randomized controller trial (arm B off / arm C on). None = trial inactive.
        self.trial_stabilizer_enabled = None
        # "sensor" | "model" | "model+deep" | "heuristic" (which supplied the stage).
        # "model+deep" = the learned stager said light, but the clock-free heuristic had
        # positive physiological evidence for DEEP and upgraded it (see state_estimator).
        self._stage_source = "sensor"
        self.last_cycle_state = {}        # ultradian trajectory estimate (see wake_orch.cycle_state)
        self.last_wake_risk = None        # last WakeRisk
        self.last_precursor = None        # last PrecursorAssessment (leading-edge drift)
        self.last_precursor_profile = None  # learned personalized awakening-precursor trajectory
        # 3AM WAKE targeted analysis: an OPTIONAL, gated pre-emption keyed off the personal
        # recurring-wake-window report (sleepctl.analysis.wake_patterns.wake_analysis_report's
        # ``recurring_windows``). None until a caller attaches one via ``set_wake_window_report``
        # (do-no-harm by construction -- see ``should_preempt_window``: a missing/empty report,
        # a disabled gate, or thin/low-confidence evidence all resolve to "no effect").
        self.wake_window_report = None
        self.last_wake_window_preempt = None  # last should_preempt_window() result, for telemetry
        self._arousal_started: Optional[datetime] = None  # for re-settling latency
        self.last_resettle_latency_min: Optional[float] = None
        self._anticipatory_active = False
        self.pending_precool_event = None  # consumed + logged by the cycle
        self.should_wake = False
        self.pending_wake_alarm = None  # WakeAlarmSpec to program (vibration + heat), once
        # Data-quality gate (Feature #6): last frame's trust assessment, exposed for
        # runtime_state/telemetry. Never raises confidence — only down-weights it and can
        # force a HOLD on untrustworthy data (see ``_apply_data_quality_gate``).
        self.last_data_quality: Optional[DataQuality] = None
        # Decision guardrail (Feature #8): a top-level invariant monitor over the recent
        # decision/frame TRAJECTORY (do-no-harm backstop, not a second controller). Recent
        # decisions are tracked here (bounded) so the guardrail can see the trajectory without
        # requiring every caller (offline runtime + live daemons) to thread history through.
        self.guardrail = DecisionGuardrail(cfg)
        self.last_guardrail: Optional[GuardrailAssessment] = None
        self._recent_decisions: list = []
        # Optional context the caller may attach for the guardrail (both fully optional —
        # absence just means those specific guardrail checks are skipped):
        #   comfort_profile: dict from repo.get_comfort_profile() -- {"cool_edge_f", "warm_edge_f", ...}
        #   thermal_health: a ThermalHealth-like object/namespace with a ``.state`` attribute
        self.comfort_profile: Optional[dict] = None
        self.thermal_health_status = None

    def _objective(self, context: Optional[ContextRecord]) -> NightObjective:
        if context is None:
            return NightObjective.OPTIMIZE
        nt = (context.night_type or "").lower()
        if nt in ("recovery", "off", "off_day", "rest"):
            return NightObjective.RECOVERY
        if nt in ("work", "constrained", "short") or context.is_short_sleep_day:
            return NightObjective.DAMAGE_CONTROL
        return NightObjective.OPTIMIZE

    def _cold_dwell_relief(self, target_f: float, now, cfg):
        """Ease the bed up if it has been camped at the cold edge of the comfort band.

        Returns ``(eased_value, reason)`` when relief applies, else ``(None, None)``.

        The comfort clamp answers "how cold may the bed be right now"; this answers "for how
        long". Four straight hours at 65-67F preceded the awakenings measured on 2026-08-24, and
        no existing check could see it because every individual reading was inside the band.

        Needs a measured cool edge to have something to be "at the edge of" -- with no comfort
        profile loaded there is no principled floor to reason about, so this stays inert rather
        than inventing one from a population default.
        """
        try:
            if not isinstance(self.comfort_profile, dict):
                return None, None
            cool_edge = self.comfort_profile.get("cool_edge_f")
            if cool_edge is None:
                return None, None
            # THE MARGIN HAS TO BE SMALL RELATIVE TO THE BAND, not an absolute degree.
            #
            # This was written when the maintenance settle was -1.0 F, which lands at 68.0 F and
            # never approached the 67.0 F cool edge -- so a 1.0 F margin cost nothing. Once the
            # pre-emptive settle was deepened to target the cool edge deliberately, that same
            # margin swallowed the entire intended operating range: with a measured band of
            # 67.0-69.5 F, "within 1 F of the edge" is 40% of the whole band, so every pre-empt
            # was immediately classified as camping and eased back up.
            #
            # Measured: the bed did not once go below 68.0 F on 2026-09-01 or 2026-09-04, on
            # either side of the settle fix -- the floor was this test, not the settle.
            margin = float(getattr(cfg.tunables, "cold_dwell_margin_f", 1.0))
            warm_edge = self.comfort_profile.get("warm_edge_f")
            if warm_edge is not None:
                band = max(0.5, float(warm_edge) - float(cool_edge))
                frac = float(getattr(cfg.tunables, "cold_dwell_margin_band_frac", 0.2))
                margin = min(margin, band * frac)
            limit = float(getattr(cfg.tunables, "cold_dwell_limit_min", 75.0))
            step = float(getattr(cfg.tunables, "cold_dwell_step_f", 0.75))
            cap = float(getattr(cfg.tunables, "cold_dwell_max_relief_f", 2.5))

            at_edge = target_f <= (float(cool_edge) + margin)
            if not at_edge:
                # left the cold edge -- the clock and the accumulated relief both reset, so a
                # later cold stretch starts from scratch rather than inheriting old credit.
                self._cold_since = None
                self._cold_relief_f = 0.0
                return None, None

            if getattr(self, "_cold_since", None) is None:
                self._cold_since = now
                return None, None

            held_min = (now - self._cold_since).total_seconds() / 60.0
            if held_min < limit:
                return None, None

            # Relief is a SUSTAINED offset, not a one-tick nudge. ``resolve`` recomputes the raw
            # target from scratch every tick and knows nothing about this, so granting once and
            # resetting would produce a single warm tick surrounded by cold ones -- the bed would
            # spend the night right back at the edge. Deriving the offset from total dwell time
            # instead makes it monotonic and stable: it holds as long as the cold stretch does,
            # steps up once per dwell period, and is capped.
            earned = int(held_min // limit) * step
            relief = min(earned, cap)
            if relief <= 0:
                return None, None
            self._cold_relief_f = relief
            return (target_f + relief,
                    f"cold-dwell relief +{relief:.2f}F after {held_min:.0f} min at the cold edge "
                    f"(cool_edge {float(cool_edge):.1f}F, cap {cap:.2f}F)")
        except Exception:
            return None, None

    def restore_bed_entry(self, ts: Optional[datetime]) -> None:
        """Seed the bed-entry anchor from persisted data, for use after a daemon restart.

        Only takes effect while bed entry has not yet been established this process, and never
        overrides an anchor already set -- so a live session is untouched and this can be called
        unconditionally at start-up.
        """
        if ts is None or self._bed_entry_time is not None:
            return
        self._recovered_bed_entry = ts

    def restore_last_physio(self, ts: Optional[datetime]) -> None:
        """Seed the abandoned-session clock from persisted data, for use after a daemon restart.

        ``session_abandon_min`` ends a session that has had no physiology for an hour, but the
        clock it measures against lives in this process. This box auto-deploys and restarts the
        daemon by design, so on a day with frequent deploys the hour could never elapse -- and
        2026-08-25 sat in WAKE_RECOVERY from 12:00 to 18:37 with zero heart rate and zero
        movement, 786 ticks of commanding the bed for nobody.

        Like ``restore_bed_entry``, this only ever fills a gap: it is consumed on the first tick
        that finds no physiology and is discarded the moment a real reading arrives.
        """
        if ts is None or getattr(self, "_last_physio_at", None) is not None:
            return
        self._recovered_physio_at = ts

    def _hold_stage(self, est, cfg):
        """Hysteresis on the ESTIMATED stage: a new sleep stage must persist before it is adopted.

        Sleep stages are physiologically persistent -- a real bout runs 15-30 minutes -- so a
        label that changes every 30-second tick is measurement noise, not physiology. Measured on
        2026-08-27: 233 stage flips across 686 maintenance ticks with a median bout of 2 ticks,
        and 187 of those flips (80%) were light<->deep oscillation driven by ordinary beat-to-beat
        HR variation across the boundary. The daemon ticks about every 30 s while the Pod frame
        refreshes every ~60 s, so consecutive ticks re-score the same physiology and land on
        different sides of it.

        This costs more than a messy hypnogram. Every deep->light flip fires a
        ``stage_regression`` vote in the wake detector, so the flapping manufactures wake
        evidence: 93 such flips on that night.

        AWAKE is deliberately EXEMPT. Wake detection has to stay responsive -- delaying an awake
        label to smooth the chart would trade the one thing this system exists to catch for
        cosmetics. Only transitions among the sleep stages are damped.
        """
        try:
            n = int(getattr(cfg.tunables, "stage_hold_ticks", 2))
        except Exception:
            n = 2
        stage, conf, source = est
        held_now = getattr(self, "_stage_held", None)
        # Any transition involving AWAKE is immediate, in BOTH directions. Exempting only the
        # ENTRY made AWAKE sticky -- easy to enter, slow to leave -- which inflated the awake
        # label count on the 2026-08-27 sequence from 30 to 53 and would have inflated WASO and
        # the wake-event count with it. Waking must be responsive; so must going back to sleep.
        if n <= 1 or stage is SleepStage.AWAKE or held_now is SleepStage.AWAKE:
            self._stage_held = stage
            self._stage_pending = None
            self._stage_pending_n = 0
            return est
        held = getattr(self, "_stage_held", None)
        if held is None or stage is held:
            self._stage_held = stage
            self._stage_pending = None
            self._stage_pending_n = 0
            return est
        # A DIFFERENT sleep stage: count consecutive agreement before switching.
        if getattr(self, "_stage_pending", None) is stage:
            self._stage_pending_n = getattr(self, "_stage_pending_n", 0) + 1
        else:
            self._stage_pending = stage
            self._stage_pending_n = 1
        if self._stage_pending_n >= n:
            self._stage_held = stage
            self._stage_pending = None
            self._stage_pending_n = 0
            return est
        # Not yet persistent -- keep the held stage. The suppression is surfaced on its own
        # field rather than by decorating `stage_source`, which is a fixed vocabulary other code
        # and tests match against; smuggling a suffix into it would break that contract.
        self._stage_hold_suppressed += 1
        return (held, conf, source)

    def _stabilize_target(self, proposed_f: float, now, cfg, preempting: bool = False):
        """Should this proposed target be HELD instead of commanded? (arm C)

        Returns ``(held_value, reason)`` when the move is suppressed, else ``(None, None)``.
        Two rules, in order:

          1. **Deadband** -- a proposed move smaller than ``stabilizer_deadband_f`` is noise, not
             a decision. Suppressing it costs nothing thermally (the bed cannot resolve it) and
             removes most of the churn.
          2. **Minimum dwell on reversals** -- a move that flips the last direction must wait
             ``stabilizer_min_dwell_min`` since the last committed move. Same-direction moves are
             never delayed, so a genuine ramp still runs at full speed; only oscillation is
             damped. This is the rule that targets the measured failure (31 of 36 interventions
             reversing, several within one minute).

        **The dwell does not apply while PRE-EMPTING.** A pre-emptive move is almost always a
        reversal -- the bed is drifting toward the cool edge and prevention wants to warm -- so
        the dwell silently swallowed exactly the moves that were most time-critical. Measured on
        2026-08-27: 235 of 294 pre-empting ticks resolved to HOLD, and the system's own
        prevention-timing check reported the pre-cool arriving at a median 7.8 min against a
        median 4.6 min to the awakening. The awakening was beating the dwell clock.

        The dwell exists to damp ORDINARY thermal hunting, which is a comfort/wear concern on a
        timescale of many minutes. An imminent awakening is not that. The deadband still applies
        in both cases, because a move the bed cannot physically resolve is not worth making
        whatever the urgency.
        """
        try:
            # Trial arm B runs with the stabilizer OFF; arm C runs with it on. None means the
            # controller trial is not active and production behaviour applies.
            if getattr(self, "trial_stabilizer_enabled", None) is False:
                return None, None
            deadband = float(getattr(cfg.tunables, "stabilizer_deadband_f", 0.4))
            dwell = float(getattr(cfg.tunables, "stabilizer_min_dwell_min", 12.0))
            last = self._last_target_f
            delta = proposed_f - last
            if abs(delta) < deadband:
                return last, f"stabilizer: held (move {delta:+.2f}F under {deadband}F deadband)"
            if preempting and getattr(cfg.tunables, "stabilizer_preempt_bypass", True):
                return None, None
            direction = 1 if delta > 0 else -1
            last_dir = getattr(self, "_stab_last_dir", 0)
            last_move_at = getattr(self, "_stab_last_move_at", None)
            if last_dir and direction != last_dir and last_move_at is not None:
                since_min = (now - last_move_at).total_seconds() / 60.0
                if since_min < dwell:
                    return last, (f"stabilizer: held (reversal {since_min:.1f} min into a "
                                  f"{dwell:.0f} min dwell)")
            return None, None
        except Exception:
            return None, None

    def _note_target_move(self, target_f: float, now) -> None:
        """Record a COMMITTED move so the stabilizer's dwell clock reflects real changes only."""
        try:
            delta = target_f - self._last_target_f
            if abs(delta) <= 1e-9:
                return
            self._stab_last_dir = 1 if delta > 0 else -1
            self._stab_last_move_at = now
        except Exception:
            pass

    def _wearable_bed_entry(self, frame, recent, cfg) -> bool:
        """Can sustained LIVE wearable physiology stand in for an unavailable Pod presence?

        Only ever consulted for the IDLE -> INDUCTION edge, and only when the Pod has NOT
        positively reported presence either way. See ``AppConfig.wearable_bed_entry``: on an
        account with no Autopilot membership presence is None forever, so the normal
        ``presence is True`` gate can never open and the controller can never start a night on
        its own -- two of four measured nights were spent entirely in IDLE with the wearable
        streaming a full, normal night of physiology.

        Strict on purpose. A band left on a charger reporting a flat line previously produced a
        whole morning of fake DEEP (2026-08-04), so a frozen or implausible reading must not
        qualify: the HR has to be in a plausible worn range AND actually MOVING across the
        window. A stale/flat feed has zero range and is rejected.
        """
        try:
            t = cfg.tunables
            if not getattr(t, "wearable_bed_entry", True):
                return False
            # Never contradict the Pod: this fills in for UNKNOWN presence only.
            if frame.presence is not None:
                return False
            need = int(getattr(t, "wearable_bed_entry_min_ticks", 5))
            lo = float(getattr(t, "wearable_bed_entry_hr_lo", 30.0))
            hi = float(getattr(t, "wearable_bed_entry_hr_hi", 120.0))
            min_range = float(getattr(t, "wearable_bed_entry_min_hr_range", 2.0))

            window = [f for f in (list(recent or []) + [frame])][-need:]
            if len(window) < need:
                return False
            hrs = [f.heart_rate for f in window if getattr(f, "heart_rate", None) is not None]
            if len(hrs) < need:
                return False
            if not all(lo <= h <= hi for h in hrs):
                return False
            # A live worn sensor varies beat to beat; a frozen/stale reading does not.
            if (max(hrs) - min(hrs)) < min_range:
                return False
            # ...and a worn sensor on someone WALKING AROUND also varies beat to beat, at a
            # heart rate the 120 bpm ceiling above happily admits. That is how 2026-08-27 opened
            # a brand-new "night" at about 06:00 and ran induction/maintenance until 11:21 with
            # a median heart rate of 102-122 bpm by local hour. Someone getting into bed is
            # LYING STILL, whatever their heart rate is doing on the way down.
            # Tolerate a controller built without __init__ (the pure-helper style used by the
            # bed-entry tests): a missing detector must fall back to a fresh one, never to the
            # broad `except` below, which would silently answer "not in bed" for every caller.
            detector = getattr(self, "bed_exit_detector", None) or BedExitDetector()
            blocked = detector.blocks_entry(frame, recent, cfg)
            if blocked:
                self.last_bed_entry_block = blocked
                return False
            self.last_bed_entry_block = None
            return True
        except Exception:
            return False

    def decide(
        self,
        frame: SensorFrame,
        context: Optional[ContextRecord],
        recent: list[SensorFrame],
        now: datetime,
        baselines=None,
    ) -> Decision:
        cfg = self.cfg
        objective = self._objective(context)
        required_wake = context.required_wake_time if context else None
        current_f = frame.bed_temp_f if frame.bed_temp_f is not None else self._last_target_f

        # --- stale-data guard: never act on stale/low-confidence data -----------
        # THE WAKE DEADLINE OUTRANKS EVERY DATA-QUALITY HOLD.
        #
        # Both holds below return EARLY, before ``self.sm.transition`` runs -- so while either is
        # active the state machine is frozen and MAINTENANCE can never become WAKE_WINDOW. That
        # silently disables the alarm. Observed 2026-08-06: the wearable dropped at 00:01, every
        # tick from then on held with "data quality low (missing_vitals:heart_rate,hrv,
        # respiratory_rate)", and the armed 08:30 wake never fired at all -- the user woke on
        # their own at 08:58. It cost nothing on a day off; on a workday it is a missed alarm
        # with no warning.
        #
        # A deadline is a CLOCK, not a physiological inference. It needs no sensor to be correct,
        # and refusing to honour it is not "do no harm" -- oversleeping is the harm. So once the
        # smart-wake window has opened we let the tick fall through to the normal path, which
        # runs the state machine and the wake orchestrator. Data quality still governs the
        # THERMAL decisions inside that path (the orchestrator falls back to a stage-less,
        # deadline-guaranteed wake when the feed is stale); it just no longer suppresses the
        # transition itself.
        wake_window_open = False
        if required_wake is not None:
            lead_min = float(getattr(self.wake_orch.cfg, "window_min", 30) or 30)
            # Bounded at BOTH ends, for the same reason the state machine's own window is (see
            # SleepStateMachine.transition). Every safety rule below stands down while this flag
            # is set -- the stale-data guard, the data-quality hold, the abandoned-session
            # timeout, the bed-exit check -- so an open-ended window disables the controller's
            # entire safety layer for the rest of the day.
            close_min = float(getattr(cfg.tunables, "wake_window_close_min", 60.0))
            wake_window_open = (required_wake - timedelta(minutes=lead_min)
                                <= now <= required_wake + timedelta(minutes=close_min))

        # --- abandoned-session timeout --------------------------------------------------------
        # Must run BEFORE the stale guard below, which returns early and freezes the state
        # machine -- that freeze is exactly what let a session outlive its physiology for ~10
        # hours on 2026-08-26 (see AppConfig.session_abandon_min). A session that cannot progress
        # (onset needs staging, staging needs a feed) and has no evidence anyone is in bed should
        # end, not persist. Never fires inside the wake window (the deadline outranks every
        # data-quality rule) and never contradicts a Pod that positively reports presence.
        # `_last_physio_at` is process-local, and this box auto-deploys and restarts the daemon
        # by design -- so without a recovered value every restart resets the abandon clock to
        # zero and a session that has been dead for hours looks a minute old. That is the same
        # failure already fixed for `_bed_entry_time` (see `restore_bed_entry`), and it is why
        # `restore_last_physio` exists: on a development day with frequent deploys the 60-minute
        # timeout can never elapse.
        if frame.heart_rate is not None:
            self._last_physio_at = now
            self._recovered_physio_at = None
        elif getattr(self, "_last_physio_at", None) is None:
            # First tick of this process with no physiology in hand: fall back to the persisted
            # value if the caller supplied one, and only to `now` if it did not.
            self._last_physio_at = self._recovered_physio_at or now
            self._recovered_physio_at = None
        abandon_min = float(getattr(cfg.tunables, "session_abandon_min", 60.0) or 0.0)
        if (abandon_min > 0
                and self.sm.state is not ControllerState.IDLE
                and not wake_window_open
                and frame.presence is not True
                and frame.heart_rate is None):
            gap_min = (now - self._last_physio_at).total_seconds() / 60.0
            if gap_min >= abandon_min:
                self.sm.state = ControllerState.IDLE
                self.sm.reason = f"session abandoned: no physiology for {gap_min:.0f} min"
                self._bed_entry_time = None
                self._sleep_onset_time = None
                self._reset_architecture()
                self._cold_since = None
                self._cold_relief_f = 0.0
                try:
                    self.onset_detector.reset()
                except Exception:
                    pass

        # --- BED EXIT on wearable evidence -----------------------------------------------
        # The abandon rule above only fires when the physiology STOPS. It cannot see the other
        # way a session outlives the night: the band is still worn, still streaming, and the
        # person wearing it is making breakfast. `arousal.py` grades that OUT_OF_BED, but only
        # from `presence is False`, which on this account is never. So this is the only thing
        # in the system that can end a session because the sleeper got up.
        try:
            # Feed the lying baseline only from ticks we believe are ASLEEP, so the ticks being
            # judged can never raise the bar they are judged against.
            if (self.sm.state is ControllerState.MAINTENANCE
                    and frame.stage not in (SleepStage.AWAKE, SleepStage.UNKNOWN)):
                self.bed_exit_detector.observe_sleeping(frame.heart_rate)
            bed_exit = self.bed_exit_detector.assess(frame, recent, cfg, now)
            self.last_bed_exit = bed_exit
            if (bed_exit.out_of_bed
                    and bool(getattr(cfg.tunables, "bed_exit_ends_session", True))
                    and self.sm.state is not ControllerState.IDLE
                    # The wake deadline outranks every other rule, and a Pod that positively
                    # reports presence outranks an inference drawn from a wristband.
                    and not wake_window_open
                    and frame.presence is not True):
                self.sm.state = ControllerState.IDLE
                self.sm.reason = ("bed exit: " + ", ".join(bed_exit.reasons)
                                  if bed_exit.reasons else "bed exit")
                self._bed_entry_time = None
                self._sleep_onset_time = None
                self._cold_since = None
                self._cold_relief_f = 0.0
                self._reset_architecture()
                self.bed_exit_events.append({
                    "ts": now.isoformat(), **bed_exit.to_dict()})
                try:
                    self.onset_detector.reset()
                    self.bed_exit_detector.reset()
                    self.hypnogram.reset()
                    self._preempt_ticks_maint = 0
                    self._maint_ticks = 0
                except Exception:
                    pass
        except Exception:
            # A detector fault must never take the control loop down with it.
            pass

        if frame.is_stale(cfg.tunables.stale_data_seconds) and not wake_window_open:
            level = self.thermal.to_level(self._last_target_f)
            decision = self._build(
                now, self.sm.state, objective, ThermalIntent.STABILIZE,
                self._last_target_f, level, CorrectionAction.HOLD,
                "data stale; holding last command", 0.3, frame,
                wake_signals=[],
            )
            self._record_decision(decision)
            return decision

        # --- data-quality gate: extends the stale-data guard with a graded trust score ----
        # (Feature #6). The hard stale-guard above already refused to act on data older than
        # ``stale_data_seconds``; this catches everything ELSE that makes a frame untrustworthy
        # (borderline staleness, high movement, missing vitals, uncertain presence, low stage
        # confidence) and, when bad enough, forces the same conservative HOLD — do no harm on
        # data we don't trust, never act aggressively on it.
        data_quality = assess_data_quality(frame, cfg, now)
        self.last_data_quality = data_quality
        # Confirmed bed-exit (presence is False, not just unknown) is high-confidence, actionable
        # information -- not untrustworthy data -- and the state machine MUST still be allowed to
        # transition to IDLE on it (that transition is itself the safe/neutral outcome). Gating
        # here on presence would instead FREEZE the state machine mid-WAKE/MAINTENANCE, which is
        # the opposite of do-no-harm. So the hard early-hold only applies while still in bed
        # (or presence is unknown); the score/reasons are still computed + surfaced either way.
        if (data_quality.score < cfg.tunables.data_quality_hold_score
                and frame.presence is not False and not wake_window_open):
            # OBSERVE before holding. Holding the thermal COMMAND on low-trust data is correct
            # (do no harm); refusing to even look for an awakening is not -- and this early
            # return used to skip arousal detection entirely.
            #
            # The failure was self-inflicting: the dominant data-quality penalty is high
            # movement, and high movement IS the awakening signal. So every real awakening
            # pushed the score under the hold threshold and made the controller blind to the
            # very event it was reacting to. Measured on a real night: 8 disruptions with
            # movement up to 1.00 and HR 74->97, and `wake_events` logged ZERO -- while
            # replaying the same frames through WakeDetector fired 4 events. Nothing learned
            # from those awakenings, and `wake_causation`/precursor training saw an empty night.
            #
            # Detection here is READ-ONLY: no pre-emption, no thermal change, no state
            # transition -- purely so the event is graded and logged.
            if self.sm.state in (ControllerState.MAINTENANCE, ControllerState.WAKE_RECOVERY):
                try:
                    hr_base, hrv_base = self._sleep_baseline(recent)
                    arousal = self.arousal_detector.assess(
                        frame, recent, now, hr_base, hrv_base)
                    self.last_arousal = arousal
                    self.last_wake_event = arousal.wake_event
                except Exception:
                    pass  # observation must never break the do-no-harm hold
            level = self.thermal.to_level(self._last_target_f)
            reason = (f"data quality low (score={data_quality.score:.2f}"
                     f"{', ' + data_quality.top_reason if data_quality.top_reason else ''}); "
                     "holding last command")
            decision = self._build(
                now, self.sm.state, objective, ThermalIntent.STABILIZE,
                self._last_target_f, level, CorrectionAction.HOLD,
                reason, round(0.3 * data_quality.score, 2), frame,
                wake_signals=(self.last_wake_event.signals if self.last_wake_event else []),
                data_quality=data_quality,
            )
            self._record_decision(decision)
            return decision

        # --- graded arousal detection (maintenance: detect + grade disturbances) -
        sleep_hr_base, sleep_hrv_base = self._sleep_baseline(recent)

        # --- vitals-based stage estimate (external HR sensor, e.g. Polar Verity Sense) ----------
        # When the Pod gives no sleep stage (UNKNOWN) but we DO have a fresh HR feed, derive a
        # coarse stage from HR/HRV/movement and overlay it here, at ONE point, so onset detection,
        # the state machine, architecture accrual and every downstream module see a usable stage
        # and can steer off the wearable alone. A real Pod stage (anything other than UNKNOWN)
        # always wins; the estimate carries a capped, sub-Pod confidence.
        self._stage_estimated = False
        self._stage_source = "sensor"
        # Require EITHER an open session (not IDLE) OR positive presence. ``presence`` is unknown
        # (None) for long stretches on this Pod, so `presence is not False` alone let the
        # estimator keep scoring all day with the user out of bed and the band sitting still on a
        # charger: a flat HR with no motion reads as "sustained quiescence below baseline", i.e.
        # DEEP. Measured on 2026-08-04: 281 of the 296 DEEP samples filed under that night_date
        # fell between 07:00 and 11:58 the MORNING AFTER, versus 15 during the actual night --
        # fake deep sleep that pollutes every night_date-keyed rollup, learner and report.
        #
        # Both arms are needed. `presence is True` alone would drop the Verity-only case, where
        # presence goes None for long mid-night stretches and staging must continue. `not IDLE`
        # alone would drop the bed-entry tick: this overlay runs BEFORE the state machine steps,
        # so on the first in-bed tick the machine is still IDLE. Together they block only the
        # daytime case -- IDLE with no positive presence -- and the machine leaves IDLE purely on
        # `presence is True`, never on stage, so onset detection is untouched either way.
        if (cfg.tunables.estimate_stage_from_vitals
                and frame.stage is SleepStage.UNKNOWN
                and frame.presence is not False
                and (self.sm.state is not ControllerState.IDLE or frame.presence is True)):
            mss = ((now - self._bed_entry_time).total_seconds() / 60.0
                   if self._bed_entry_time is not None else None)
            mso = ((now - self._sleep_onset_time).total_seconds() / 60.0
                   if self._sleep_onset_time is not None else None)
            est = estimate_sleep_stage(
                frame, sleep_hr_base, recent, cfg,
                minutes_since_start=mss, minutes_since_onset=mso,
                # MEASURED resting HR (not the trailing pool) -- the absolute wake anchor. None
                # until the resting baseline is learned, which disables that test on its own.
                resting_hr=(self.resting_baseline or {}).get("hr"))
            if est is not None:
                # Structural constraints FIRST, then hysteresis -- so the damping operates on
                # labels that are physiologically possible rather than smoothing impossible
                # ones into a confident, impossible average.
                est = constrain(est, now, cfg, self.hypnogram, self._sleep_onset_time)
                est = self._hold_stage(est, cfg)
                frame.stage, frame.stage_confidence, self._stage_source = est
                self._stage_estimated = True
                self.hypnogram.observe(frame.stage, now)

        arousal = None
        wake_detected = False
        self._preempt_cool = False
        if self.sm.state in (ControllerState.MAINTENANCE, ControllerState.WAKE_RECOVERY):
            arousal = self.arousal_detector.assess(
                frame, recent, now, sleep_hr_base, sleep_hrv_base)
            self.last_arousal = arousal
            wake_detected = arousal.is_awakening
            wake_event = arousal.wake_event
            # Proactive prevention: in maintenance, watch for wake PRECURSORS and pre-empt
            # with a gentle cooling assist before a disturbance becomes an awakening.
            if self.sm.state is ControllerState.MAINTENANCE and not wake_detected:
                mins_since_onset = (
                    (now - self._sleep_onset_time).total_seconds() / 60.0
                    if self._sleep_onset_time is not None else None)
                risk = self.wake_risk_assessor.assess(
                    frame, recent, now, target_temp_f=self._last_target_f,
                    sleep_hr_baseline=sleep_hr_base,
                    minutes_since_onset=mins_since_onset)
                self.last_wake_risk = risk
                # Leading-edge: detect the slow pre-arousal drift (HR creep, HRV decay,
                # building restlessness, bed warming) over a short window — earlier than the
                # point-in-time wake-risk score.
                precursor = self.precursor_detector.detect(
                    frame, recent, now, sleep_hr_base, sleep_hrv_base)
                self.last_precursor = precursor
                # Pre-empt on rising risk OR a leading-edge precursor OR a micro-arousal.
                evidence_backed = (
                    (risk.preempt and getattr(risk, "evidence_score", 0.0) > 0)
                    or precursor.should_preempt
                    or (arousal.level is ArousalLevel.MICRO
                        and frame.stage is not SleepStage.DEEP))
                self._preempt_cool = risk.preempt or precursor.should_preempt or (
                    arousal.level is ArousalLevel.MICRO and frame.stage is not SleepStage.DEEP)
                # DUTY CYCLE on the evidence-free ANTICIPATORY pre-cool.
                #
                # The pre-cool is meant to start cooling before a learned vulnerable window
                # arrives. Measured on 2026-08-30 it claimed 72% of pre-empting maintenance
                # ticks, which is not a run-up to anything -- it is the whole night. That was
                # tolerable while a pre-empt barely moved the bed; now that the settle actually
                # reaches the cool edge, an always-on pre-cool would park the bed at 67 F all
                # night, and this user's own record puts awakenings at the cold end too.
                #
                # Evidence-backed pre-empts are never rate-limited. Only the anticipatory-only
                # case stands down, and only once it has already had more than its share.
                self._preempt_ticks_maint = getattr(self, "_preempt_ticks_maint", 0)
                self._maint_ticks = getattr(self, "_maint_ticks", 0) + 1
                duty_max = float(getattr(cfg.tunables, "preempt_duty_cycle_max", 0.35) or 0.0)
                self.last_preempt_duty = (self._preempt_ticks_maint / self._maint_ticks
                                          if self._maint_ticks else 0.0)
                self.last_preempt_duty_capped = False
                if (duty_max > 0 and self._preempt_cool and not evidence_backed
                        and self._maint_ticks >= MIN_TICKS_FOR_DUTY_CYCLE
                        and self.last_preempt_duty > duty_max):
                    self._preempt_cool = False
                    self.last_preempt_duty_capped = True
                if self._preempt_cool:
                    self._preempt_ticks_maint += 1
                # 3AM WAKE targeted analysis: a fourth, purely ADDITIVE vote -- a HIGH-CONFIDENCE
                # personal recurring wake window (see sleepctl.analysis.wake_patterns) approaching
                # on the clock. Gated (enabled flag + min-nights + confidence) inside
                # ``should_preempt_window``; resolves to None (no effect, telemetry-only) whenever
                # no report is attached or the evidence hasn't cleared the bar yet.
                from sleepctl.analysis.wake_patterns import should_preempt_window
                self.last_wake_window_preempt = should_preempt_window(
                    self.wake_window_report, now, cfg)
                self._preempt_cool = self._preempt_cool or bool(self.last_wake_window_preempt)
                # Edge-trigger a pre-cool efficacy event when anticipatory cooling first
                # fires for a window (so the lead-time learner can later score prevention).
                anticip = next((r for r in risk.reasons if r.startswith("anticipatory_")), None)
                if anticip and not self._anticipatory_active:
                    wtype = anticip[len("anticipatory_"):]
                    eta, _ = self.wake_risk_assessor.profile.next_window_eta(
                        now, mins_since_onset)
                    lead = (self.wake_risk_assessor.lead_profile.lead_for(wtype)
                            if self.wake_risk_assessor.lead_profile else None)
                    self.pending_precool_event = {
                        "ts": now, "window_type": wtype,
                        "lead_used_min": lead if lead is not None else 0.0,
                        "eta_min": eta if eta is not None else 0.0,
                    }
                self._anticipatory_active = bool(anticip)
        else:
            wake_event = None
        self.last_wake_event = wake_event

        # Re-settling latency: time from an awakening to physiology re-stabilising.
        if wake_detected and self._arousal_started is None:
            self._arousal_started = now
        elif (not wake_detected and self._arousal_started is not None
              and self.sm.state is ControllerState.MAINTENANCE):
            self.last_resettle_latency_min = (
                now - self._arousal_started).total_seconds() / 60.0
            self._arousal_started = None

        # --- accurate sleep-onset detection (asleep vs lying in bed awake) -------
        # `presence is not False` (NOT truthiness): UNKNOWN presence must still register bed
        # entry, matching the convention used for wearable fusion. presence is None whenever the
        # Pod's presence can't be read -- permanently so on an account without an Autopilot
        # membership -- and `if frame.presence:` treated that None as "not in bed", so
        # _bed_entry_time was never set. That silently starved the stager of its
        # `minutes_since_start` feature (and left onset latency unanchored).
        #
        # Measured cost: with no time context the model cannot tell hour 5 from hour 1, and REM
        # is back-loaded, so it predicted REM in 0% of epochs -- against a documented CV recall
        # of 0.545. Replaying one real night with the time features restored: light 96%->64%,
        # rem 0%->27%, deep 0%->3%. The whole hypnogram had collapsed onto "light".
        if self._bed_entry_time is None and frame.presence is not False:
            # Prefer a RECOVERED bed entry over stamping `now`. This value is process-local, and
            # this system auto-deploys and restarts the daemon by design -- so a restart mid-night
            # would otherwise re-anchor bed entry to the restart moment. Measured on 2026-08-27:
            # all three sleep onsets reported a latency of ~1.0 min (one exactly 0.0) against a
            # rollup-computed SOL of 36.3 min, because each followed a restart.
            #
            # The damage is not just a wrong latency. `minutes_since_start` is a stager feature,
            # and losing it is what the comment above measures at REM 27% -> 0% with the whole
            # hypnogram collapsing onto LIGHT. A restart was silently degrading the staging for
            # the rest of the night.
            self._bed_entry_time = self._recovered_bed_entry or now
            self._recovered_bed_entry = None
            self.onset_detector.reset()
            self._sleep_onset_time = None
        onset_confirmed = None
        if self._sleep_onset_time is None and self.sm.state in (
            ControllerState.INDUCTION, ControllerState.IDLE, ControllerState.CALIBRATION,
        ):
            onset_event = self.onset_detector.evaluate(
                frame, recent, now, bed_entry_time=self._bed_entry_time)
            if onset_event is not None:
                self._sleep_onset_time = onset_event.timestamp
                self.last_onset_event = onset_event
            onset_confirmed = onset_event is not None

        # --- advance state machine ---------------------------------------------
        state_before = self.sm.state
        state = self.sm.transition(frame, now, wake_detected, required_wake,
                                   onset_confirmed=onset_confirmed,
                                   wearable_bed_entry=self._wearable_bed_entry(frame, recent, cfg))

        minutes_in_bed = (
            (now - self._bed_entry_time).total_seconds() / 60.0
            if self._bed_entry_time
            else 0.0
        )

        # --- accrue the realized architecture (drives in-night steering) -------
        if self._sleep_onset_time is not None and frame.presence is not False:
            self._accrue_architecture(now, frame.stage)
            # Feed the ultradian cycle predictor ALL night, not just inside WAKE_WINDOW (where
            # wake_orch.evaluate runs). It learns this night's deep-bout length from observed stage
            # transitions and its confidence grows with them, so observing only during the wake
            # window left it with an empty history, a generic bout estimate and pinned-low
            # confidence -- i.e. an uninformed trajectory prediction exactly when it matters.
            self.wake_orch.observe_stage(now, frame.stage)
        self.last_cycle_state = self.wake_orch.cycle_state(now, frame.stage)

        # --- pick thermal intent per state -------------------------------------
        self.should_wake = False
        self.last_wake_action = None      # only set inside WAKE_WINDOW (drives lights/therapy)
        if state in (ControllerState.IDLE, ControllerState.CALIBRATION):
            # Night ended: reset onset tracking and the realized architecture for the next one.
            #
            # This used to require `presence is False`, which on an account with no Autopilot
            # membership NEVER happens -- the same gate that left the system with no bed-exit
            # path at all. The consequence here is quieter and worse: `_arch_rem_min` and
            # `_arch_deep_min` are what IN-NIGHT STEERING reads, and without a reset they
            # accumulate across days. Measured on 2026-08-31, the steerer believed 402.4 min of
            # REM in an 8.3-hour night; the night's own stage record and the nightly rollup both
            # say ~70. It was steering by a number six times too large, which is how it came to
            # "defend" a 300-minute REM surplus that did not exist.
            #
            # Reaching IDLE at all is the end of a session, whatever the Pod will or will not
            # say about presence, so that is the condition.
            # ...on the TRANSITION into IDLE, not on every idle tick. Bed entry is stamped
            # while still IDLE (onset detection runs from IDLE/INDUCTION/CALIBRATION), so
            # clearing it unconditionally here would wipe the anchor a moment after it was set --
            # including one recovered across a restart.
            if state_before is not ControllerState.IDLE:
                self._bed_entry_time = None
                self._sleep_onset_time = None
                self.onset_detector.reset()
                self.wake_orch.reset()
                self._reset_architecture()
                self.hypnogram.reset()
                self._preempt_ticks_maint = 0
                self._maint_ticks = 0
            intent = ThermalIntent.NEUTRAL
            self._induction_entered_at = None  # left induction -> next entry restarts the cascade
        elif state is ControllerState.INDUCTION:
            # Start (or restart, on a fresh "help me fall asleep" press) the cascade clock so
            # phase 1 (cold settle) always begins NOW, regardless of how long you've been in bed.
            if self._induction_restart or self._induction_entered_at is None:
                self._induction_entered_at = now
                self._induction_restart = False
            induction_minutes = (now - self._induction_entered_at).total_seconds() / 60.0
            # Refresh the cascade's phase target levels from tonight's live thermal targets so the
            # reach-aware warm-pulse sizing uses the actual cold/warm/consolidate levels.
            self._sync_induction_phase_levels(objective)
            intent = self.induction.step(frame, objective, induction_minutes)
        elif state is ControllerState.MAINTENANCE:
            # In-night architecture steering: compare the realized deep/REM curve to tonight's
            # personalized ideal and, when behind on deep + light + wake-risk LOW, nudge deeper —
            # reconciled with the wake-up trajectory (stands down near the deadline).
            deepen = self._evaluate_steering(now, frame, wake_detected, minutes_in_bed,
                                             required_wake)
            intent = self.maintenance.step(frame, objective,
                                           preempt_cool=getattr(self, "_preempt_cool", False),
                                           keep_light=self.session_keep_light, deepen=deepen)
        elif state is ControllerState.WAKE_RECOVERY:
            self._deepen_active = False     # an awakening breaks any active deepen maneuver
            intent = self.wake_recovery.step(frame)
        elif state is ControllerState.WAKE_WINDOW:
            # Multi-signal orchestrator: fuse the calibrated P(wake) with stage to catch a real
            # light-sleep moment early, run the thermal dawn, escalate vibration silently, and
            # guarantee the deadline. Falls back to stage-only when data is stale.
            stale = (frame.data_age_seconds is not None
                     and frame.data_age_seconds > self.cfg.tunables.telemetry_stale_seconds)
            action = self.wake_orch.evaluate(
                now, frame, recent, required_wake,
                hr_base=sleep_hr_base, hrv_base=sleep_hrv_base, data_stale=stale,
                debt_min=self.wake_debt_min)
            self.last_wake_action = action
            intent, self.should_wake = action.thermal_intent, action.should_wake
            # Program the device's native vibration+heat smart alarm as the hardware backstop.
            self.pending_wake_alarm = self.smart_wake.alarm_spec(now, required_wake)
            if self.pending_wake_alarm is not None and action.vibration_power:
                self.pending_wake_alarm.vibration_power = action.vibration_power
        else:
            intent = ThermalIntent.NEUTRAL

        # --- composite temperature inputs --------------------------------------
        # Exposed-skin ambient = bedroom air. There is deliberately NO outdoor-weather fallback
        # here any more.
        #
        # The composite model inverts as water = (effective - (1-a)*ambient)/a, so with the
        # default a=0.75 the ambient term is DIVIDED by 0.75 -- it amplifies. Outdoor air is a
        # poor proxy for bedroom air (a house does not track the sky), and on a measured night
        # the forecast ran 62.3 -> 84.5 F while the effective target barely moved: that 22 F
        # swing alone moves the commanded water ~7.4 F, i.e. ~32 device levels. The bed drifted
        # -37 -> -80 and the sleeper woke at BOTH ends. Feeding an unmeasured, amplified,
        # wrong-location number into an open-loop inversion is worse than not compensating at
        # all: with ambient None the inversion returns the effective target unchanged, which is
        # the honest behaviour when the exposed-skin term is genuinely unknown.
        #
        # Outdoor weather still drives the SEPARATE feed-forward bias
        # (``ThermalController.set_ambient_bias``), which is explicitly capped by
        # ``precomp_max_bias_f`` -- bounded pre-compensation is fine; an unbounded amplified
        # divisor is not.
        ambient_temp_f = frame.room_temp_f
        if ambient_temp_f is None and getattr(cfg.tunables, "ambient_outdoor_fallback", False):
            ambient_temp_f = context.outdoor_temp_f if context is not None else None
        # Covered-body signal = the Pod's measured bed-surface temperature.
        bed_temp_f = frame.bed_temp_f

        # --- resolve safe target + level (composite feedback) ------------------
        # The water command is nudged so the blended effective temperature hits target;
        # slew is anchored to the last command so the device never jumps > max_step_f.
        # While PRE-EMPTING, settle deeper than the ordinary post-awakening nudge -- see
        # AppConfig.preempt_settle_nudge_f. The comfort clamp downstream still bounds it to the
        # measured band, so "deeper" can never mean "colder than this user tolerates".
        settle_nudge = (float(getattr(cfg.tunables, "preempt_settle_nudge_f", -2.5))
                        if getattr(self, "_preempt_cool", False) else None)
        target_f, level = self.thermal.resolve(
            intent, objective, cfg.profile.hot_sleeper, self._last_target_f,
            bed_temp_f, ambient_temp_f, now=now, settle_nudge_f=settle_nudge,
        )
        self._settle_nudge_used = settle_nudge

        # --- correction action vs current bed temp -----------------------------
        action = self._action_for(current_f, target_f)
        reason = self._reason(state, intent, wake_event)
        if not wake_detected:
            confidence = 0.9
        else:
            arousal_conf = arousal.confidence if arousal is not None else 0.6
            confidence = min(0.9, arousal_conf + 0.3)
        # The Pod senses HR/HRV/RR via ballistocardiography, which needs stillness, so
        # discount confidence when there is significant movement (biometrics less reliable).
        confidence *= self._biometric_reliability(frame)
        # Data-quality gate (Feature #6): a borderline-trustworthy frame (above the hard HOLD
        # floor but below the downweight floor) further discounts confidence proportionally —
        # extends the movement-only discount above with the fuller multi-signal picture.
        if data_quality.score < cfg.tunables.data_quality_downweight_score:
            confidence *= data_quality.score
            if data_quality.top_reason:
                reason += f" [data_quality={data_quality.score:.2f}:{data_quality.top_reason}]"

        # --- decision guardrail (Feature #8): trajectory-level do-no-harm backstop --------
        # Runs over the recent decision/frame history (not this tick's sub-modules) looking for
        # invariant violations a single-tick check would miss. A CRITICAL finding overrides to a
        # SAFE HOLD (revert toward the last-good target, no aggressive move); non-critical
        # findings are just surfaced for visibility.
        guardrail = self.guardrail.evaluate(
            recent, self._recent_decisions, target_f, now,
            sleep_hr_baseline=sleep_hr_base,
            comfort_profile=self.comfort_profile,
            thermal_health=self.thermal_health_status,
        )
        self.last_guardrail = guardrail
        if guardrail.critical:
            # The LEARNED neutral, not cfg.tunables.neutral_temp_f. That tunable is the
            # population default (70.0 F) and is exactly the anchor the comfort sweep exists to
            # replace -- reverting a critical guardrail toward it would drag the bed to the
            # temperature the user's own calibration says is too warm, i.e. the "safe" hold would
            # walk into the failure mode. Falls back to the tunable when no profile is loaded.
            safe_f = getattr(getattr(self.thermal, "profile", None), "neutral_f", None)
            if safe_f is None:
                safe_f = cfg.tunables.neutral_temp_f
            # Revert toward neutral gently — never jump further than the normal max step.
            step = cfg.tunables.max_step_f
            if safe_f > self._last_target_f:
                target_f = min(safe_f, self._last_target_f + step)
            elif safe_f < self._last_target_f:
                target_f = max(safe_f, self._last_target_f - step)
            else:
                target_f = self._last_target_f
            level = self.thermal.to_level(target_f)
            # Label the action from the actual step taken (toward neutral), not unconditionally
            # HOLD -- this keeps _recent_decisions truthful for the guardrail's own
            # _check_driving_arousal streak logic and any other trajectory-level analysis.
            action = self._action_for(self._last_target_f, target_f)
            codes = ",".join(f.code for f in guardrail.findings if f.severity == "critical")
            reason = f"guardrail critical ({codes}); safe hold toward neutral"
            confidence = min(confidence, 0.3)
            # Keep the thermal controller's internal bookkeeping (variability-cap window +
            # latency-damping anchor) in sync with this override, so next tick's resolve()
            # doesn't fight stale un-overridden history (see ThermalController.note_override).
            self.thermal.note_override(target_f, now=now)

        # --- personal comfort CLAMP: a hard bound, not just a warning ---------------------
        # The guardrail above only FLAGS an out-of-band target ("never picks a target itself"),
        # and the only hard clamp in the stack is the device's 55-110 F range -- useless here,
        # because a real usable range spans ~2 F (levels -80..-37 on one measured night). So
        # nothing physically stopped an open-loop drift, and one did happen: the bed ran to the
        # too-warm edge for hours (awakenings every ~20 min), then overshot to the too-cold edge
        # (three more awakenings within 7 min).
        #
        # With NO sensed bed temperature (paywalled) the thermal loop is open-loop by
        # construction, so a bound on the commanded target is the only backstop available.
        # Applied ONLY in the long sleep-holding states: INDUCTION owns a deliberately cold
        # opener and WAKE_WINDOW a deliberately warm ramp, and clamping those would break
        # designed behaviour rather than protect sleep.
        clamped_from = None
        if (getattr(cfg.tunables, "comfort_clamp_enabled", True)
                and state in (ControllerState.MAINTENANCE, ControllerState.WAKE_RECOVERY)
                and isinstance(self.comfort_profile, dict)):
            lo_edge = self.comfort_profile.get("cool_edge_f")
            hi_edge = self.comfort_profile.get("warm_edge_f")
            margin = getattr(cfg.tunables, "comfort_clamp_margin_f", 0.5)
            if lo_edge is not None and hi_edge is not None and hi_edge >= lo_edge:
                lo, hi = float(lo_edge) - margin, float(hi_edge) + margin
                bounded = max(lo, min(hi, target_f))
                if abs(bounded - target_f) > 1e-9:
                    clamped_from, target_f = target_f, bounded
                    level = self.thermal.to_level(target_f)
                    action = self._action_for(self._last_target_f, target_f)
                    reason = (f"{reason}; clamped to personal comfort band "
                              f"{lo:.1f}-{hi:.1f}F (was {clamped_from:.1f}F)")
                    # Keep the thermal controller's slew/variability bookkeeping consistent with
                    # the value actually commanded, exactly as the guardrail override does.
                    self.thermal.note_override(target_f, now=now)

        # --- target stabilizer (trial arm C) --------------------------------------------------
        # Damps thermal hunting: a move must clear a deadband, and a move that REVERSES the last
        # direction must also wait out a minimum dwell. See AppConfig.target_stabilizer.
        #
        # Runs LAST but deliberately yields to both safety layers above -- it is skipped entirely
        # when a critical guardrail forced a safe hold or the comfort clamp bounded the target.
        # Holding a stale value over either of those would convert a damping policy into a way of
        # ignoring the guardrail, which is the opposite of what it is for. Confined to the long
        # holding states for the same reason the comfort clamp is: INDUCTION owns a deliberately
        # cold opener and WAKE_WINDOW a warm ramp, and damping those would break designed
        # behaviour rather than protect sleep.
        if (getattr(cfg.tunables, "target_stabilizer", False)
                and state in (ControllerState.MAINTENANCE, ControllerState.WAKE_RECOVERY)
                and not (guardrail is not None and guardrail.critical)
                and clamped_from is None):
            held, why = self._stabilize_target(
                target_f, now, cfg, preempting=bool(getattr(self, "_preempt_cool", False)))
            if held is not None:
                target_f = held
                level = self.thermal.to_level(target_f)
                action = self._action_for(self._last_target_f, target_f)
                reason = f"{reason}; {why}"
                self.thermal.note_override(target_f, now=now)
            else:
                self._note_target_move(target_f, now)

        # --- sustained-cold relief (safety property, applies to EVERY arm) ---------------------
        # Runs last, after the stabilizer, and deliberately outranks it: a damping policy must not
        # be able to hold the bed parked at the cold edge, which is the exact harm this prevents.
        # One-directional (warmer only) and capped, so the worst case is slight under-cooling.
        if (getattr(cfg.tunables, "cold_dwell_relief_enabled", True)
                and state in (ControllerState.MAINTENANCE, ControllerState.WAKE_RECOVERY)):
            eased, why = self._cold_dwell_relief(target_f, now, cfg)
            if eased is not None and eased > target_f:
                target_f = eased
                level = self.thermal.to_level(target_f)
                action = self._action_for(self._last_target_f, target_f)
                reason = f"{reason}; {why}"
                self.thermal.note_override(target_f, now=now)
        else:
            self._cold_since = None

        self._last_target_f = target_f
        decision = self._build(
            now, state, objective, intent, target_f, level, action, reason,
            confidence, frame,
            wake_signals=wake_event.signals if wake_event else [],
            minutes_in_bed=minutes_in_bed,
            ambient_temp_f=ambient_temp_f,
            data_quality=data_quality,
            guardrail=guardrail,
        )
        self._record_decision(decision)
        return decision

    # -- helpers -----------------------------------------------------------------
    @staticmethod
    def _round_opt(value, ndigits: int = 2):
        return round(value, ndigits) if value is not None else None

    def set_session(self, mode: str, keep_light: Optional[bool] = None) -> None:
        """Select the session mode ('night' | 'induce' | 'nap_power' | 'nap_cycle'). Power
        naps keep the bed light so slow-wave sleep doesn't set in."""
        self.session_mode = mode or "night"
        if keep_light is None:
            keep_light = mode in ("nap_power",)
        self.session_keep_light = bool(keep_light)
        # "Help me fall asleep" / nap modes FORCE an induction session immediately. The user
        # explicitly asked to be put to sleep NOW, so we must not wait for cloud PRESENCE to flip
        # True -- on Eight Sleep presence is derived from a sleep SESSION, which only opens
        # retroactively AFTER onset, so a presence-gated induction would never fire pre-sleep
        # (the exact bug: session_mode='induce' but state stuck IDLE). Jump straight into
        # INDUCTION from IDLE/CALIBRATION so the onset thermal cascade runs open-loop right away;
        # confirmed onset (once physiology arrives) then hands off to MAINTENANCE as usual.
        if self.session_mode in ("induce", "nap_power", "nap_cycle"):
            # Restart the onset cascade clock so cold-settle begins NOW on every press, even if
            # already in INDUCTION or lying awake in bed for a while.
            self._induction_restart = True
            if self.sm.state in (ControllerState.IDLE, ControllerState.CALIBRATION):
                self.sm.state = ControllerState.INDUCTION
                self.sm.reason = "induction forced on user request (help me fall asleep)"

    @property
    def sleep_onset_time(self) -> Optional[datetime]:
        """The measured, confirmed sleep-onset timestamp for the current bed session (None until
        onset is confirmed by ``SleepOnsetDetector``). This is what on-demand nap/induce sessions
        must anchor their dosing to — NOT the moment the user pressed the button — since sleep
        inertia is governed by time actually ASLEEP, not time in bed (see ``sleepctl.controller.
        nap``)."""
        return self._sleep_onset_time

    def update_nap_keep_light(self, keep_light: bool) -> None:
        """Adjust the active session's keep-light policy WITHOUT re-forcing the onset cascade
        (unlike ``set_session``). Used when a nap is RE-PLANNED after onset is already confirmed
        (see ``sleepctl.controller.nap.replan_on_onset``) — onset has already happened, so
        restarting the induction cascade here would be wrong; only whether maintenance keeps the
        bed light (avoids driving deep cooling) needs to change."""
        self.session_keep_light = bool(keep_light)

    def preemption_summary(self) -> dict:
        """Live predictive-pre-emption state for the dashboard: is the controller actively
        heading off an awakening, and which signals (point-in-time risk + leading-edge drift)
        drove it."""
        risk = self.last_wake_risk
        pre = self.last_precursor
        preempting = bool(getattr(self, "_preempt_cool", False))
        return {
            "preempting": preempting,
            "intent": "settle_cool" if preempting else None,
            "wake_risk": round(risk.score, 3) if risk else None,
            # Split out so a saturated score is visible per tick: a risk of 0.58 built from
            # 0.28 of evidence plus 0.30 of clock is a different fact from 0.58 of pure clock,
            # and the flat number could not tell them apart.
            "wake_risk_evidence": (round(getattr(risk, "evidence_score", 0.0), 3)
                                   if risk else None),
            "wake_risk_context": (round(getattr(risk, "context_score", 0.0), 3)
                                  if risk else None),
            "risk_reasons": list(risk.reasons) if risk else [],
            "precursor_score": round(pre.score, 3) if pre else None,
            "precursor_reasons": list(pre.reasons) if pre else [],
            "precursor_signals": pre.signals if pre else {},
            # 3AM WAKE targeted analysis: whether a personal recurring wake window is the (or
            # part of the) reason we're pre-empting right now -- None when no gated window fired.
            "wake_window_preempt": self.last_wake_window_preempt,
        }

    def data_quality_summary(self) -> dict:
        """Live data-quality-gate state for the dashboard: current trust score + top reason,
        and whether it's currently forcing/would force a conservative HOLD."""
        dq = self.last_data_quality
        if dq is None:
            return {"score": None, "reasons": [], "top_reason": None, "gating": False}
        gating = dq.score < self.cfg.tunables.data_quality_hold_score
        return {
            "score": round(dq.score, 3),
            "reasons": list(dq.reasons),
            "top_reason": dq.top_reason,
            "gating": gating,
        }

    def guardrail_summary(self) -> dict:
        """Live decision-guardrail state for the dashboard: any current findings and whether a
        CRITICAL one is forcing a safe hold this tick."""
        gr = self.last_guardrail
        if gr is None:
            return {"triggered": False, "critical": False, "findings": []}
        return gr.to_dict()

    def set_comfort_profile(self, profile: Optional[dict]) -> None:
        """Attach the learned personal comfort band (``repo.get_comfort_profile()``) so the
        guardrail can flag a target outside it. Optional — absence just skips that check."""
        self.comfort_profile = profile or None

    def set_thermal_health_status(self, status) -> None:
        """Attach the latest ``ThermalHealth`` (device-responsiveness) reading so the guardrail
        can fold sustained commanded-vs-device divergence into its trajectory checks. Optional —
        absence just skips that check (the dedicated ThermalResponseMonitor still runs
        independently wherever it's already wired)."""
        self.thermal_health_status = status

    def _record_decision(self, decision: Decision, max_history: int = 60) -> None:
        """Keep a bounded trailing window of decisions so the guardrail can see the recent
        TRAJECTORY (oscillation, sustained cooling runs) without every caller threading decision
        history through ``decide()``."""
        self._recent_decisions.append(decision)
        if len(self._recent_decisions) > max_history:
            self._recent_decisions = self._recent_decisions[-max_history:]

    def set_night_targets(self, targets, est_sleep_min: Optional[float] = None) -> None:
        """Hand the controller tonight's PERSONALIZED ideal architecture (from the SleepPlan) so the
        in-night steerer can chase the same deep/REM curve the dashboard shows and the policy
        learns. ``est_sleep_min`` is the expected sleep duration the trajectory is scaled to."""
        self.night_targets = targets
        if est_sleep_min is not None:
            self.est_sleep_min = float(est_sleep_min)

    def _evaluate_steering(self, now, frame, wake_detected, minutes_in_bed,
                           required_wake=None) -> bool:
        """Run the in-night steerer (MAINTENANCE only). Returns True to nudge deeper this tick.

        This is where the three in-night thermal maneuvers RECONCILE, by a strict precedence:
          1. wake-PREVENTION wins — ``risk_low`` requires no detected awakening AND no active
             pre-empt (which folds in rising wake-risk, the leading-edge precursor, and a
             micro-arousal), so the steerer never fights a brewing disturbance (maintenance first);
          2. wake-UP handoff — within the pre-wake standoff of the deadline the steerer stands
             down so the smart-wake ramp owns the bed (no deepening into sleep inertia);
          3. then the favorable-state controller acts: ACQUIRE deeper when behind, or DEFEND the
             deep/REM state you're already in.
        """
        cfg = self.cfg
        if self.night_targets is None or not cfg.tunables.inight_steering_enabled \
                or self.session_keep_light:
            self._deepen_active = False
            return False
        # Refuse to steer by an architecture that cannot be true. The steerer's inputs are the
        # accrued deep/REM minutes, and on 2026-08-30 those read 336 min of REM against 2 min of
        # deep -- so it concluded a 216-minute REM surplus and spent the night defending it.
        # Standing down is the correct response to a bad measurement; acting on it is not.
        ok, why = architecture_plausible(self._arch_deep_min, self._arch_rem_min,
                                         getattr(self, "_arch_light_min", 0.0))
        self.last_architecture_implausible = None if ok else why
        if not ok:
            self._deepen_active = False
            return False
        mso = ((now - self._sleep_onset_time).total_seconds() / 60.0
               if self._sleep_onset_time is not None else minutes_in_bed)
        est = self.est_sleep_min or getattr(self.night_targets, "total_sleep_target_min", 0) or 0.0
        risk_low = (not wake_detected) and (not getattr(self, "_preempt_cool", False))
        mins_to_wake = ((required_wake - now).total_seconds() / 60.0
                        if required_wake is not None else None)
        steer = self.steering.evaluate(
            minutes_since_onset=mso, est_sleep_min=est,
            deep_min_so_far=self._arch_deep_min, rem_min_so_far=self._arch_rem_min,
            current_stage=frame.stage, targets=self.night_targets, risk_low=risk_low,
            minutes_to_wake=mins_to_wake)
        self.last_steer = steer
        deepen = steer.deepen
        # n-of-1 control: ACTUATE only on 'act' nights; on 'observe'/disabled nights the steerer
        # still judges + logs a SHADOW event (applied=0) but does NOT cool — that's the control arm
        # the deepening-response learner compares against (does cooling beat the natural base rate?).
        actuate = deepen and self.steer_actuate
        # Edge-trigger the steer-event ledger when the deepen VERDICT first starts (either arm), so
        # the learner scores stage response + any awakening for both actuated and control nights.
        if deepen and not self._deepen_active:
            self.pending_steer_event = {
                "ts": now,
                "maneuver": steer.maneuver,
                "stage_before": frame.stage.value if frame.stage is not None else None,
                "deep_deficit_min": round(steer.deep_deficit_min, 2),
                "frac_of_night": round(steer.frac_of_night, 3),
                "horizon_min": self._steer_horizon_min(),
                "applied": 1 if actuate else 0,
            }
        self._deepen_active = deepen
        return actuate

    def set_precursor_profile(self, profile) -> None:
        """Apply the learned, personalized awakening-precursor trajectory to the precursor detector,
        so pre-emption triggers on the drift pattern that actually precedes YOUR awakenings."""
        self.last_precursor_profile = profile
        try:
            self.precursor_detector.personalize(profile)
        except Exception:
            pass

    def set_wake_window_report(self, recurring_windows) -> None:
        """Attach the personal recurring-wake-window report (the ``recurring_windows`` list from
        ``sleepctl.analysis.wake_patterns.wake_analysis_report``) so maintenance can preemptively
        smooth the bed a little before a HIGH-CONFIDENCE recurring wake window. Purely additive to
        the existing wake-risk/precursor/micro-arousal pre-empt union; a no-op (log-only) below
        the configured nights/confidence gate -- see ``should_preempt_window``."""
        self.wake_window_report = recurring_windows

    def set_resting_baseline(self, baseline: Optional[dict]) -> None:
        """Apply the in-bed resting-physiology baseline (used as the early-night HR/HRV anchor)."""
        self.resting_baseline = baseline or None

    def set_measured_thermal(self, cool_lag_min, heat_lag_min) -> None:
        """Apply the in-bed self-test's measured cool/heat effect-latency (minutes-to-settle):
        floors the deepening horizon (cool) and widens the wake warm-up runway (heat)."""
        self.measured_cool_lag_min = cool_lag_min
        self.measured_heat_lag_min = heat_lag_min
        self.wake_orch.set_warm_lead(self.warm_lead_min())

    def _steer_horizon_min(self) -> float:
        """Deepening-response scoring horizon, floored at the measured cool-lag (+2 min) so the
        learner never judges 'did cooling deepen me?' before the cool has actually landed."""
        base = self.cfg.tunables.steer_response_horizon_min
        if self.measured_cool_lag_min:
            return round(max(base, self.measured_cool_lag_min + 2.0), 1)
        return base

    def set_induction_latency(self, model: Optional[ThermalLatencyModel]) -> None:
        """Attach the reach-time model (built by the daemon from the repo) so the induction cascade
        sizes its warm-pulse phase to the bed's real warm-from-cold speed. None-safe; also refreshes
        the wake warm-up lead in case the model widens it (never shortens — see ``warm_lead_min``)."""
        self.induction_latency = model
        try:
            self.induction.set_latency(model)
        except Exception:
            pass
        try:
            self.wake_orch.set_warm_lead(self.warm_lead_min())
        except Exception:
            pass

    def _sync_induction_phase_levels(self, objective) -> None:
        """Compute the device levels of the warm-first onset phases from tonight's live thermal
        targets and hand them to the induction routine so its reach-aware pulse sizing is grounded
        in the real targets. The warm-first cascade starts near the consolidate/neutral level (not a
        cold floor), so that level is the reach baseline. Defensive: never raises."""
        try:
            hot = self.cfg.profile.hot_sleeper
            warm_f = self.thermal.target_for(ThermalIntent.ONSET_WARM, objective, hot)
            cool_f = self.thermal.target_for(ThermalIntent.INDUCTION_COOL, objective, hot)
            self.induction.set_phase_levels(
                fahrenheit_to_level(cool_f),   # reach baseline: warm-first starts near cool/neutral
                fahrenheit_to_level(warm_f),
                fahrenheit_to_level(cool_f),
            )
        except Exception:
            pass

    def warm_lead_min(self) -> Optional[float]:
        """How many minutes before the wake deadline the warming ramp should begin so the bed is
        actually warm by then — the measured heat-lag (+2 min margin), or None if uncalibrated.
        Consumed by the wake orchestrator to widen a too-short warm-up runway.

        Widening-only reach-awareness: when a latency model AND the current + wake-target levels are
        available, the traverse term can only ENLARGE the runway (``max`` with the lag-based value),
        never shrink it — so we never wake the user late, only start warming earlier for a big gap."""
        base: Optional[float] = None
        if self.measured_heat_lag_min:
            base = round(self.measured_heat_lag_min + 2.0, 1)
        reach = self._wake_reach_lead()
        if base is None and reach is None:
            return None
        return round(max(base or 0.0, reach or 0.0), 1)

    def _wake_reach_lead(self) -> Optional[float]:
        """Reach-based wake warm-up lead (current level -> wake-ramp target) if both the latency
        model and the levels are available; else None so ``warm_lead_min`` keeps today's value."""
        model = self.induction_latency
        if model is None:
            return None
        try:
            cur_f = self._last_target_f
            if cur_f is None:
                return None
            hot = self.cfg.profile.hot_sleeper
            wake_f = self.thermal.target_for(
                ThermalIntent.WAKE_RAMP, NightObjective.OPTIMIZE, hot)
            cur_level = fahrenheit_to_level(cur_f)
            wake_level = fahrenheit_to_level(wake_f)
            # Only a WARMING gap needs a longer runway; a cool/no-op gap returns 0 and is ignored.
            if wake_level <= cur_level:
                return None
            return round(model.lead_minutes(cur_level, wake_level), 1)
        except Exception:
            return None

    def set_steer_policy(self, actuate: bool) -> None:
        """Set whether tonight ACTUATES the deepen nudge (learned do-no-harm gate + the n-of-1
        control schedule). False = a control/observe night: judge + shadow-log, but don't cool."""
        self.steer_actuate = bool(actuate)

    def _reset_architecture(self) -> None:
        self._arch_deep_min = self._arch_rem_min = self._arch_light_min = 0.0
        self._arch_last_ts = None
        self._deepen_active = False
        self.last_steer = None

    def _accrue_architecture(self, now: datetime, stage) -> None:
        """Accumulate realized time-in-stage since onset (the night's unfolding architecture).
        Ignores gaps/jumps so a stale tick can't inflate a bucket.

        A LONG GAP ENDS THE NIGHT, here, unconditionally. The reset on entering IDLE is the
        intended path, but it lives in the intent block and several guards return from the tick
        before reaching it -- the stale-data guard in particular, which fires on every tick of a
        day with no wearable. So on 2026-09-04 the accumulator still carried 2026-09-01's totals
        across three idle days: deep read 57.9 min, which is 30.3 from the earlier night plus
        27.6 from this one, and REM read 97.9 against a rollup that scored ZERO REM.

        Doing it here catches every path, because this is the one place that sees the gap.
        """
        if self._arch_last_ts is not None:
            gap = (now - self._arch_last_ts).total_seconds() / 60.0
            if gap > ARCHITECTURE_GAP_RESET_MIN or gap < 0:
                self._reset_architecture()
        if self._arch_last_ts is not None and stage is not None:
            dt = (now - self._arch_last_ts).total_seconds() / 60.0
            if 0.0 < dt <= 10.0:
                if stage is SleepStage.DEEP:
                    self._arch_deep_min += dt
                elif stage is SleepStage.REM:
                    self._arch_rem_min += dt
                elif stage is SleepStage.LIGHT:
                    self._arch_light_min += dt
        self._arch_last_ts = now

    def steering_summary(self) -> dict:
        """Live in-night steering state for the dashboard: are we actively nudging deeper, and how
        far off the ideal deep/REM curve are we right now."""
        s = self.last_steer
        maneuver = s.maneuver if s else "hold"
        verdict_deepen = bool(self._deepen_active)
        return {
            "active": verdict_deepen and self.steer_actuate,     # actually nudging deeper (acquire)
            "observing": verdict_deepen and not self.steer_actuate,  # control night: judging, not cooling
            "defending": maneuver in ("defend_deep", "defend_rem"),  # holding a favorable state
            "maneuver": maneuver,
            "deep_deficit_min": round(s.deep_deficit_min, 1) if s else None,
            "rem_deficit_min": round(s.rem_deficit_min, 1) if s else None,
            "frac_of_night": round(s.frac_of_night, 3) if s else None,
            "deep_min_so_far": round(self._arch_deep_min, 1),
            "rem_min_so_far": round(self._arch_rem_min, 1),
            "reason": s.reason if s else None,
        }

    def set_settle_nudge(self, nudge_f: float) -> None:
        """Apply the learned signed maintenance settle nudge to the thermal controller."""
        self.thermal.set_settle_nudge(nudge_f)

    def set_onset_warm(self, warm_f: float) -> None:
        """Apply tonight's learned (per-mode, explored) onset warm nudge to the induction phase."""
        self.thermal.set_onset_warm(warm_f)

    def set_onset_cold_settle(self, cold_f: float) -> None:
        """Apply tonight's learned (per-mode, explored) really-cold onset opener target."""
        self.thermal.set_onset_cold_settle(cold_f)

    def set_warm_pulse_arm(self, on: bool) -> None:
        """Arm/disarm tonight's brief onset warm pulse (the induction A/B toggle)."""
        self.induction.set_warm_pulse_arm(on)

    def set_wake_window(self, minutes: int) -> None:
        """The time selector sets the per-night smart-wake window ceiling (choose_wake_window)."""
        self.wake_orch.cfg.window_min = max(1, int(minutes))

    def set_dawn_light(self, enabled: bool) -> None:
        """Tell the orchestrator a smart-bulb sunrise is wired up, so it actually computes a
        ramping ``light_level`` through the dawn window (otherwise it stays 0 and only the
        therapy plug — which keys off ``should_wake`` — would fire). The daemon calls this when a
        Hue dawn driver with sunrise targets is configured."""
        self.wake_orch.cfg.light_enabled = bool(enabled)

    def set_wake_ramp_f(self, wake_f: float) -> None:
        """Apply the learned per-person thermal wake maneuver (the WAKE_RAMP target temperature)."""
        from dataclasses import replace
        self.thermal.profile = replace(self.thermal.profile, wake_ramp_f=float(wake_f))

    def set_setpoints(self, profile) -> None:
        """Swap the active SetpointProfile for the night (e.g. an experiment arm applied on top
        of the learned setpoint). No-op on None so callers can pass through safely."""
        if profile is not None:
            self.thermal.profile = profile

    def set_wake_profile(self, profile=None, lead_profile=None) -> None:
        """Attach the learned per-user awakening phenotype + cooling lead-times to the
        wake-risk assessor (proactive sleep maintenance)."""
        if profile is not None:
            self.wake_risk_assessor.profile = profile
        if lead_profile is not None:
            self.wake_risk_assessor.lead_profile = lead_profile
            # Make the whole thermal loop latency-aware with the learned actuation lag.
            self.thermal.set_response_lag(lead_profile.response_lag_min)

    def _sleep_baseline(self, recent: list) -> tuple:
        """Recent settled-sleep HR/HRV baselines (from asleep, low-motion frames) so the
        arousal + wake-risk detectors measure surges/creep against the right reference. Before
        enough asleep frames exist (early night / night one), fall back to the measured RESTING
        baseline so the detectors are anchored to YOUR numbers from tick one instead of the
        population defaults."""
        asleep = [f for f in (recent or [])
                  if f.stage in (SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM)
                  and (f.movement is None or f.movement < 0.2)]
        pool = asleep[-15:] if asleep else (recent[-10:] if recent else [])
        hrs = [f.heart_rate for f in pool if f.heart_rate is not None]
        hrvs = [f.hrv for f in pool if f.hrv is not None]
        import statistics as _st
        hr = _st.fmean(hrs) if hrs else None
        hrv = _st.fmean(hrvs) if hrvs else None
        rb = self.resting_baseline
        if rb:
            # Asleep HR sits a touch below quiet-wakeful resting HR; nudge the fallback down a bit.
            if hr is None and rb.get("hr") is not None:
                hr = rb["hr"] - 2.0
            if hrv is None and rb.get("hrv") is not None:
                hrv = rb["hrv"]
        return hr, hrv

    @staticmethod
    def _biometric_reliability(frame: SensorFrame) -> float:
        """1.0 when still; lower when moving (ballistocardiography needs stillness)."""
        if frame.movement is None:
            return 1.0
        # movement ~0 -> 1.0; movement >= 0.5 -> ~0.6 floor. Linear in between.
        return max(0.6, 1.0 - 0.8 * min(frame.movement, 0.5))

    def _action_for(self, current_f: float, target_f: float) -> CorrectionAction:
        delta = target_f - current_f
        if abs(delta) < 0.5:
            return CorrectionAction.HOLD
        return CorrectionAction.WARMER if delta > 0 else CorrectionAction.COOLER

    def _reason(self, state, intent, wake_event) -> str:
        if wake_event is not None:
            return f"{state.value}: awakening ({','.join(wake_event.signals)}); stabilizing"
        base = f"{state.value} -> {intent.value}"
        if self.sm.reason:
            base += f" ({self.sm.reason})"
        return base

    def _build(
        self, now, state, objective, intent, target_f, level, action, reason,
        confidence, frame, wake_signals, minutes_in_bed: float = 0.0,
        ambient_temp_f=None, data_quality: Optional[DataQuality] = None,
        guardrail: Optional[GuardrailAssessment] = None,
    ) -> Decision:
        # Data-quality gate (Feature #6) + decision guardrail (Feature #8): surfaced on every
        # Decision (and, from there, in runtime_state) so the score/findings are visible even
        # when they didn't change the outcome this tick.
        dq = data_quality or self.last_data_quality
        gr = guardrail or self.last_guardrail
        log_payload = {
            "stage": frame.stage.value if frame.stage is not None else None,
            "stage_confidence": frame.stage_confidence,
            # Where the sleep stage steering the controller came from this tick:
            #   "sensor"    => a real Pod/device sleep stage
            #   "model"     => the learned wearable stager (HR -> stage; PhysioNet-trained)
            #   "heuristic" => the interpretable HR/HRV/movement fallback
            "stage_source": self._stage_source if self._stage_estimated else "sensor",
            # How many estimated-stage flips the hysteresis has absorbed so far this session --
            # the churn stays measurable instead of being silently smoothed away.
            "stage_flips_suppressed": self._stage_hold_suppressed,
            # Ultradian trajectory: in_light / minutes_to_next_light / typical_deep_bout_min /
            # confidence, accumulated across the whole night (see wake_orch.observe_stage).
            "cycle": self.last_cycle_state or None,
            "heart_rate": frame.heart_rate,
            "hrv": frame.hrv,
            "respiratory_rate": frame.respiratory_rate,
            "movement": frame.movement,
            "bed_temp_f": frame.bed_temp_f,
            "room_temp_f": frame.room_temp_f,
            "ambient_temp_f": ambient_temp_f,
            "composite_temp_f": self._round_opt(
                self.thermal.composite_temp(frame.bed_temp_f, ambient_temp_f)
            ),
            "effective_target_f": round(
                self.thermal.target_for(intent, objective, self.cfg.profile.hot_sleeper,
                                        self._last_target_f,
                                        getattr(self, "_settle_nudge_used", None)), 2
            ),
            "data_age_seconds": frame.data_age_seconds,
            "wake_signals": wake_signals,
            # Whether PRE-EMPTION was engaged on this tick, and what drove it. Without this the
            # only way to tell if awakening prevention actually fired was to re-run the night
            # through the controller offline and inspect `_preempt_cool` -- the `interventions`
            # ledger records a narrower class of correction, so a pre-emptive nudge that resolved
            # to a small or held command left no trace anywhere. "Did prevention run?" is the
            # single most important question this system has to answer about itself.
            "preemption": self.preemption_summary(),
            "steering": self.steering_summary(),
            "should_wake": self.should_wake,
            "wake_action": self.last_wake_action.to_dict() if self.last_wake_action else None,
            "minutes_in_bed": round(minutes_in_bed, 1),
            "data_quality": dq.to_dict() if dq is not None else None,
            "guardrail": gr.to_dict() if gr is not None else None,
            # Published every tick, not just when it fires: the interesting question is how
            # close the detector runs to its threshold on ordinary restless nights, and that is
            # unanswerable from events alone.
            "bed_exit": (self.last_bed_exit.to_dict()
                         if getattr(self, "last_bed_exit", None) is not None else None),
            "bed_entry_blocked": getattr(self, "last_bed_entry_block", None),
            "architecture_implausible": getattr(self, "last_architecture_implausible", None),
            "preempt_duty": {
                "duty": round(float(getattr(self, "last_preempt_duty", 0.0)), 3),
                "capped": bool(getattr(self, "last_preempt_duty_capped", False)),
                "maintenance_ticks": int(getattr(self, "_maint_ticks", 0)),
            },
            "hypnogram": (self.hypnogram.summary()
                          if getattr(self, "hypnogram", None) is not None else None),
        }
        return Decision(
            timestamp=now,
            state=state,
            objective=objective,
            thermal_intent=intent,
            target_temp_f=round(target_f, 2),
            target_level=level,
            action=action,
            reason=reason,
            confidence=round(confidence, 2),
            log_payload=log_payload,
        )
