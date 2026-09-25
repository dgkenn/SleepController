"""EEG-headband ground truth: import a scored hypnogram, score the stager against it, and fit
(and gate) a personal calibration -- all on synthetic hypnograms and a synthetic night."""
from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from sleepctl.eval import eeg_agreement as ea
from sleepctl.eval import hypnogram_import as hi
from sleepctl.learning import eeg_calibration as ec
from sleepctl.storage.repository import Repository

NIGHT = "2026-09-24"


@pytest.fixture()
def eastern(monkeypatch):
    """Run with the machine in US/Eastern so naive-local and UTC genuinely differ."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture()
def repo(tmp_path):
    r = Repository(str(tmp_path / "eeg.db"))
    r.conn.executescript(
        "CREATE TABLE IF NOT EXISTS sensor_samples (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, hr REAL, hrv REAL, movement REAL, source TEXT, hr_frozen INTEGER,"
        " not_worn INTEGER);")
    return r


# --------------------------------------------------------------------------- vocabulary
@pytest.mark.parametrize("tok,want", [
    ("W", "awake"), ("Wake", "awake"), ("0", "awake"), ("SLEEP-S0", "awake"),
    ("N1", "light"), ("N2", "light"), ("Sleep stage 2", "light"), ("Core", "light"),
    ("N3", "deep"), ("SWS", "deep"), ("Deep", "deep"), ("SLEEP-S3", "deep"),
    ("R", "rem"), ("REM", "rem"), ("Sleep stage R", "rem"), ("SLEEP-REM", "rem"),
    ("Sleep stage ?", "unknown"), ("Movement time", "unknown"), ("-1", "unknown"),
    ("Arousal", None), ("Sleep Stage", None),
])
def test_stage_vocabularies_map_to_controller_classes(tok, want):
    assert hi.map_stage(tok) == want


def test_numeric_code_four_depends_on_the_scheme():
    assert hi.map_stage("4", "aasm") == "rem"      # BIDSleep / Dreem numeric
    assert hi.map_stage("4", "rk") == "deep"       # R&K stage 4
    assert hi.map_stage("5", "rk") == "rem"        # sleep-accel reduction
    assert hi.map_stage("5", "aasm") == "unknown"  # BIDSleep "unscored"


# --------------------------------------------------------------------------- formats
def test_csv_with_offset_timestamps_is_exact_whatever_the_machine_zone(eastern):
    h = hi.parse_hypnogram("timestamp,stage\n2026-09-25T03:00:00Z,W\n"
                           "2026-09-25T03:00:30Z,N2\n2026-09-25T03:01:00Z,N3\n")
    assert [s for _, s, _ in h.epochs] == ["awake", "light", "deep"]
    assert h.epochs[0][0] == datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc).timestamp()
    # stored naive LOCAL, like raw_samples.ts: 03:00 UTC is 23:00 the evening before in EDT
    assert hi.local_naive(h.epochs[0][0]) == "2026-09-24T23:00:00"


def test_naive_timestamps_are_local_unless_a_zone_is_given(eastern):
    local = hi.parse_hypnogram("time,stage\n2026-09-24 23:00:00,W\n2026-09-24 23:00:30,N1\n")
    assert hi.local_naive(local.epochs[0][0]) == "2026-09-24T23:00:00"
    tokyo = hi.parse_hypnogram("time,stage\n2026-09-24 23:00:00,W\n", tz="Asia/Tokyo")
    assert tokyo.epochs[0][0] == datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc).timestamp()


def test_dreem_style_time_of_day_export_rolls_over_midnight(eastern):
    text = ("Sleep Stage\tTime [hh:mm:ss]\tEvent\tDuration[s]\n"
            "SLEEP-S0\t23:59:00\tSLEEP-S0\t30\n"
            "SLEEP-S1\t23:59:30\tSLEEP-S1\t30\n"
            "SLEEP-S2\t00:00:00\tSLEEP-S2\t60\n"
            "SLEEP-REM\t00:01:00\tSLEEP-REM\t30\n")
    h = hi.parse_hypnogram(text, night_date=NIGHT)
    assert [s for _, s, _ in h.epochs] == ["awake", "light", "light", "light", "rem"]
    assert hi.local_naive(h.epochs[2][0]) == "2026-09-25T00:00:00"   # the next calendar day
    assert h.epochs[-1][0] - h.epochs[0][0] == 120.0


def test_time_of_day_without_a_night_is_refused():
    with pytest.raises(ValueError, match="night date"):
        hi.parse_hypnogram("time,stage\n23:00:00,W\n23:00:30,N1\n")


def test_epoch_list_with_a_start_line(eastern):
    h = hi.parse_hypnogram("# start: 2026-09-24 23:00:00\nW\nW\nN2\nN3\nR\n")
    assert len(h.epochs) == 5 and h.epochs[1][0] - h.epochs[0][0] == 30.0
    assert hi.local_naive(h.epochs[0][0]) == "2026-09-24T23:00:00"
    assert [s for _, s, _ in h.epochs] == ["awake", "awake", "light", "deep", "rem"]


def test_epoch_list_needs_a_start():
    with pytest.raises(ValueError, match="start"):
        hi.parse_hypnogram("W\nN1\nN2\n")


def test_sleep_accel_relative_format_reads_five_as_rem(eastern):
    h = hi.parse_hypnogram("0 0\n30 1\n60 3\n90 5\n120 -1\n", start="2026-09-24T23:00:00")
    assert [s for _, s, _ in h.epochs] == ["awake", "light", "deep", "rem", "unknown"]
    assert h.scored == 4


def test_indexed_epochs_with_aasm_codes():
    h = hi.parse_hypnogram("epoch,stage\n1,0\n2,1\n3,4\n4,3\n", start="2026-09-24T23:00:00")
    assert [s for _, s, _ in h.epochs] == ["awake", "light", "rem", "deep"]


def test_json_epoch_list():
    doc = {"start": "2026-09-25T03:00:00Z", "epoch_s": 30, "device": "band-x",
           "stages": ["wake", "light", "deep", "rem"]}
    h = hi.parse_hypnogram(json.dumps(doc))
    assert h.fmt == "json" and h.device == "band-x" and h.scored == 4


def test_intervals_expand_and_an_in_bed_overlay_never_blanks_the_stages():
    text = ("Start Time,End Time,Stage\n"
            "2026-09-24 23:00:00,2026-09-24 23:10:00,Core\n"
            "2026-09-24 23:10:00,2026-09-24 23:20:00,Deep\n"
            "2026-09-24 22:50:00,2026-09-24 23:30:00,InBed\n")
    h = hi.parse_hypnogram(text)
    st = [s for _, s, _ in h.epochs]
    assert st.count("light") == 20 and st.count("deep") == 20
    assert len(h.epochs) == 80


def _edf_plus(start: datetime, annotations) -> bytes:
    """A minimal annotation-only EDF+ file: one record holding every TAL."""
    tals = b"+0\x14\x14\x00"
    for onset, dur, text in annotations:
        tals += f"+{onset}\x15{dur}\x14{text}\x14\x00".encode()
    n_samp = (len(tals) + 1) // 2 + 4
    tals = tals.ljust(n_samp * 2, b"\x00")

    def f(v, n):
        return str(v).ljust(n)[:n].encode("ascii")
    hdr = (f("0", 8) + f("X X X X", 80) + f("Startdate X X X X", 80)
           + f(start.strftime("%d.%m.%y"), 8) + f(start.strftime("%H.%M.%S"), 8)
           + f(512, 8) + f("EDF+C", 44) + f(1, 8) + f(0, 8) + f(1, 4))
    sig = (f("EDF Annotations", 16) + f("", 80) + f("", 8) + f(-1, 8) + f(1, 8)
           + f(-32768, 8) + f(32767, 8) + f("", 80) + f(n_samp, 8) + f("", 32))
    return hdr + sig + tals


def test_edf_plus_stage_annotations(eastern):
    data = _edf_plus(datetime(2026, 9, 24, 23, 0, 0),
                     [(0, 60, "Sleep stage W"), (60, 90, "Sleep stage 2"),
                      (150, 30, "Arousal"), (150, 60, "Sleep stage 4"),
                      (210, 30, "Sleep stage R")])
    h = hi.parse_hypnogram(data, filename="night.edf")
    assert h.fmt == "edf+"
    assert [s for _, s, _ in h.epochs] == ["awake", "awake", "light", "light", "light",
                                           "deep", "deep", "rem"]   # R&K: stage 4 is deep
    assert hi.local_naive(h.epochs[0][0]) == "2026-09-24T23:00:00"
    assert any("non-stage" in w for w in h.warnings)


def test_bidsleep_labels_mat_reuses_the_reduction_loader(tmp_path, eastern):
    sio = pytest.importorskip("scipy.io")
    np = pytest.importorskip("numpy")
    path = tmp_path / "labels.mat"
    sio.savemat(str(path), {"recStart": "2026-09-24 23:00:00",
                            "expert_label": np.array([], dtype=int),
                            "dreem_label": np.array([0, 1, 2, 3, 4, 5])})
    h = hi.parse_hypnogram(path.read_bytes(), filename="labels.mat")
    assert [s for _, s, _ in h.epochs] == ["awake", "light", "light", "deep", "rem", "unknown"]
    assert h.device == "dreem" and hi.local_naive(h.epochs[0][0]) == "2026-09-24T23:00:00"


def test_a_file_without_stages_is_refused_readably():
    with pytest.raises(ValueError):
        hi.parse_hypnogram("a,b\nfoo,bar\nbaz,qux\n")


# --------------------------------------------------------------------------- storage
def _raw_rows(repo, night, start_local: datetime, n: int, stage_fn=lambda i: "light",
              state="maintenance", step_s=30):
    rows = []
    for i in range(n):
        t = start_local + timedelta(seconds=i * step_s)
        rows.append((t.replace(microsecond=0).isoformat(), night, stage_fn(i), 58.0,
                     state if i else "settling", t.isoformat()))
    repo.conn.executemany(
        "INSERT INTO raw_samples (ts, night_date, stage, heart_rate, controller_state,"
        " sample_ts) VALUES (?,?,?,?,?,?)", rows)
    repo.conn.commit()


def test_import_infers_the_controller_night_and_replaces_on_reimport(repo, eastern):
    # A day sleeper: 04:00-05:00 local sits under the PREVIOUS date by the noon cutoff, but
    # the controller filed this session under NIGHT.
    _raw_rows(repo, NIGHT, datetime(2026, 9, 25, 4, 0), 120)
    csv_text = "time,stage\n" + "".join(
        f"2026-09-25 04:{m:02d}:{s:02d},N2\n" for m in range(0, 30) for s in (0, 30))
    out = hi.import_hypnogram(repo.conn, csv_text, source="headband")
    assert out["night_date"] == NIGHT and out["scored_epochs"] == 60
    row = repo.conn.execute("SELECT epoch_ts, epoch_unix, source FROM eeg_hypnogram "
                            "ORDER BY epoch_unix LIMIT 1").fetchone()
    assert row[0] == "2026-09-25T04:00:00" and row[2] == "headband"
    assert row[1] == datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc).timestamp()
    hi.import_hypnogram(repo.conn, "time,stage\n2026-09-25 04:00:00,W\n", night_date=NIGHT)
    assert len(hi.load_epochs(repo.conn, NIGHT)) == 1
    assert hi.imported_nights(repo.conn)[0]["night_date"] == NIGHT


# --------------------------------------------------------------------------- agreement
def test_kappa_and_confusion():
    t = ["awake", "light", "deep", "rem"] * 5
    assert ea.cohen_kappa(t, t) == pytest.approx(1.0)
    cm = ea.confusion(t, ["light"] * 20)
    assert cm[1][1] == 5 and sum(r[1] for r in cm) == 20
    assert ea.cohen_kappa(t, ["light"] * 20) == pytest.approx(0.0)


def test_agreement_report_aligns_local_ticks_with_utc_epochs(repo, eastern):
    start = datetime(2026, 9, 24, 23, 0)
    truth = (["awake"] * 10 + ["light"] * 40 + ["deep"] * 30 + ["awake"] * 6
             + ["rem"] * 30 + ["light"] * 20)
    # The stager is right except that it notices the mid-night awakening 2 epochs late and
    # calls one REM stretch light.
    pred = list(truth)
    pred[80] = pred[81] = "deep"
    for i in range(90, 100):
        pred[i] = "light"
    # Controller ticks: naive LOCAL, every 30 s, landing 10 s into each epoch.
    _raw_rows(repo, NIGHT, start + timedelta(seconds=10), len(pred), lambda i: pred[i])
    # EEG export: aware UTC -- 23:00 EDT is 03:00Z.
    t0 = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)
    csv_text = "time,stage\n" + "".join(
        f"{(t0 + timedelta(seconds=30 * i)).isoformat()},{s}\n" for i, s in enumerate(truth))
    hi.import_hypnogram(repo.conn, csv_text, night_date=NIGHT, source="headband")

    rep = ea.agreement_report(repo, NIGHT, include_restage=True, restaged={})
    assert rep["available"] and rep["source"] == "headband"
    rec = rep["streams"]["recorded"]
    assert rec["n_epochs"] == len(truth) and rec["coverage"] == 1.0
    assert rec["accuracy"] == pytest.approx((len(truth) - 12) / len(truth), abs=1e-3)
    assert 0.7 < rec["kappa"] < 1.0
    idx = {c: i for i, c in enumerate(ea.CLASSES)}
    assert rec["confusion"][idx["rem"]][idx["light"]] == 10
    assert rec["confusion"][idx["awake"]][idx["deep"]] == 2
    assert rec["minutes"]["eeg"]["rem"] == 15.0 and rec["minutes"]["stager"]["rem"] == 10.0
    w = rec["wake"]
    assert w["eeg_awakenings"] == 1 and w["detected"] == 1 and w["missed"] == 0
    assert w["latency_median_min"] == 1.0               # two epochs late
    assert w["false_wake_min"] == 0.0
    assert rep["streams"]["restaged"]["n_epochs"] == 0  # nothing to replay here


def test_a_missed_awakening_and_a_false_alarm_are_both_reported(repo):
    start = datetime(2026, 9, 24, 23, 0)
    truth = ["light"] * 20 + ["awake"] * 4 + ["light"] * 20
    pred = ["light"] * 44
    pred[5] = pred[6] = "awake"
    _raw_rows(repo, NIGHT, start + timedelta(seconds=5), len(pred), lambda i: pred[i])
    hi.import_hypnogram(repo.conn, "\n".join(truth), night_date=NIGHT,
                        start=start.isoformat())
    w = ea.agreement_report(repo, NIGHT, include_restage=False)["streams"]["recorded"]["wake"]
    assert w["eeg_awakenings"] == 1 and w["missed"] == 1 and w["detected"] == 0
    assert w["false_wake_min"] == 1.0


def test_report_for_a_night_without_an_import(repo):
    assert ea.agreement_report(repo, NIGHT)["available"] is False


# --------------------------------------------------------------------------- live smoothing parity
def test_windowed_posteriors_match_the_stagers_forward_filter():
    from sleepctl.ml.sleep_staging.infer import forward_filter
    pop = ec.population_hmm()
    rng = random.Random(3)
    em = []
    for _ in range(40):
        v = [rng.random() for _ in range(4)]
        em.append([x / sum(v) for x in v])
    w = int(pop["smoothing_epochs"])
    got = ec.windowed_posteriors(em, pop["trans"], pop["start"], pop["emission_prior"],
                                 pop["temper"], w)
    for i in (0, 5, 19, 39):
        want = forward_filter(em[max(0, i - w + 1):i + 1], pop["trans"], pop["start"],
                              pop["emission_prior"], pop["temper"])
        assert got[i] == pytest.approx(tuple(want), rel=1e-12)


def test_decide_mirrors_the_shipped_rule_and_the_threshold_variant():
    assert ec.decide((0.45, 0.2, 0.2, 0.15)) == "awake"       # argmax wins below 0.5
    assert ec.decide((0.55, 0.45, 0.0, 0.0)) == "awake"
    assert ec.decide((0.45, 0.2, 0.2, 0.15), 0.5) == "light"  # threshold: wake only at >= 0.5
    assert ec.decide((0.35, 0.4, 0.2, 0.05), 0.3) == "awake"


# --------------------------------------------------------------------------- calibration
POP = None


def _pop():
    global POP
    if POP is None:
        POP = ec.population_hmm()
    return POP


def _synthetic_truth(seed, n):
    rng = random.Random(seed)
    tr = _pop()["trans"]
    s, out = 0, []
    for _ in range(n):
        out.append(ec.CLASSES[s])
        r, acc = rng.random(), 0.0
        for j, p in enumerate(tr[s]):
            acc += p
            if r < acc:
                s = j
                break
    return out


def _emissions(truth, seed, biased=True, strength=0.5, noise=0.6):
    """A stager that sees the truth through noise; ``biased`` makes it under-call deep and
    wake the way a population model can for one person."""
    rng = random.Random(seed + 100)
    em = []
    for t in truth:
        v = [0.25 + rng.random() * noise for _ in range(4)]
        v[ec.CLASSES.index(t)] += strength
        if biased:
            v[0] *= 0.6
            v[2] *= 0.3
        em.append([x / sum(v) for x in v])
    return em


def _seed_nights(repo, k, n=300, **kw):
    """k imported nights plus an emission_fn standing in for the replay."""
    table = {}
    for i in range(k):
        night = f"2026-09-{10 + i:02d}"
        truth = _synthetic_truth(i, n)
        start = datetime(2026, 9, 10 + i, 23, 0)
        hi.import_hypnogram(repo.conn, "\n".join(truth), night_date=night,
                            start=start.isoformat(), source="headband")
        table[night] = _emissions(truth, i, **kw)
    return lambda _repo, night, epochs: table[night]


def test_calibration_is_enabled_only_when_it_beats_the_model_on_held_out_nights(repo):
    fn = _seed_nights(repo, 3)
    prof = ec.calibrate(repo, emission_fn=fn, pop=_pop())
    val = prof["validation"]
    assert prof["enabled"] is True, prof["rationale"]
    assert val["kappa_calibrated"] >= val["kappa_unadjusted"] + ec.MIN_KAPPA_GAIN
    assert val["wins"] >= 2 and len(val["per_night"]) == 3
    # it learned the direction of the bias: deep and wake up-weighted relative to light
    assert prof["emission_bias"]["deep"] > 0 and prof["emission_bias"]["light"] == 0.0
    assert abs(math.log(prof["temperature"])) <= ec.T_BOUND + 1e-9
    stored = ec.load_calibration(repo.conn)
    assert stored["enabled"] and stored["hmm"]["trans"] == prof["hmm"]["trans"]


def test_calibration_stays_off_when_it_cannot_help(repo):
    # A stager with nothing to say about this sleeper: no recalibration can conjure signal.
    fn = _seed_nights(repo, 3, biased=False, strength=0.0, noise=0.0)
    prof = ec.build_calibration(repo, emission_fn=fn, pop=_pop())
    assert prof["enabled"] is False and prof["wake_threshold_enabled"] is False
    assert "unadjusted stager stays" in prof["rationale"]


@pytest.mark.parametrize("override", [
    {"wins": 1},            # a pooled gain carried by one night
    {"p_better": 0.6},      # a gain the bootstrap does not believe
])
def test_every_gate_must_pass(repo, monkeypatch, override):
    fn = _seed_nights(repo, 3)
    real = ec.validate

    def patched(nights, pop):
        v = real(nights, pop)
        v.update(override)
        return v
    monkeypatch.setattr(ec, "validate", patched)
    assert ec.build_calibration(repo, emission_fn=fn, pop=_pop())["enabled"] is False


def test_too_few_nights_is_learning_not_a_fit(repo):
    fn = _seed_nights(repo, 2)
    prof = ec.build_calibration(repo, emission_fn=fn, pop=_pop())
    assert prof["enabled"] is False and prof["n_nights"] == 2
    assert prof["rationale"].startswith("learning")


def test_eeg_transitions_follow_the_headband_and_keep_deep_and_rem_reachable():
    pop = _pop()
    truth = ["light"] * 600 + ["deep"] * 300
    night = ec.NightData("x", truth, [None] * 900, [30.0 * i for i in range(900)])
    tr = ec.eeg_transitions([night], pop)
    assert tr[1][1] > pop["trans"][1][1]                    # far stickier light than population
    assert tr[1][3] > 0.45 * pop["trans"][1][3]             # REM entry never closed off
    assert all(abs(sum(r) - 1.0) < 1e-9 for r in tr)


class _FakeStager:
    def __init__(self, hrv=False):
        self.hmm = dict(_pop())
        self.hrv_available = hrv
        self.wake_bias = 1.4
        self.threshold = None

    def set_wake_bias(self, b):
        self.wake_bias = b

    def set_wake_threshold(self, t):
        self.threshold = t


def _profile(**over):
    pop = _pop()
    prof = {"enabled": True, "wake_threshold_enabled": True, "wake_threshold": 0.4,
            "hmm": {"trans": pop["trans"], "emission_prior": [0.3, 0.25, 0.2, 0.25],
                    "temper": 0.5}}
    prof.update(over)
    return prof


def test_apply_installs_an_enabled_calibration():
    st = _FakeStager()
    msg = ec.apply_calibration(st, _profile())
    assert st.hmm["emission_prior"] == [0.3, 0.25, 0.2, 0.25] and st.hmm["temper"] == 0.5
    assert st.wake_bias == 1.0 and st.threshold == 0.4
    assert "transitions" in msg and "wake threshold" in msg
    assert st.hmm["smoothing_epochs"] == _pop()["smoothing_epochs"]   # the rest untouched


def test_apply_withholds_the_emission_fit_from_an_hrv_stager():
    st = _FakeStager(hrv=True)
    msg = ec.apply_calibration(st, _profile())
    assert st.hmm["emission_prior"] == _pop()["emission_prior"] and st.wake_bias == 1.4
    assert msg.startswith("transitions")


@pytest.mark.parametrize("prof", [
    None, {"enabled": False},
    _profile(hmm={"trans": [[1.0, 0, 0]], "emission_prior": [0.25] * 4, "temper": 0.35}),
    _profile(hmm={"trans": _profile()["hmm"]["trans"], "emission_prior": [0, 1, 1, 1],
                  "temper": 0.35}),
])
def test_apply_ignores_disabled_or_malformed_profiles(prof):
    st = _FakeStager()
    before = dict(st.hmm)
    assert ec.apply_calibration(st, prof) is None
    assert st.hmm == before


def test_apply_on_a_real_stager_changes_only_its_smoothing_model():
    from sleepctl.ml.sleep_staging.infer import SleepStager
    st = SleepStager.load()
    ec.apply_calibration(st, _profile(wake_threshold_enabled=False))
    assert st.hmm["temper"] == 0.5 and st.wake_bias == 1.0


# --------------------------------------------------------------------------- replay
def test_replay_scores_every_epoch_with_heart_rate_and_none_without(repo, eastern):
    """The real stager over a synthetic night: aware-UTC HR samples, naive-local ticks."""
    start = datetime(2026, 9, 24, 23, 0)
    _raw_rows(repo, NIGHT, start - timedelta(minutes=30), 200)
    t_utc = start.astimezone(timezone.utc) - timedelta(minutes=45)
    rows = []
    rng = random.Random(1)
    for i in range(0, 95 * 60, 5):           # HR every 5 s from 45 min before to ~50 min in
        t = t_utc + timedelta(seconds=i)
        rows.append((t.isoformat(), 58 + 4 * math.sin(i / 300.0) + rng.random(), 0, 0))
    repo.conn.executemany("INSERT INTO sensor_samples (ts, hr, hr_frozen, not_worn) "
                          "VALUES (?,?,?,?)", rows)
    repo.conn.commit()
    hi.import_hypnogram(repo.conn, "\n".join(["N2"] * 120), night_date=NIGHT,
                        start=start.isoformat())
    epochs = hi.load_epochs(repo.conn, NIGHT)
    em = ec.replay_emissions(repo, NIGHT, epochs)
    assert len(em) == 120
    scored = [e for e in em if e is not None]
    assert len(scored) >= 95                 # HR runs out ~50 min in
    assert em[-1] is None
    assert all(abs(sum(e) - 1.0) < 1e-6 and len(e) == 4 for e in scored)
