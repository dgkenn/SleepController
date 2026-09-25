"""Learners over the event ledgers must read the history once, not once per event.

2026-09-25: the health-snapshot builder hung for over an hour on the box. ``maneuver_records``
judged every resolved steer event (up to 2,400) with range queries on ``decisions.ts``, which has
no index, and each one read the whole decision log -- a ~3 KB payload per tick, all day, for 90
days. The pre-cool timing report and the steer/pre-cool resolvers did the same per event against
``raw_samples.ts``. Measured on a synthetic 90-day box: snapshot 463 s -> 4 s. Each now reads its
table once and bisects; these pin both the results (against the per-event SQL they replaced) and
the number of scans."""
import random
import time
from datetime import datetime, timedelta

from sleepctl.learning import prevention_timing as pt
from sleepctl.ml.sleep_wake import windows_from_ibis
from sleepctl.ml.sleep_staging.hrv_features import hrv_features
from sleepctl.storage.repository import Repository

NOW = datetime.now().replace(microsecond=0)
NIGHTS = 4


def _night(t):
    return (t.date() if t.hour >= 12 else t.date() - timedelta(days=1)).isoformat()


def _repo(tmp_path, nights=NIGHTS):
    """``nights`` of round-the-clock ticks ending an hour ago: raw_samples every 15 s with the pod
    frame ``ts`` shared by consecutive rows (as on the box), decisions every 30 s with the target
    stepping, awake bouts with every tick flagged, and a DST-style repeated hour in decisions."""
    repo = Repository(str(tmp_path / "s.db"))
    c = repo.conn
    rng = random.Random(5)
    raw, decs = [], []
    start = NOW - timedelta(days=nights, hours=1)
    t, i = start, 0
    while t < NOW - timedelta(hours=1):
        mins = t.hour * 60 + t.minute
        in_bed = mins >= 23 * 60 or mins < 7 * 60
        stage = ("light", "deep", "light", "rem")[(mins // 25) % 4] if in_bed else "awake"
        awake = in_bed and (mins % 97) < 6
        raw.append((t.replace(second=0).isoformat(), _night(t), "awake" if awake else stage, 0.8,
                    60.0, 50.0, 14.0, 0.1, 1, (80.0 - (i % 37) * 0.1) if i % 3 else None, 70.0,
                    -40, "maintenance" if in_bed else "idle", 1 if awake else 0, 5.0, t.isoformat()))
        if i % 2 == 0:
            tgt = None if i % 23 == 0 else (69.0 - 0.5 * ((i // 60) % 3))
            decs.append((t.isoformat(), _night(t), "maintenance" if in_bed else "idle", tgt))
        t += timedelta(seconds=15)
        i += 1
    # a repeated hour (DST fall-back): earlier ts, higher ids, different targets
    for ts, nd, st, tgt in decs[-800:-680]:
        decs.append((ts, nd, st, None if tgt is None else tgt - rng.choice((0.0, 1.0, -1.0))))
    c.executemany("INSERT INTO raw_samples (ts,night_date,stage,stage_confidence,heart_rate,hrv,"
                  "respiratory_rate,movement,presence,bed_temp_f,room_temp_f,commanded_level,"
                  "controller_state,wake_event,data_age_seconds,sample_ts) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", raw)
    c.executemany("INSERT INTO decisions (ts,night_date,state,target_temp_f) VALUES (?,?,?,?)",
                  decs)
    # events, several on exactly a tick's timestamp so the window edges are exercised
    maint = [r for r in raw if r[12] == "maintenance"]
    for k in range(600):
        r = rng.choice(maint)
        ts = r[0] if k % 2 else (datetime.fromisoformat(r[15])
                                 + timedelta(seconds=rng.randint(0, 29))).isoformat()
        c.execute("INSERT INTO steer_events (night_date,ts,maneuver,stage_before,deep_deficit_min,"
                  "frac_of_night,horizon_min,applied,deepened,succeeded,caused_wake,resolved) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (r[1], ts, rng.choice(("deepen", "deepen", "rem_warm")), "light", 5.0, 0.3,
                   rng.choice((None, 5.0, 20.0, 45.0)), rng.choice((0, 1, None)),
                   rng.randint(0, 1), rng.randint(0, 1), 0, 1))
        c.execute("INSERT INTO precool_events (night_date,ts,window_type,lead_used_min,eta_min,"
                  "prevented,resolved) VALUES (?,?,?,?,?,?,?)",
                  (r[1], ts, "cycle_boundary", 12.0, rng.choice((5.0, 15.0)), rng.randint(0, 1), 1))
    c.commit()
    return repo


class _Trace:
    """Counts statements that read ``table``, and notes whether any ran inside a write
    transaction (after a write, before its commit)."""

    def __init__(self, conn, table):
        self.table, self.reads, self.read_after_write, self._wrote = table, 0, False, False
        conn.set_trace_callback(self)

    def __call__(self, sql):
        s = " ".join(sql.split()).upper()
        if s.startswith("UPDATE") or s.startswith("INSERT"):
            self._wrote = True
        elif s.startswith("COMMIT"):
            self._wrote = False
        elif s.startswith("SELECT") and f"FROM {self.table.upper()}" in s:
            self.reads += 1
            self.read_after_write |= self._wrote


# ---------------------------------------------------------------- steer delivery (decisions)
def test_maneuver_records_match_the_per_event_queries_and_read_decisions_once(tmp_path):
    repo = _repo(tmp_path)
    # The oracle: the per-event SQL path, still used when no pre-read is supplied.
    rows = repo.conn.execute(
        "SELECT night_date, ts, horizon_min, maneuver, applied FROM steer_events "
        "WHERE resolved = 1 ORDER BY id DESC").fetchall()
    want = [repo._steer_event_delivered(r) if r["applied"] in (1, None) else None for r in rows]
    assert {True, False} <= set(want)          # both outcomes present, or the test proves nothing

    tr = _Trace(repo.conn, "decisions")
    t0 = time.time()
    got = repo.maneuver_records("deepen") + repo.maneuver_records("rem_warm")
    assert time.time() - t0 < 10
    repo.conn.set_trace_callback(None)
    assert tr.reads == 2                       # one read of the log per call, not one per event
    by_kind = {"deepen": [], "rem_warm": []}
    for r, w in zip(rows, want):
        by_kind[r["maneuver"]].append(w)
    assert [g["delivered"] for g in got] == by_kind["deepen"] + by_kind["rem_warm"]


# ---------------------------------------------------------------- pre-cool timing (raw_samples)
def _timing_per_event(repo, search_min=pt.ARRIVAL_SEARCH_MIN):
    """The report built the way it was before: one ranged raw_samples query per event."""
    rows = repo.conn.execute(
        "SELECT ts, window_type, lead_used_min, prevented FROM precool_events "
        "WHERE resolved = 1 AND ts >= ? ORDER BY ts ASC",
        ((datetime.now() - timedelta(days=30)).isoformat(),)).fetchall()
    events = []
    for r in rows:
        t0 = pt._as_dt(r["ts"])
        samples = [dict(s) for s in repo.conn.execute(
            "SELECT ts, bed_temp_f, wake_event FROM raw_samples WHERE ts >= ? AND ts <= ? "
            "ORDER BY ts ASC", ((t0 - timedelta(minutes=10)).isoformat(),
                                (t0 + timedelta(minutes=float(search_min))).isoformat()))]
        if pt.has_readings(samples, "bed_temp_f"):
            source, arrival = "bed_temp", pt.measure_arrival_min(samples, t0, search_min=search_min)
        else:
            source, arrival = None, None
        prevented = bool(r["prevented"])
        events.append(pt.PreventionEvent(
            ts=t0, window_type=r["window_type"], lead_used_min=r["lead_used_min"],
            prevented=prevented, arrival_min=arrival, arrival_source=source,
            wake_min=None if prevented else pt.first_wake_min(samples, t0, search_min=search_min)))
    return pt.analyze(events)


def test_prevention_timing_matches_per_event_queries_with_one_scan(tmp_path):
    repo = _repo(tmp_path)
    want = _timing_per_event(repo)
    tr = _Trace(repo.conn, "raw_samples")
    t0 = time.time()
    got = pt.from_repo(repo)
    assert time.time() - t0 < 10
    repo.conn.set_trace_callback(None)
    assert tr.reads == 1
    assert got.events == want.events
    assert got.to_dict() == want.to_dict()
    assert sum(1 for e in got.events if e.arrival_min is not None) > 10
    assert sum(1 for e in got.events if e.wake_min is not None) > 10


# ---------------------------------------------------------------- resolvers (write lock)
def test_resolvers_match_per_event_counts_and_read_before_writing(tmp_path):
    repo = _repo(tmp_path)
    c = repo.conn
    c.execute("UPDATE precool_events SET resolved = 0, prevented = NULL")
    c.execute("UPDATE steer_events SET resolved = 0, deepened = NULL, succeeded = NULL, "
              "caused_wake = NULL WHERE horizon_min IS NOT NULL")
    c.commit()

    def n(where, lo, hi, lo_op=">"):
        return c.execute(f"SELECT COUNT(*) FROM raw_samples WHERE {where} AND ts {lo_op} ? "
                         f"AND ts <= ?", (lo, hi)).fetchone()[0]

    want_pre = {}
    for r in c.execute("SELECT id, ts, eta_min FROM precool_events").fetchall():
        t0 = datetime.fromisoformat(r["ts"])
        end = t0 + timedelta(minutes=r["eta_min"] + 8.0)
        want_pre[r["id"]] = 0 if n("wake_event = 1", t0.isoformat(), end.isoformat(), ">=") else 1
    want_steer = {}
    for r in c.execute("SELECT id, ts, horizon_min, maneuver FROM steer_events "
                       "WHERE resolved = 0").fetchall():
        t0 = datetime.fromisoformat(r["ts"])
        lo, hi = t0.isoformat(), (t0 + timedelta(minutes=r["horizon_min"])).isoformat()
        tgt = "rem" if r["maneuver"] == "rem_warm" else "deep"
        want_steer[r["id"]] = (int(bool(n("stage = 'deep'", lo, hi))),
                               int(bool(n(f"stage = '{tgt}'", lo, hi))),
                               int(bool(n("wake_event = 1", lo, hi))))
    assert len(set(want_pre.values())) == 2 and len(set(want_steer.values())) > 2

    tr = _Trace(c, "raw_samples")
    t0 = time.time()
    assert repo.resolve_precool_events() == len(want_pre)
    assert repo.resolve_steer_events() == len(want_steer)
    assert time.time() - t0 < 10
    c.set_trace_callback(None)
    # one read per signal, and none while holding the write lock the first UPDATE takes
    assert tr.reads <= 4 and not tr.read_after_write
    got_pre = dict(c.execute("SELECT id, prevented FROM precool_events").fetchall())
    assert got_pre == want_pre
    got_steer = {r[0]: tuple(r[1:]) for r in c.execute(
        "SELECT id, deepened, succeeded, caused_wake FROM steer_events WHERE horizon_min "
        "IS NOT NULL").fetchall()}
    assert got_steer == want_steer


# ---------------------------------------------------------------- HRV windows (night export)
def test_hrv_windows_match_the_rescan_and_stay_linear():
    rng = random.Random(2)
    rr, t = [], 1_700_000_000.0
    # ~2.7 h of beats, a few duplicate stamps. The per-window HRV work (now with the
    # respiration features) is linear but not free; the old per-epoch rescan of every beat was
    # quadratic, so the bound below still catches it without flaking on a loaded CI box.
    while len(rr) < 10000:
        rr.append((t, 900.0 + rng.random() * 200))
        t += 0.0 if rng.random() < 0.01 else 0.9 + rng.random() * 0.2
    rng.shuffle(rr)
    t0 = time.time()
    feats, starts = windows_from_ibis(rr)
    assert time.time() - t0 < 60
    srt = sorted(rr, key=lambda x: x[0])
    for k in range(0, len(starts), 37):        # spot-check against the full rescan
        lo = starts[k] - 300.0
        win = [(ts, v) for ts, v in srt if lo <= ts <= starts[k]]
        want = hrv_features([w[0] for w in win], [w[1] for w in win]) if len(win) >= 8 else {}
        assert feats[k] == want
