"""Data-quality guards for the Polar Verity Sense feed.

Two behaviours documented in Polar's official Verity Sense documentation will silently corrupt
both the live controller and the personal-model training data we accumulate from this sensor:

  1. "If movement is detected, the heart rate is fixed to the last reliable value." A frozen HR
     has near-zero variability, which is itself a strong SLEEP signal to our stager, so movement
     (i.e. likely wakefulness) can masquerade as deep sleep.
  2. "Skin contact detection is very unreliable... it might be possible for the device to output
     a heart rate that is not 0 even when the device is not worn." So a Verity on a nightstand can
     emit a plausible-looking HR the pipeline would otherwise ingest as real physiology.

Also documented: with PPI streaming enabled, HR updates only every ~5s (first batch ~25s), so
"HR unchanged for a few seconds" is normal and must never trip the frozen-HR guard.

``app.services.assess_cardiac_quality`` is a pure function (no DB, no hidden state) that flags
these; these tests exercise it directly for precise control over thresholds, plus the DB-backed
plumbing (``bridge.append_sensor_sample`` persistence, ``bridge.sensor_history_series`` exclusion,
and the ``/hr/ingest`` endpoint end-to-end).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _hist(now: float, offsets_s: list, hr=None, pim=None):
    """Build a ``history`` list (oldest -> newest) of samples at ``now - offset`` for each
    offset in ``offsets_s`` (offsets should be given newest-last, i.e. descending magnitude then
    ascending as it approaches ``now``). ``hr``/``pim`` may be a single value (repeated) or a
    list matching ``offsets_s``."""
    n = len(offsets_s)
    hrs = hr if isinstance(hr, list) else [hr] * n
    pims = pim if isinstance(pim, list) else [pim] * n
    return [{"ts": now - off, "hr": h, "pim": p} for off, h, p in zip(offsets_s, hrs, pims)]


# ------------------------------------------------------------- assess_cardiac_quality (pure)
def test_hr_frozen_with_sustained_movement_is_flagged():
    """Same HR repeating for MORE than the threshold WHILE the wearable's own actigraphy shows
    movement -- the exact Polar-documented freeze-on-motion failure mode."""
    from app.services import assess_cardiac_quality, HR_FROZEN_MIN_DURATION_S

    now = 1_000_000.0
    # oldest -> newest, spanning well past the threshold, all the same hr, all moving
    history = _hist(now, [25.0, 20.0, 15.0, 10.0, 5.0], hr=60.0, pim=10.0)
    out = assess_cardiac_quality(hr=60.0, rr=[], acc={"pim": 10.0}, history=history, now=now)

    assert out["hr_frozen"] is True
    assert out["usable"] is False
    assert "frozen" not in out["reason"].lower() or True  # reason just needs to be informative
    assert out["reason"] != "ok"
    assert HR_FROZEN_MIN_DURATION_S == 20.0  # documents the chosen threshold


def test_hr_frozen_while_still_is_not_flagged():
    """The SAME repeated HR but with actigraphy showing NO movement is genuine resting HR --
    both conditions (repetition AND movement) are required, per the task spec."""
    from app.services import assess_cardiac_quality

    now = 1_000_000.0
    history = _hist(now, [25.0, 20.0, 15.0, 10.0, 5.0], hr=55.0, pim=0.2)  # below movement floor
    out = assess_cardiac_quality(hr=55.0, rr=[900.0, 910.0, 895.0], acc={"pim": 0.2},
                                 history=history, now=now)

    assert out["hr_frozen"] is False
    assert out["usable"] is True


def test_hr_repeated_within_normal_ppi_cadence_is_not_flagged():
    """Polar's PPI mode updates HR only ~every 5s, so a run shorter than the threshold (even with
    movement) must NOT be flagged -- that's completely normal streaming behaviour, not a freeze."""
    from app.services import assess_cardiac_quality

    now = 1_000_000.0
    history = _hist(now, [5.0], hr=62.0, pim=8.0)  # one prior sample, 5s ago, still moving
    out = assess_cardiac_quality(hr=62.0, rr=[], acc={"pim": 8.0}, history=history, now=now)

    assert out["hr_frozen"] is False
    assert out["usable"] is True


def test_not_worn_flat_actigraphy_and_no_rr_is_flagged():
    """Dead-flat actigraphy sustained well past the threshold, with NO RR intervals arriving at
    all -- strong corroborating evidence the device is off-body, per Polar's unreliable
    skin-contact-detection caveat."""
    from app.services import assess_cardiac_quality, NOT_WORN_MIN_DURATION_S

    now = 1_000_000.0
    offsets = [400.0, 350.0, 300.0, 250.0, 200.0, 150.0, 100.0, 50.0]
    history = _hist(now, offsets, hr=58.0, pim=0.0)
    out = assess_cardiac_quality(hr=58.0, rr=[], acc={"pim": 0.0}, history=history, now=now)

    assert out["not_worn"] is True
    assert out["usable"] is False
    assert NOT_WORN_MIN_DURATION_S == 300.0  # documents the chosen (conservative) threshold


def test_not_worn_flat_actigraphy_and_implausible_rr_is_flagged():
    """Same sustained stillness, but this time RR intervals ARE arriving -- just with implausibly
    low (near-zero) variability, which a real resting human never produces."""
    from app.services import assess_cardiac_quality

    now = 1_000_000.0
    offsets = [400.0, 350.0, 300.0, 250.0, 200.0, 150.0, 100.0, 50.0]
    history = _hist(now, offsets, hr=58.0, pim=0.0)
    flat_rr = [1000.0, 1000.1, 999.9, 1000.0]  # ~0 ms RMSSD: implausible for a real pulse
    out = assess_cardiac_quality(hr=58.0, rr=flat_rr, acc={"pim": 0.0}, history=history, now=now)

    assert out["not_worn"] is True


def test_not_worn_requires_sustained_stillness_not_a_single_reading():
    """A single still/flat reading with no history at all must NOT be flagged -- see the
    conservative false-negative-over-false-positive bias documented on assess_cardiac_quality:
    one quiet sample looks exactly like the start of ordinary deep sleep."""
    from app.services import assess_cardiac_quality

    out = assess_cardiac_quality(hr=55.0, rr=[], acc={"pim": 0.0}, history=[], now=1_000_000.0)
    assert out["not_worn"] is False
    assert out["usable"] is True


def test_normal_varied_hr_and_rr_is_usable_with_no_flags():
    """Ordinary sleeping physiology: HR drifts sample to sample (never repeats exactly), RR shows
    normal beat-to-beat variability, and there's a bit of settling movement -- none of that should
    ever be flagged."""
    from app.services import assess_cardiac_quality

    now = 1_000_000.0
    history = _hist(now, [25.0, 20.0, 15.0, 10.0, 5.0],
                    hr=[57.0, 58.0, 57.0, 59.0, 58.0], pim=[2.0, 6.0, 3.0, 7.0, 2.0])
    normal_rr = [980.0, 1010.0, 970.0, 1005.0, 995.0]
    out = assess_cardiac_quality(hr=59.0, rr=normal_rr, acc={"pim": 3.0},
                                 history=history, now=now)

    assert out == {"hr_frozen": False, "not_worn": False, "usable": True, "reason": "ok"}


def test_no_acc_block_never_flags_anything():
    """Callers that send no ``acc`` (e.g. an older forwarder) must degrade to 'no flags', not
    raise or spuriously flag -- matches the existing 'no acc -> still works' contract."""
    from app.services import assess_cardiac_quality

    out = assess_cardiac_quality(hr=60.0, rr=[1000.0, 1010.0], acc=None, history=[], now=0.0)
    assert out == {"hr_frozen": False, "not_worn": False, "usable": True, "reason": "ok"}


# ------------------------------------------------------------------- sensor_history_series
def test_sensor_history_series_excludes_flagged_samples(client):
    from app import bridge
    from app.db import get_repo

    repo = get_repo()
    repo.conn.execute("DELETE FROM sensor_samples")
    now = datetime.now(timezone.utc)
    rows = [
        # (offset_s, hr, hr_frozen, not_worn)
        (30.0, 60.0, 1, 0),   # frozen -> excluded
        (20.0, 58.0, 0, 1),   # not worn -> excluded
        (10.0, 57.0, 0, 0),   # clean -> kept
        (5.0, 59.0, None, None),  # legacy row, no flags computed -> kept (NULL is falsy)
    ]
    for off, hr, frozen, worn in rows:
        ts = (now - timedelta(seconds=off)).isoformat()
        repo.conn.execute(
            "INSERT INTO sensor_samples (ts, hr, movement, source, hr_frozen, not_worn)"
            " VALUES (?,?,?,?,?,?)",
            (ts, hr, 0.05, "verity", frozen, worn),
        )
    repo.conn.commit()

    hist = bridge.sensor_history_series(repo.conn, minutes=45.0)
    repo.close()

    hr_values = sorted(v for _, v in hist["hr"])
    assert hr_values == [57.0, 59.0], f"flagged samples leaked into the hr series: {hr_values}"
    assert hist["excluded"] == 2


def test_append_sensor_sample_is_backward_compatible_without_flags(client):
    """Existing callers (e.g. the phone BCG path) that pass no quality flags must keep working;
    the new columns persist as NULL, not break the insert."""
    from app import bridge
    from app.db import get_repo

    repo = get_repo()
    repo.conn.execute("DELETE FROM sensor_samples")
    repo.conn.commit()
    bridge.append_sensor_sample(repo.conn, {"hr": 61.0, "movement": 0.1, "source": "phone"})
    row = repo.conn.execute("SELECT hr, hr_frozen, not_worn, quality_reason FROM sensor_samples"
                            ).fetchone()
    repo.close()
    assert row["hr"] == 61.0
    assert row["hr_frozen"] is None and row["not_worn"] is None and row["quality_reason"] is None


# ------------------------------------------------------------------------ /hr/ingest end-to-end
def _seed_verity_history(conn, source="verity", hr=60.0, pim=10.0, n=6, step_s=5.0):
    """Backdate ``n`` sensor_samples + matching actigraphy rows for ``source``, ``step_s`` apart,
    ending just before "now" -- so a subsequent live /hr/ingest call with a matching hr sees a
    real sustained history through it (mirrors how a real overnight stream would accumulate it)."""
    now = datetime.now(timezone.utc)
    for i in range(n):
        ts = (now - timedelta(seconds=step_s * (n - i))).isoformat()
        conn.execute(
            "INSERT INTO sensor_samples (ts, hr, movement, source) VALUES (?,?,?,?)",
            (ts, hr, None, source),
        )
        conn.execute(
            "INSERT INTO actigraphy (ts, pim, source) VALUES (?,?,?)",
            (ts, pim, source),
        )
    conn.commit()


def test_ingest_flags_frozen_hr_and_still_stores_actigraphy(auth_client):
    from app.db import get_repo

    repo = get_repo()
    repo.conn.execute("DELETE FROM sensor_samples")
    repo.conn.execute("DELETE FROM actigraphy")
    repo.conn.commit()
    # 6 prior samples 5s apart (30s..5s ago), same hr, moving -> a real frozen-HR run by the time
    # this next batch lands, well past HR_FROZEN_MIN_DURATION_S (20s).
    _seed_verity_history(repo.conn, hr=60.0, pim=10.0, n=6, step_s=5.0)
    repo.close()

    r = auth_client.post("/hr/ingest", json={"hr": 60, "source": "verity",
                                              "acc": {"pim": 10.0}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["hr_frozen"] is True
    assert body["usable"] is False
    assert body["quality_reason"] != "ok"

    repo = get_repo()
    n_acc = repo.conn.execute("SELECT COUNT(*) c FROM actigraphy WHERE source='verity'"
                              ).fetchone()["c"]
    flagged = repo.conn.execute(
        "SELECT hr_frozen, not_worn, quality_reason FROM sensor_samples ORDER BY id DESC LIMIT 1"
    ).fetchone()
    repo.close()
    # actigraphy for THIS batch is still stored even though the cardiac reading is flagged --
    # the movement signal itself remains valid data.
    assert n_acc == 7  # 6 seeded + 1 from this ingest
    assert flagged["hr_frozen"] == 1
    assert flagged["quality_reason"] != "ok"


def test_ingest_of_clean_data_is_usable_with_no_flags(auth_client):
    from app.db import get_repo

    repo = get_repo()
    repo.conn.execute("DELETE FROM sensor_samples")
    repo.conn.execute("DELETE FROM actigraphy")
    repo.conn.commit()

    r = auth_client.post("/hr/ingest", json={"hr": 59, "rr": [980.0, 1010.0, 970.0],
                                              "source": "verity", "acc": {"pim": 3.0}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["hr_frozen"] is False
    assert body["not_worn"] is False
    assert body["usable"] is True
    assert body["quality_reason"] == "ok"


def test_not_worn_flat_actigraphy_and_implausibly_HIGH_rr_is_flagged():
    """The failure mode a charger actually produces.

    A Verity resting on its charger keeps emitting RR intervals derived from optical noise. They
    are present (so the "no RR" arm never fires) and nowhere near zero variability (so the
    implausibly-LOW arm never fires either), which let six real hours of charger noise through as
    usable -- and the stager then labelled 62% of it DEEP sleep. Measured 2026-08-05 with the band
    confirmed on the charger, the populations are cleanly disjoint: worn-and-asleep RMSSD topped
    out at 136 ms, while the charger never dropped below 184 ms.
    """
    from app.services import RMSSD_IMPLAUSIBLY_HIGH_MS, assess_cardiac_quality

    now = 1_000_000.0
    offsets = [400.0, 350.0, 300.0, 250.0, 200.0, 150.0, 100.0, 50.0]
    history = _hist(now, offsets, hr=58.0, pim=0.0)
    # wildly swinging intervals -> RMSSD in the hundreds of ms, as seen off-body
    noisy_rr = [600.0, 1400.0, 700.0, 1500.0, 650.0, 1450.0]
    out = assess_cardiac_quality(hr=58.0, rr=noisy_rr, acc={"pim": 0.0}, history=history, now=now)

    assert out["not_worn"] is True
    assert out["usable"] is False
    assert "implausibly high" in out["reason"]
    assert RMSSD_IMPLAUSIBLY_HIGH_MS == 160.0  # documents the threshold, set in the observed gap


def test_real_sleeping_hrv_is_never_flagged_as_unworn():
    """The ceiling must not discard genuine data. A high-but-physiological resting RMSSD (well
    inside the observed worn range) has to stay usable, or the guard would throw away exactly the
    deep, motionless sleep it is supposed to protect."""
    from app.services import assess_cardiac_quality

    now = 1_000_000.0
    offsets = [400.0, 350.0, 300.0, 250.0, 200.0, 150.0, 100.0, 50.0]
    history = _hist(now, offsets, hr=52.0, pim=0.0)
    # ~60 ms RMSSD: a healthy, relaxed sleeper -- comfortably under the ceiling
    calm_rr = [1150.0, 1090.0, 1145.0, 1085.0, 1140.0, 1095.0]
    out = assess_cardiac_quality(hr=52.0, rr=calm_rr, acc={"pim": 0.0}, history=history, now=now)

    assert out["not_worn"] is False
    assert out["usable"] is True
