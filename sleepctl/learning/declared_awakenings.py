"""Declared awakenings from the morning note: the second source of ground truth.

The marker gesture declares "awake NOW" at a known second. A note written in the morning --
"awake 00:15-00:25, up 3:10" -- declares the same thing more coarsely, from recall, and it
is the only labelled data on the nights the sleeper forgot to tap. 2026-09-19: "a few
awakenings around midnight-1am" was said in conversation and recorded nowhere.

Grammar (case-insensitive, one clause per comma / semicolon / line):
    <keyword> ... <time>[-<time>]        keyword in awake | woke | wake | up | awakening
    time: H:MM or HH:MM, optional am/pm  (a lone hour like "3am" also works)
A single time is a five-minute awakening starting then. Times from noon to midnight belong
to the night's evening; midnight to noon belong to the following morning.

Everything here is a pure function of the note text plus the night's own samples.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

KEYWORDS = ("awake", "awakening", "woke", "wake", "up ")
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
_RANGE = re.compile(_TIME + r"\s*(?:-|–|—|to|until|till)\s*" + _TIME, re.I)
_SINGLE = re.compile(r"(?<![\d:])" + _TIME + r"(?![\d:])", re.I)
SINGLE_MIN = 5.0
#: A declared instant is scored against the stage held within this many minutes of it.
MATCH_MIN = 3.0


def _hhmm(h: str, m: Optional[str], ap: Optional[str]) -> Optional[Tuple[int, int]]:
    try:
        hh, mm = int(h), int(m or 0)
    except Exception:
        return None
    ap = (ap or "").lower().replace(".", "")
    if ap == "pm" and hh < 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def parse_declared(text: str) -> List[Tuple[Tuple[int, int], Optional[Tuple[int, int]]]]:
    """[(start_hhmm, end_hhmm_or_None), ...] for every awakening clause in the note."""
    out = []
    for clause in re.split(r"[,;\n]+", text or ""):
        low = " " + clause.lower().strip() + " "
        if not any(k in low for k in KEYWORDS):
            continue
        m = _RANGE.search(clause)
        if m:
            a = _hhmm(m.group(1), m.group(2), m.group(3) or m.group(6))
            b = _hhmm(m.group(4), m.group(5), m.group(6))
            if a and b:
                out.append((a, b))
                continue
        m = _SINGLE.search(clause)
        if m:
            # a bare number with no colon and no am/pm is not a time ("up 3 times")
            if m.group(2) is None and not m.group(3):
                continue
            a = _hhmm(m.group(1), m.group(2), m.group(3))
            if a:
                out.append((a, None))
    return out


def _at(night_date: str, hhmm: Tuple[int, int]) -> datetime:
    d = datetime.fromisoformat(night_date)
    if hhmm[0] < 12:
        d += timedelta(days=1)
    return d.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)


def declared_intervals(night_date: str, texts: List[str]) -> List[Dict[str, object]]:
    """Absolute (naive local) intervals for a night from its note texts."""
    out = []
    for text in texts:
        for start, end in parse_declared(text):
            s = _at(night_date, start)
            e = _at(night_date, end) if end else s + timedelta(minutes=SINGLE_MIN)
            if e <= s:
                e += timedelta(days=1) if (e + timedelta(days=1) - s) < timedelta(hours=6) else timedelta(minutes=SINGLE_MIN)
            out.append({"start": s.isoformat(), "end": e.isoformat(),
                        "minutes": round((e - s).total_seconds() / 60.0, 1)})
    return out


def night_note_texts(repo, night_date: str) -> List[str]:
    """Notes filed under the night's evening date or the following morning's."""
    try:
        nxt = (datetime.fromisoformat(night_date) + timedelta(days=1)).date().isoformat()
        rows = repo.conn.execute("SELECT text FROM notes WHERE date IN (?, ?) ORDER BY id ASC",
                                 (night_date, nxt)).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def score_declared(repo, night_date: str) -> Dict[str, object]:
    """Each declared awakening against the stages the stager held inside it."""
    intervals = declared_intervals(night_date, night_note_texts(repo, night_date))
    scored = []
    for iv in intervals:
        try:
            rows = repo.conn.execute(
                "SELECT ts, stage FROM raw_samples WHERE night_date = ? AND ts >= ? AND ts <= ? "
                "ORDER BY ts ASC", (night_date, iv["start"], iv["end"])).fetchall()
        except Exception:
            rows = []
        stages = [str(r[1]) for r in rows if r[1] and str(r[1]) != "unknown"]
        awake = sum(1 for s in stages if s == "awake")
        mix: Dict[str, int] = {}
        for s in stages:
            mix[s] = mix.get(s, 0) + 1
        scored.append(dict(iv, n_samples=len(stages), awake_fraction=(round(awake / len(stages), 2) if stages else None),
                           scored_awake=(awake / len(stages) >= 0.5) if stages else None, stage_mix=mix))
    judged = [s for s in scored if s["scored_awake"] is not None]
    return {"n": len(scored), "intervals": scored,
            "n_scored_awake": sum(1 for s in judged if s["scored_awake"]),
            "agreement": (round(sum(1 for s in judged if s["scored_awake"]) / len(judged), 2) if judged else None)}


def declared_instants(repo, nights: int = 30) -> List[Tuple[str, Optional[str]]]:
    """(midpoint_ts, stage_at_midpoint) for every declared awakening in recent notes -- the
    same shape the marker gesture produces, so the wake-truth learner can count both."""
    out: List[Tuple[str, Optional[str]]] = []
    try:
        rows = repo.conn.execute(
            "SELECT date, text FROM notes ORDER BY id DESC LIMIT ?", (int(nights) * 3,)).fetchall()
    except Exception:
        return out
    for date, text in rows:
        if not text:
            continue
        for night in _candidate_nights(str(date)):
            for iv in declared_intervals(night, [str(text)]):
                s, e = datetime.fromisoformat(iv["start"]), datetime.fromisoformat(iv["end"])
                mid = s + (e - s) / 2
                try:
                    row = repo.conn.execute(
                        "SELECT stage FROM raw_samples WHERE night_date = ? AND ts >= ? AND ts <= ? "
                        "AND stage IS NOT NULL AND stage != 'unknown' ORDER BY ABS(julianday(ts) - julianday(?)) LIMIT 1",
                        (night, (mid - timedelta(minutes=MATCH_MIN)).isoformat(),
                         (mid + timedelta(minutes=MATCH_MIN)).isoformat(), mid.isoformat())).fetchone()
                except Exception:
                    row = None
                if row is not None:
                    out.append((mid.isoformat(), str(row[0])))
                    break            # matched under one night's samples: do not double count
    return out


def _candidate_nights(date: str) -> List[str]:
    """A note dated D may describe the night that started D (written that evening or the next
    morning under the evening's date) or the night that started D-1 (written the morning after)."""
    try:
        d = datetime.fromisoformat(date)
    except Exception:
        return [date]
    return [(d - timedelta(days=1)).date().isoformat(), d.date().isoformat()]
