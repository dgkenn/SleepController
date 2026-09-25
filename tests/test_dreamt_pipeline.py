"""DREAMT reduce -> dataset -> train -> infer, end to end on a synthetic night.

The real corpus needs PhysioNet credentials and never enters the repo, so the pipeline is
exercised on a generated file with the documented layout: 64 Hz rows, sparse IBI, 1 Hz HR,
ACC in 1/64 g repeated at 32 Hz, and a Sleep_Stage label repeated per 30 s epoch.
"""
import csv
import math
import os
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dreamt_reduce  # noqa: E402

FS = 64


def write_synthetic_night(path, minutes=40, seed=0, stages=None):
    """A night whose stages have distinct physiology so a tiny model can learn something."""
    random.seed(seed)
    stages = stages or ["P", "W", "N1", "N2", "N2", "N3", "N3", "N2", "R", "R", "W", "Missing"]
    n_epochs = minutes * 2
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["TIMESTAMP", "BVP", "ACC_X", "ACC_Y", "ACC_Z", "TEMP", "EDA", "HR", "IBI",
                    "Sleep_Stage", "Obstructive_Apnea"])
        t = 0.0
        next_beat = 0.5
        acc = (0.0, 0.0, 64.0)
        for e in range(n_epochs):
            st = stages[(e * len(stages)) // n_epochs]
            hr0 = {"P": 75, "W": 72, "N1": 64, "N2": 60, "N3": 55, "R": 66, "Missing": 62}[st]
            var = {"P": 40, "W": 40, "N1": 50, "N2": 55, "N3": 80, "R": 45, "Missing": 50}[st]
            for i in range(30 * FS):
                if i % 2 == 0:                       # 32 Hz accelerometer, repeated at 64 Hz
                    jitter = 6.0 if st in ("W", "P") else 0.3
                    acc = (random.gauss(0, jitter), random.gauss(0, jitter), 64.0 + random.gauss(0, jitter))
                ibi = ""
                if t >= next_beat:
                    ibi_ms = 60000.0 / hr0 + random.gauss(0, var / 3.0)
                    ibi = f"{ibi_ms:.1f}"
                    next_beat = t + ibi_ms / 1000.0
                hr = f"{hr0 + random.gauss(0, 1.5):.1f}" if i % FS == 0 else f"{hr0:.1f}"
                w.writerow([f"{t:.6f}", f"{random.gauss(0, 10):.2f}", f"{acc[0]:.2f}", f"{acc[1]:.2f}",
                            f"{acc[2]:.2f}", "33.1", "0.2", hr, ibi, st, "0"])
                t += 1.0 / FS
    return path


@pytest.fixture(scope="module")
def reduced(tmp_path_factory):
    raw = tmp_path_factory.mktemp("dreamt_raw")
    (raw / "data_64Hz").mkdir()
    for k, sid in enumerate(("S002", "S003", "S004")):
        write_synthetic_night(raw / "data_64Hz" / f"{sid}_whole_df.csv", minutes=36, seed=k)
    out = tmp_path_factory.mktemp("dreamt_reduced")
    assert dreamt_reduce.main(["--data-dir", str(raw), "--out", str(out)]) == 0
    return out


def test_reducer_writes_every_input_the_dataset_loader_parses(reduced):
    from sleepctl.ml.sleep_staging.dataset import (_parse_labels, _parse_pairs, discover_subjects,
                                                    parse_activity, subjects_with_ibi)
    assert discover_subjects(str(reduced)) == ["S002", "S003", "S004"]
    assert subjects_with_ibi(str(reduced)) == ["S002", "S003", "S004"]
    labels = _parse_labels(str(reduced / "S002_labeled_sleep.txt"))
    assert len(labels) == 72
    codes = {c for _t, c in labels}
    assert codes == {0, 1, 2, 3, 5, -1}                    # P->wake, Missing->unscored
    assert labels[0] == (0.0, 0) and labels[1][0] == 30.0
    hr = _parse_pairs(str(reduced / "S002_heartrate.txt"))
    assert 2100 <= len(hr) <= 2160 and all(50 <= v <= 80 for _t, v in hr)
    ibi = _parse_pairs(str(reduced / "S002_ibi.txt"))
    assert len(ibi) > 1800 and all(300 <= v <= 2000 for _t, v in ibi)
    act = parse_activity(str(reduced / "activity" / "S002_activity.txt"))
    assert len(act) == 72
    # 32 Hz after de-duplicating the 64 Hz repeats -> 960 samples per 30 s epoch
    with open(reduced / "activity" / "S002_activity.txt") as fh:
        n = [int(float(l.split(",")[6])) for l in fh if l and not l.startswith("#")]
    assert 940 <= n[1] <= 960


def test_reducer_verify_mode_and_missing_columns(reduced, tmp_path):
    assert dreamt_reduce.main(["--out", str(reduced), "--verify"]) == 0
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError):
        dreamt_reduce.reduce_file(str(bad), str(tmp_path / "o"), verbose=False)


def test_rows_carry_the_hrv_block_and_a_tiny_model_trains_and_infers(reduced, tmp_path):
    from sleepctl.ml.sleep_staging.dataset import build_dataset
    from sleepctl.ml.sleep_staging.features import FEATURE_NAMES_HRV, FEATURE_NAMES_HRV_MOTION
    from sleepctl.ml.sleep_staging.infer import SleepStager, validate_forest_dict
    import train_dreamt

    ds = build_dataset(str(reduced), ["S002"], use_activity=True, use_ibi=True, require_ibi=True)
    assert len(ds) > 40 and sum(ds.has_ibi) > 30
    row = dict(zip(FEATURE_NAMES_HRV_MOTION, ds.matrix(FEATURE_NAMES_HRV_MOTION)[-1]))
    assert row["hrv_present_w5"] == 1.0 and row["hrv_n_w5"] > 200
    assert any(row[n] != 0.0 for n in FEATURE_NAMES_HRV if n.startswith("hrvn_"))

    out = tmp_path / "weights"
    slim = train_dreamt.train(str(reduced), str(out), folds=3, quick=True, jobs=1)
    for name in ("wake_hrv.json", "stage4_hrv.json", "wake_hrvonly.json", "stage4_hrvonly.json",
                 "hmm_dreamt.json"):
        assert (out / name).exists(), name
    import json
    assert validate_forest_dict(json.loads((out / "stage4_hrv.json").read_text()))
    assert slim["hrv"]["raw"]["kappa4"] > -1.0

    # the runtime picks the HRV variant when beats are streaming, HR otherwise: bundle the
    # shipped HR models beside the new ones so the fallback path exists too
    import shutil
    from sleepctl.ml.sleep_staging.infer import WEIGHTS_DIR
    for name in ("wake_hr.json", "stage4_hr.json", "hmm.json"):
        shutil.copy(os.path.join(WEIGHTS_DIR, name), out / name)
    st = SleepStager.load(str(out))
    assert st.hrv_available
    from sleepctl.ml.sleep_staging.dataset import _parse_pairs, parse_activity
    hr = _parse_pairs(str(reduced / "S003_heartrate.txt"))
    ibi = _parse_pairs(str(reduced / "S003_ibi.txt"))
    act = [(a[0], a[1]) for a in parse_activity(str(reduced / "activity" / "S003_activity.txt"))]
    est = st.predict(hr, act, ibi_samples=ibi)
    assert est is not None and est.variant == "hrv" and est.stage_label in ("wake", "light", "deep", "rem")
    est2 = st.predict(hr, None, ibi_samples=ibi)
    assert est2 is not None and est2.variant == "hrvonly"
    est3 = st.predict(hr, act, ibi_samples=ibi[:50])       # a stale trickle of beats: HR path
    assert est3 is not None and est3.variant == "hr"


def test_a_still_wrist_still_gets_an_activity_line(tmp_path):
    """Regression (audit 2026-09-25): the 32 Hz repeats were dropped by VALUE, so a motionless
    wrist -- the same quantised 1/64 g reading for minutes -- collapsed to one sample per epoch
    and got no activity line at all. The stillest epochs (deep sleep) vanished instead of
    reading pim=0."""
    p = tmp_path / "S009_whole_df.csv"
    random.seed(0)
    with open(p, "w", newline="") as fh:
        fh.write("TIMESTAMP,ACC_X,ACC_Y,ACC_Z,HR,IBI,Sleep_Stage\n")
        acc = (10, -20, 60)
        for i in range(FS * 90):                       # 90 s at 64 Hz
            t = i / FS
            if t >= 60 and i % 2 == 0:                  # epoch 2 moves (32 Hz, repeated)
                acc = (10 + random.randint(-3, 3), -20, 60)
            fh.write(f"{t:.6f},{acc[0]},{acc[1]},{acc[2]},60,,N3\n")
    out = tmp_path / "out"
    dreamt_reduce.reduce_file(str(p), str(out), verbose=False)
    rows = [l.strip().split(",") for l in open(out / "activity" / "S009_activity.txt")
            if l.strip() and not l.startswith("#")]
    assert [r[0] for r in rows] == ["0", "30", "60"]    # every epoch, still ones included
    for r in rows[:2]:
        assert float(r[1]) == 0.0 and int(r[6]) == 960  # pim 0 over the full 32 Hz epoch
    assert float(rows[2][1]) > 0.0 and int(rows[2][6]) == 960
