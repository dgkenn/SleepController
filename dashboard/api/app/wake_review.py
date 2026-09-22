"""The wake review: end the session, then say what the night was actually like.

Two things no sensor can supply, collected in the one moment the sleeper is certainly awake
and looking at the app:

  * how the night FELT -- every learner in this system scores itself against objective
    numbers derived from the same model whose staging the plausibility audit keeps rejecting;
  * a verdict on each awakening the detector believes it found. A CONFIRMED one is a declared
    awake instant, exactly like a marker gesture. A DENIED one is a declared ASLEEP instant at
    a moment the detector called wake -- false-alarm evidence, which nothing else has ever
    produced, and which the marker gesture by construction cannot.

Kept deliberately short: three taps and the awakening list. A survey nobody finishes at 6 a.m.
measures nothing.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import List, Optional

#: Wake ticks closer together than this are one awakening, not several.
EPISODE_GAP_MIN = 10.0
#: Episodes shorter than this are not worth asking about (a single flagged tick).
MIN_EPISODE_TICKS = 1
#: The most awakenings to ask about. Beyond this the list is a chore, not a question.
MAX_EPISODES = 8
RESTED_CHOICES = (1, 2, 3, 4, 5)
TEMPERATURE_CHOICES = ("too_cold", "bit_cold", "right", "bit_warm", "too_warm")
ONSET_CHOICES = ("fast", "normal", "slow")
VERDICTS = ("yes", "no", "unsure")


def night_date_for(now: Optional[datetime] = None) -> str:
    """The night a review filed now belongs to (noon cutoff, as the controller groups them)."""
    now = now or datetime.now()
    return (now - timedelta(hours=12)).date().isoformat()


def suspected_awakenings(repo, night_date: str) -> List[dict]:
    """The awakenings the detector believes it found, clustered into episodes.

    One row per episode: when it started, how long the flagged run lasted, and the stage the
    stager held. These are what the sleeper is asked to confirm or deny.
    """
    try:
        rows = repo.conn.execute(
            # The voter's wake events AND every tick the controller spent answering an
            # awakening (WAKE_RECOVERY): a sustained awakening the voter did not log is still
            # one the bed acted on, and its verdict is exactly what the learners need.
            "SELECT ts, stage FROM raw_samples WHERE night_date = ? "
            "AND (wake_event = 1 OR controller_state = 'wake_recovery') "
            "ORDER BY ts ASC", (night_date,)).fetchall()
    except Exception:
        return []
    eps: List[dict] = []
    for r in rows:
        try:
            t = datetime.fromisoformat(str(r[0]))
        except Exception:
            continue
        stage = str(r[1] or "unknown")
        if eps and (t - eps[-1]["_last"]).total_seconds() / 60.0 <= EPISODE_GAP_MIN:
            eps[-1]["_last"] = t
            eps[-1]["n_ticks"] += 1
            eps[-1]["stages"].add(stage)
        else:
            eps.append({"ts": t.isoformat(), "_last": t, "_start": t, "n_ticks": 1,
                        "stages": {stage}})
    out = []
    for e in eps:
        if e["n_ticks"] < MIN_EPISODE_TICKS:
            continue
        mins = (e["_last"] - e["_start"]).total_seconds() / 60.0
        out.append({"ts": e["ts"], "minutes": round(mins, 1), "n_ticks": e["n_ticks"],
                    "stages": sorted(e["stages"])})
    # Longest first: if the list has to be cut, keep the ones most worth asking about.
    out.sort(key=lambda e: (-e["n_ticks"], e["ts"]))
    out = out[:MAX_EPISODES]
    out.sort(key=lambda e: e["ts"])
    return out


def get_review(repo, night_date: str) -> Optional[dict]:
    try:
        row = repo.conn.execute(
            "SELECT night_date, ts, rested, temperature, onset_feel, note, verdicts "
            "FROM wake_review WHERE night_date = ?", (night_date,)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        verdicts = json.loads(row[6]) if row[6] else []
    except Exception:
        verdicts = []
    return {"night_date": row[0], "ts": row[1], "rested": row[2], "temperature": row[3],
            "onset_feel": row[4], "note": row[5], "verdicts": verdicts}


def review_payload(repo, night_date: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """Everything the popup needs: the night, its suspected awakenings, and any review
    already filed for it (so a second visit shows the answers rather than asking again)."""
    night_date = night_date or night_date_for(now)
    return {"night_date": night_date,
            "awakenings": suspected_awakenings(repo, night_date),
            "review": get_review(repo, night_date)}


def _clean(value, allowed):
    return value if value in allowed else None


def save_review(repo, payload: dict, now: Optional[datetime] = None) -> dict:
    """Store the review and turn its verdicts into declared instants the learners can read."""
    now = now or datetime.now()
    night_date = str(payload.get("night_date") or night_date_for(now))
    rested = payload.get("rested")
    try:
        rested = int(rested) if rested is not None else None
    except (TypeError, ValueError):
        rested = None
    if rested is not None and rested not in RESTED_CHOICES:
        rested = None
    temperature = _clean(payload.get("temperature"), TEMPERATURE_CHOICES)
    onset_feel = _clean(payload.get("onset_feel"), ONSET_CHOICES)
    note = (str(payload.get("note"))[:500] if payload.get("note") else None)
    verdicts = []
    for v in (payload.get("verdicts") or []):
        ts, verdict = str(v.get("ts") or ""), _clean(v.get("verdict"), VERDICTS)
        if ts and verdict:
            verdicts.append({"ts": ts, "verdict": verdict})
    repo.conn.execute(
        "INSERT INTO wake_review (night_date, ts, rested, temperature, onset_feel, note, verdicts) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(night_date) DO UPDATE SET "
        "ts=excluded.ts, rested=excluded.rested, temperature=excluded.temperature, "
        "onset_feel=excluded.onset_feel, note=excluded.note, verdicts=excluded.verdicts",
        (night_date, now.isoformat(), rested, temperature, onset_feel, note,
         json.dumps(verdicts)))
    n_events = _record_declared(repo, night_date, verdicts)
    repo.conn.commit()
    return {"ok": True, "night_date": night_date, "verdicts": len(verdicts),
            "declared_instants": n_events}


def _record_declared(repo, night_date: str, verdicts: List[dict]) -> int:
    """A confirmed awakening is a declared-awake instant; a denied one is declared asleep.

    Written into the same ``marker_vs_stage`` event stream the marker gesture uses, with the
    stage the stager actually held at that moment, so ``wake_truth`` reads both sources
    without knowing where they came from.
    """
    n = 0
    for v in verdicts:
        if v["verdict"] == "unsure":
            continue
        try:
            row = repo.conn.execute(
                "SELECT stage FROM raw_samples WHERE night_date = ? AND ts >= ? "
                "AND stage IS NOT NULL AND stage != 'unknown' ORDER BY ts ASC LIMIT 1",
                (night_date, v["ts"])).fetchone()
        except Exception:
            row = None
        stage = str(row[0]) if row is not None else None
        declared_awake = v["verdict"] == "yes"
        try:
            repo.conn.execute(
                "INSERT INTO events (ts, category, severity, code, message, data) "
                "VALUES (?,?,?,?,?,?)",
                (v["ts"], "sensor", "info", "marker_vs_stage",
                 f"wake review: {'confirmed' if declared_awake else 'denied'} "
                 f"while the stager said {stage or 'unknown'}",
                 json.dumps({"stage_at_marker": stage, "kind": "review",
                             "declared_awake": declared_awake})))
            n += 1
        except Exception:
            continue
    return n
