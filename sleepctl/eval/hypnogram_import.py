"""Import a scored hypnogram from an EEG sleep headband into ``eeg_hypnogram``.

A few nights with a headband worn beside the arm band are the only personal ground truth the
stager can be measured and calibrated against (``sleepctl.eval.eeg_agreement``,
``sleepctl.learning.eeg_calibration``). The device is not known in advance, so the importer is
deliberately forgiving about the export's shape:

  * delimited text (CSV / TSV / semicolon / whitespace), with or without a header:
      - a timestamp + stage per row (ISO with or without offset, Unix s / ms, or time of day
        with a night date to anchor it -- the Dreem-style ``SLEEP-S2  23:10:30`` export);
      - intervals (start + end or duration), expanded to 30 s epochs;
      - a bare epoch list (one stage per row, or ``epoch, stage``) plus a start time, given
        as a parameter or as a ``start: ...`` line at the top of the file;
      - the reduced BIDSleep / sleep-accel ``t_seconds stage`` format
        (``scripts/bidsleep_reduce.py``), relative to a start time;
  * JSON: ``{"start": ..., "epoch_s": 30, "stages": [...]}`` or a list of
    ``{"time": ..., "stage": ...}`` records;
  * EDF+ annotation files (``Sleep stage W/1/2/3/4/R/?`` annotations, R&K or AASM);
  * the BIDSleep ``labels.mat`` (``recStart`` + ``expert_label`` / ``dreem_label``), read with
    the loader in ``scripts/bidsleep_reduce.py`` (needs scipy).

Stage vocabularies map to the controller's classes: W / Wake / 0 -> awake; N1 / N2 / core ->
light; N3 / N4 / SWS / deep -> deep; R / REM -> rem. Movement time, artefact and unscored epochs
are kept as ``unknown`` (stored, never scored against).

TIME ZONES. Every epoch is resolved to a real instant (``epoch_unix``) before it is stored. A
timestamp carrying an offset is exact. A naive one is wall-clock time in ``tz`` (an IANA name)
when given, else in this machine's local zone -- the same zone the daemon wrote the naive-local
``raw_samples.ts`` in, which is what makes an export from a device set to local time line up
with the controller's rows. ``epoch_ts`` stores the naive-local rendering of the same instant
(see ``sleepctl.storage.schema``).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

EPOCH_S = 30.0
CLASSES = ("awake", "light", "deep", "rem")
UNKNOWN = "unknown"

#: A change-point row (a stage that lasts until the next row) longer than this is a recording
#: gap, not a 3-hour stage: only one epoch of it is kept.
MAX_ROW_SPAN_S = 90 * 60.0

_WAKE = {"w", "wake", "awake", "wk", "s0", "n0", "wakefulness", "wach"}
_LIGHT = {"n1", "n2", "s1", "s2", "light", "core", "nrem1", "nrem2", "stage1", "stage2",
          "light sleep", "lightsleep", "1/2", "n1/n2"}
_DEEP = {"n3", "n4", "s3", "s4", "deep", "sws", "slow wave", "slow wave sleep", "nrem3",
         "nrem4", "deep sleep", "deepsleep", "stage3", "stage4", "n3/n4"}
_REM = {"r", "rem", "rem sleep", "remsleep", "paradoxical", "stage r", "stager"}
_UNSCORED = {"?", "unscored", "unknown", "u", "mt", "movement", "movement time", "artifact",
             "artefact", "na", "n/a", "nan", "none", "x", "not scored", "notscored", "inbed",
             "in bed", "asleep", "asleepunspecified", "no data", "nodata", "missing"}


# --------------------------------------------------------------------------- vocabulary
def _norm_token(tok) -> str:
    s = str(tok).strip().strip("\"'").lower()
    s = re.sub(r"^(hkcategoryvaluesleepanalysis|sleep[\s_-]*stage|sleep[\s_-]*|stage[\s_-]*)",
               "", s).strip(" :-_")
    s = re.sub(r"^(stage[\s_-]*)", "", s).strip(" :-_")
    return re.sub(r"\s+", " ", s)


def map_stage(tok, numeric_scheme: str = "aasm") -> Optional[str]:
    """Controller class for one exported stage label, ``"unknown"`` for an explicitly unscored
    epoch, and None for something that is not a stage label at all (an arousal annotation, a
    column header).

    ``numeric_scheme`` settles the one real ambiguity in numeric codes: under ``"aasm"``
    (BIDSleep / Dreem ``0 W, 1 N1, 2 N2, 3 N3, 4 REM, 5 unscored``) 4 is REM; under ``"rk"``
    (Rechtschaffen & Kales, sleep-accel ``... 3, 4 deep, 5 REM, -1 unscored``) 4 is deep.
    """
    raw = str(tok).strip()
    if raw == "":
        return None
    num = raw.strip("\"'")
    s = num if re.fullmatch(r"[+-]?\d+(\.0+)?", num) else _norm_token(raw)
    if re.fullmatch(r"[+-]?\d+(\.0+)?", s):
        n = int(float(s))
        if n == 0:
            return "awake"
        if n in (1, 2):
            return "light"
        if n == 3:
            return "deep"
        if n == 4:
            return "deep" if numeric_scheme == "rk" else "rem"
        if n == 5:
            return "rem" if numeric_scheme == "rk" else UNKNOWN
        return UNKNOWN
    if s in _WAKE:
        return "awake"
    if s in _LIGHT:
        return "light"
    if s in _DEEP:
        return "deep"
    if s in _REM:
        return "rem"
    if s in _UNSCORED:
        return UNKNOWN
    return None


def _auto_scheme(tokens: Sequence[str]) -> Tuple[str, Optional[str]]:
    """Pick the numeric scheme for a file whose stages are numbers.

    4 without 5 is AASM (4 = REM); 5 without 4 is the sleep-accel reduction (5 = REM). With
    both, the more frequent code is REM -- adult REM is ~20-25% of the night, while S4 and an
    unscored code are each a few percent -- and the caller is told it was a guess."""
    nums = [int(float(t)) for t in tokens if re.fullmatch(r"\s*[+-]?\d+(\.0+)?\s*", str(t))]
    c4, c5 = nums.count(4), nums.count(5)
    if c5 and not c4:
        return "rk", None
    if c4 and c5:
        scheme = "rk" if c5 > c4 else "aasm"
        return scheme, (f"numeric codes 4 and 5 both present; read as "
                        f"{'R&K (4 deep, 5 REM)' if scheme == 'rk' else 'AASM (4 REM, 5 unscored)'}"
                        f" -- pass numeric_scheme to override")
    return "aasm", None


# --------------------------------------------------------------------------- time parsing
def resolve_tz(tz):
    """An IANA name / tzinfo / None (machine local) -> tzinfo or None."""
    if tz is None or tz == "" or hasattr(tz, "utcoffset"):
        return tz or None
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(str(tz))
    except Exception as exc:
        raise ValueError(f"unknown time zone {tz!r}") from exc


def _instant(dt: datetime, tz) -> float:
    """A parsed datetime -> Unix seconds. Naive means wall clock in ``tz`` (else machine local,
    which is what ``datetime.timestamp`` assumes for a naive value)."""
    if dt.tzinfo is not None:
        return dt.timestamp()
    if tz is not None:
        return dt.replace(tzinfo=tz).timestamp()
    return dt.timestamp()


_DT_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
               "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
               "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%y %H.%M.%S",
               "%d-%b-%Y %H:%M:%S", "%Y%m%d %H%M%S", "%Y%m%dT%H%M%S")
_TOD_FORMATS = ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p", "%H.%M.%S")


def _parse_datetime(s: str) -> Optional[datetime]:
    s = str(s).strip().strip("\"'")
    if not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_time_of_day(s: str):
    s = str(s).strip().strip("\"'")
    for fmt in _TOD_FORMATS:
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _is_number(s) -> bool:
    try:
        float(str(s).strip())
        return True
    except ValueError:
        return False


def _numeric_instant(v: float) -> Optional[float]:
    """Unix ms / s for an absolute numeric timestamp, None for a relative offset."""
    if v > 1e11:
        return v / 1000.0
    if v > 1e8:
        return v
    return None


def _parse_start(start, tz, night_date: Optional[str]) -> Optional[float]:
    """A start-of-recording value (datetime, number or string) -> Unix seconds."""
    if start is None or start == "":
        return None
    if isinstance(start, datetime):
        return _instant(start, tz)
    if isinstance(start, (int, float)) or _is_number(start):
        v = _numeric_instant(float(start))
        if v is None:
            raise ValueError(f"start {start!r} is not an absolute time")
        return v
    dt = _parse_datetime(str(start))
    if dt is not None:
        return _instant(dt, tz)
    tod = _parse_time_of_day(str(start))
    if tod is not None and night_date:
        return _instant(_anchor_tod(tod, night_date), tz)
    raise ValueError(f"could not read start time {start!r}"
                     + ("" if night_date else " (a time of day needs a night date)"))


def _anchor_tod(tod, night_date: str) -> datetime:
    """A time of day on the night of ``night_date``: noon onwards is that evening, earlier is
    the next morning -- the same noon cutoff the controller files nights under."""
    d = date.fromisoformat(night_date)
    if tod.hour < 12:
        d = d + timedelta(days=1)
    return datetime.combine(d, tod)


# --------------------------------------------------------------------------- result type
@dataclass
class Hypnogram:
    """Parsed epochs: ``(unix_start, stage, raw_label)``, ascending, on a fixed epoch length."""

    epochs: List[Tuple[float, str, str]] = field(default_factory=list)
    epoch_s: float = EPOCH_S
    fmt: str = ""
    device: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def scored(self) -> int:
        return sum(1 for _, st, _ in self.epochs if st in CLASSES)

    def span(self) -> Tuple[Optional[float], Optional[float]]:
        if not self.epochs:
            return None, None
        return self.epochs[0][0], self.epochs[-1][0] + self.epoch_s


def _expand(rows: List[Tuple[float, Optional[float], str, str]], epoch_s: float,
            warnings: List[str]) -> List[Tuple[float, str, str]]:
    """``(start, duration|None, stage, raw)`` rows -> one entry per ``epoch_s`` epoch.

    A row without a duration lasts until the next row (so an epoch-per-row export and a
    change-point export both come out right), except across a recording gap, where it is one
    epoch. An unscored epoch never overwrites a scored one: exports that overlay an "in bed"
    interval on top of the stages would otherwise blank the whole night."""
    rows = sorted(rows, key=lambda r: r[0])
    out: dict = {}
    gaps = 0
    for i, (t0, dur, stage, raw) in enumerate(rows):
        if dur is None:
            nxt = rows[i + 1][0] if i + 1 < len(rows) else None
            dur = (nxt - t0) if nxt is not None else epoch_s
            if dur <= 0:
                dur = epoch_s
            elif dur > MAX_ROW_SPAN_S:
                gaps += 1
                dur = epoch_s
        n = max(1, int(round(float(dur) / epoch_s)))
        for k in range(n):
            t = round(t0 + k * epoch_s, 3)
            prev = out.get(t)
            if prev is not None and prev[0] in CLASSES and stage not in CLASSES:
                continue
            out[t] = (stage, raw)
    if gaps:
        warnings.append(f"{gaps} gap(s) longer than {MAX_ROW_SPAN_S / 60:.0f} min between rows")
    return [(t, st, raw) for t, (st, raw) in sorted(out.items())]


# --------------------------------------------------------------------------- delimited text
_START_RE = re.compile(
    r"^\s*[#;/]*\s*(start[\s_-]*(time|date|datetime)?|recording[\s_-]*start|recstart|"
    r"lights[\s_-]*off|start_?ts)\s*[:=,\t]\s*(?P<v>.+?)\s*$", re.I)
_EPOCH_RE = re.compile(r"^\s*[#;/]*\s*epoch[\s_-]*(length|len|s|sec|seconds|duration)?\s*"
                       r"(\[s\]|\(s\))?\s*[:=,\t]\s*(?P<v>\d+(\.\d+)?)\s*s?\s*$", re.I)
_DEVICE_RE = re.compile(r"^\s*[#;/]*\s*(device|source|model)\s*[:=]\s*(?P<v>.+?)\s*$", re.I)


def _cells(line: str, delim: Optional[str]) -> List[str]:
    if delim is None:
        # Aligned columns keep multi-word labels ("Sleep stage W") together.
        parts = re.split(r"\s{2,}", line.strip())
        return parts if len(parts) > 1 else line.split()
    return [c.strip() for c in next(csv.reader([line], delimiter=delim))]


def _header_role(name: str) -> Optional[str]:
    n = re.sub(r"[^a-z]", "", name.lower())
    if not n:
        return None
    if "stage" in n or "hypno" in n or n in ("label", "sleep", "score", "scoring", "state",
                                              "event", "annotation", "value", "level", "class",
                                              "description"):
        return "stage"
    if "dur" in n or n in ("length", "lengths"):
        return "duration"
    if n.startswith("end") or n.startswith("stop") or n in ("to", "until", "endtime", "enddate"):
        return "end"
    if n == "date":
        return "date"
    if n in ("epoch", "epochno", "epochnumber", "epochindex", "index", "idx", "n", "no"):
        return "index"
    if ("time" in n or "start" in n or "onset" in n or "begin" in n
            or n in ("t", "ts", "from", "datetime", "seconds", "sec", "secs")):
        return "time"
    return None


def _parse_delimited(text: str, *, start, tz, night_date, epoch_s, numeric_scheme,
                     warnings) -> Hypnogram:
    meta_start, meta_device = None, None
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _START_RE.match(line)
        if m and not lines:
            v = m.group("v")
            # "Start Time,End Time,Stage" is a header, not a start line.
            if _is_number(v) or _parse_datetime(v) or _parse_time_of_day(v):
                meta_start = v
                continue
        m = _EPOCH_RE.match(line)
        if m and not lines:
            epoch_s = float(m.group("v"))
            continue
        m = _DEVICE_RE.match(line)
        if m and not lines:
            meta_device = m.group("v")
            continue
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
    if not lines:
        raise ValueError("no rows found")
    sample = "\n".join(lines[:20])
    counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
    delim = max(counts, key=counts.get)
    if counts[delim] < min(len(lines[:20]), 2):
        delim = None
    first = _cells(lines[0], delim)
    roles = [_header_role(c) for c in first]
    has_header = (any(r is not None for r in roles)
                  and not any(map_stage(c) in CLASSES for c in first if not _is_number(c))
                  and not any(_is_number(c) or _parse_datetime(c) for c in first))
    rows = [_cells(ln, delim) for ln in (lines[1:] if has_header else lines)]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("no data rows found")
    ncol = max(len(r) for r in rows)
    col = lambda j: [r[j] if j < len(r) else "" for r in rows]  # noqa: E731

    idx = {}
    if has_header:
        for j, r in enumerate(roles):
            if r and r not in idx:
                idx[r] = j
        # Several stage-like headers (Dreem: "Sleep Stage" and "Event"): prefer one naming stage.
        for j, name in enumerate(first):
            if "stage" in name.lower() or "hypno" in name.lower():
                idx["stage"] = j
                break
    if "stage" not in idx:
        # No usable header: the stage column is the one whose cells read as stages, preferring
        # word labels over numbers (a numeric column could as easily be a clock or an index).
        best, best_key = None, None
        for j in range(ncol):
            if j in idx.values():
                continue
            vals = [v for v in col(j) if v.strip()]
            if not vals:
                continue
            mapped = [map_stage(v) for v in vals]
            hits = sum(1 for m in mapped if m is not None)
            scored = sum(1 for m in mapped if m in CLASSES)
            words = sum(1 for v, m in zip(vals, mapped) if not _is_number(v) and m is not None)
            numeric_ok = all(_is_number(v) and -2 <= float(v) <= 9 and float(v) == int(float(v))
                             for v in vals)
            if hits / len(vals) < 0.9 or (not words and not numeric_ok):
                continue
            key = (words > 0, scored / len(vals), hits / len(vals), -j if words else j)
            if best_key is None or key > best_key:
                best, best_key = j, key
        if best is None:
            raise ValueError("could not find a sleep-stage column")
        idx["stage"] = best
    if "time" not in idx and "index" not in idx:
        for j in range(ncol):
            if j == idx["stage"]:
                continue
            vals = [v for v in col(j) if v.strip()]
            if vals and all(_is_number(v) or _parse_datetime(v) or _parse_time_of_day(v)
                            for v in vals):
                idx["time"] = j
                break

    stages_raw = col(idx["stage"])
    scheme = numeric_scheme
    if scheme == "auto":
        scheme, warn = _auto_scheme(stages_raw)
        if warn:
            warnings.append(warn)
    start_unix = _parse_start(start if start not in (None, "") else meta_start, tz, night_date)

    # Resolve every row's start instant.
    starts: List[Optional[float]] = []
    tcol = col(idx["time"]) if "time" in idx else None
    dcol = col(idx["date"]) if "date" in idx else None
    if tcol is not None:
        prev_dt: Optional[datetime] = None
        numeric = all(_is_number(v) for v in tcol if v.strip())
        for i, v in enumerate(tcol):
            v = v.strip()
            if not v:
                starts.append(None)
                continue
            if numeric:
                x = float(v)
                ab = _numeric_instant(x)
                if ab is None:
                    if start_unix is None:
                        raise ValueError("times are relative offsets; a start time is needed")
                    ab = start_unix + x
                starts.append(ab)
                continue
            if dcol is not None and dcol[i].strip():
                v = f"{dcol[i].strip()} {v}"
            dt = _parse_datetime(v)
            if dt is None:
                tod = _parse_time_of_day(v)
                if tod is None:
                    raise ValueError(f"unreadable time {v!r} on row {i + 1}")
                if prev_dt is None:
                    if night_date:
                        dt = _anchor_tod(tod, night_date)
                    elif start_unix is not None:
                        base = datetime.fromtimestamp(start_unix, tz) if tz else \
                            datetime.fromtimestamp(start_unix)
                        dt = datetime.combine(base.date(), tod)
                        if dt < base.replace(tzinfo=None) - timedelta(hours=12):
                            dt += timedelta(days=1)
                    else:
                        raise ValueError("times of day need a night date to anchor them")
                else:
                    dt = datetime.combine(prev_dt.date(), tod)
                    if dt < prev_dt - timedelta(hours=1):   # rolled over midnight
                        dt += timedelta(days=1)
                prev_dt = dt
            starts.append(_instant(dt, tz))
    else:
        if start_unix is None:
            raise ValueError("an epoch list needs a start time (parameter or a 'start:' line)")
        if "index" in idx:
            ix = [int(float(v)) for v in col(idx["index"])]
            base = min(ix) if ix else 0
            base = base if base in (0, 1) else 0
            starts = [start_unix + (k - base) * epoch_s for k in ix]
        else:
            starts = [start_unix + k * epoch_s for k in range(len(rows))]

    durs: List[Optional[float]] = [None] * len(rows)
    if "duration" in idx:
        for i, v in enumerate(col(idx["duration"])):
            if _is_number(v) and float(v) > 0:
                durs[i] = float(v)
    elif "end" in idx:
        for i, v in enumerate(col(idx["end"])):
            if starts[i] is None or not v.strip():
                continue
            if _is_number(v):
                e = _numeric_instant(float(v))
                e = e if e is not None else ((start_unix or 0.0) + float(v))
            else:
                dt = _parse_datetime(v)
                if dt is None:
                    continue
                e = _instant(dt, tz)
            if e > starts[i]:
                durs[i] = e - starts[i]

    parsed, skipped = [], 0
    for i, raw in enumerate(stages_raw):
        st = map_stage(raw, scheme)
        if starts[i] is None or st is None:
            skipped += 1
            continue
        parsed.append((starts[i], durs[i], st, raw.strip()))
    if skipped:
        warnings.append(f"{skipped} row(s) without a readable time or stage were skipped")
    return Hypnogram(epochs=_expand(parsed, epoch_s, warnings), epoch_s=epoch_s,
                     fmt="delimited", device=meta_device, warnings=warnings)


# --------------------------------------------------------------------------- JSON
def _parse_json(obj, *, start, tz, night_date, epoch_s, numeric_scheme, warnings) -> Hypnogram:
    device = None
    items = obj
    if isinstance(obj, dict):
        device = obj.get("device") or obj.get("source")
        epoch_s = float(obj.get("epoch_s") or obj.get("epoch_length") or epoch_s)
        if start in (None, ""):
            for k in ("start", "start_time", "startTime", "recStart", "bedtime_start",
                      "lights_off"):
                if obj.get(k) not in (None, ""):
                    start = obj[k]
                    break
        for k in ("hypnogram", "epochs", "stages", "data", "sleep_stages", "labels"):
            if isinstance(obj.get(k), list):
                items = obj[k]
                break
        else:
            raise ValueError("no epoch list in the JSON document")
    if not isinstance(items, list):
        raise ValueError("unrecognised JSON hypnogram")
    if items and isinstance(items[0], dict):
        # Records -> the delimited parser, so both paths share the column logic.
        keys = list(items[0].keys())
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(keys)
        for it in items:
            w.writerow([it.get(k, "") for k in keys])
        h = _parse_delimited(buf.getvalue(), start=start, tz=tz, night_date=night_date,
                             epoch_s=epoch_s, numeric_scheme=numeric_scheme, warnings=warnings)
    else:
        text = "\n".join(str(x) for x in items)
        h = _parse_delimited(text, start=start, tz=tz, night_date=night_date, epoch_s=epoch_s,
                             numeric_scheme=numeric_scheme, warnings=warnings)
    h.fmt = "json"
    h.device = h.device or device
    return h


# --------------------------------------------------------------------------- EDF+
def _parse_edf(data: bytes, *, tz, epoch_s, warnings) -> Hypnogram:
    """Stage annotations from an EDF+ file (a hypnogram file or a full recording).

    The header's start date/time is the recording's wall clock with no zone, so it is read in
    ``tz`` / machine local like any other naive time. Stage codes in ``Sleep stage N`` follow
    R&K (4 is deep); AASM ``N3`` reads as deep either way."""
    if len(data) < 256 or data[:1] != b"0":
        raise ValueError("not an EDF file")
    h = data[:256].decode("ascii", "replace")
    recording = h[88:168]
    d, t = h[168:176].strip(), h[176:184].strip()
    header_bytes = int(h[184:192].strip())
    n_records = int(h[236:244].strip())
    ns = int(h[252:256].strip())
    try:
        dd, mm, yy = (int(x) for x in d.split("."))
        year = 1900 + yy if yy >= 85 else 2000 + yy
        m = re.search(r"Startdate\s+(\d{2}-[A-Za-z]{3}-\d{4})", recording)
        if m:
            year = datetime.strptime(m.group(1), "%d-%b-%Y").year
        hh, mi, ss = (int(x) for x in t.split("."))
        start_dt = datetime(year, mm, dd, hh, mi, ss)
    except Exception as exc:
        raise ValueError(f"unreadable EDF start date/time {d!r} {t!r}") from exc
    start_unix = _instant(start_dt, tz)
    sh = data[256:256 + ns * 256].decode("ascii", "replace")
    labels = [sh[i * 16:(i + 1) * 16].strip() for i in range(ns)]
    off = ns * (16 + 80 + 8 + 8 + 8 + 8 + 8 + 80)
    n_samp = [int(sh[off + i * 8: off + (i + 1) * 8].strip()) for i in range(ns)]
    if "EDF Annotations" not in labels:
        raise ValueError("EDF file has no annotation signal")
    rec_bytes = sum(n_samp) * 2
    ann = labels.index("EDF Annotations")
    a_off = sum(n_samp[:ann]) * 2
    a_len = n_samp[ann] * 2
    anns: List[Tuple[float, Optional[float], str]] = []
    if n_records < 0:
        n_records = (len(data) - header_bytes) // max(1, rec_bytes)
    for r in range(n_records):
        base = header_bytes + r * rec_bytes + a_off
        block = data[base:base + a_len]
        for tal in block.split(b"\x00"):
            if not tal or not tal.startswith((b"+", b"-")):
                continue
            parts = tal.split(b"\x14")
            head = parts[0].split(b"\x15")
            try:
                onset = float(head[0])
                dur = float(head[1]) if len(head) > 1 and head[1] else None
            except ValueError:
                continue
            for txt in parts[1:]:
                txt = txt.decode("utf-8", "replace").strip()
                if txt:
                    anns.append((onset, dur, txt))
    rows, other = [], 0
    for onset, dur, txt in anns:
        st = map_stage(txt, "rk")
        if st is None:
            other += 1
            continue
        rows.append((start_unix + onset, dur, st, txt))
    if not rows:
        raise ValueError("no sleep-stage annotations in the EDF file")
    if other:
        warnings.append(f"{other} non-stage annotation(s) ignored")
    return Hypnogram(epochs=_expand(rows, epoch_s, warnings), epoch_s=epoch_s, fmt="edf+",
                     warnings=warnings)


# --------------------------------------------------------------------------- BIDSleep .mat
def _parse_mat(data: bytes, *, tz, epoch_s, warnings) -> Hypnogram:
    """BIDSleep ``labels.mat`` via the loader in ``scripts/bidsleep_reduce.py``. Its
    ``choose_labels`` already prefers expert scoring over the automated headband and maps codes
    to the R&K-style trainer codes (5 = REM, -1 = unscored)."""
    import importlib.util
    import tempfile

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "..", "scripts", "bidsleep_reduce.py"))
    spec = importlib.util.spec_from_file_location("_bidsleep_reduce", path)
    if spec is None or spec.loader is None:
        raise ValueError(".mat import needs scripts/bidsleep_reduce.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.NamedTemporaryFile(suffix=".mat", delete=False) as fh:
        fh.write(data)
        tmp = fh.name
    try:
        lab = mod.load_labels(tmp)
    finally:
        os.unlink(tmp)
    codes, src = mod.choose_labels(lab)
    if not codes:
        raise ValueError("labels.mat holds no scored epochs")
    dt = _parse_datetime(lab["recStart"])
    if dt is None:
        raise ValueError(f"unreadable recStart {lab['recStart']!r}")
    t0 = _instant(dt, tz)
    rows = [(t0 + k * epoch_s, epoch_s, map_stage(c, "rk") or UNKNOWN, str(c))
            for k, c in enumerate(codes)]
    return Hypnogram(epochs=_expand(rows, epoch_s, warnings), epoch_s=epoch_s,
                     fmt=f"bidsleep-mat ({src})", device="dreem" if src == "dreem" else None,
                     warnings=warnings)


# --------------------------------------------------------------------------- entry point
def parse_hypnogram(data, *, filename: str = "", fmt: str = "auto", start=None, tz=None,
                    night_date: Optional[str] = None, epoch_s: float = EPOCH_S,
                    numeric_scheme: str = "auto") -> Hypnogram:
    """Parse an exported hypnogram (bytes or text) into 30 s epochs on real instants.

    ``fmt``: ``auto`` | ``csv`` | ``json`` | ``edf`` | ``mat``. ``start`` anchors an epoch list
    or relative offsets; ``night_date`` anchors bare times of day; ``tz`` is the zone naive
    times are wall clock in (default: this machine's). Raises ValueError with a readable
    message when the file cannot be understood."""
    tz = resolve_tz(tz)
    warnings: List[str] = []
    raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
    name = (filename or "").lower()
    f = (fmt or "auto").lower()
    if f == "auto":
        if name.endswith((".edf", ".bdf", ".rec")) or (raw[:8].strip() == b"0" and len(raw) >= 256
                                                        and b"EDF" in raw[192:236]):
            f = "edf"
        elif name.endswith(".mat") or raw[:6] == b"MATLAB":
            f = "mat"
        elif raw.lstrip()[:1] in (b"{", b"["):
            f = "json"
        else:
            f = "csv"
    if f == "edf":
        h = _parse_edf(bytes(raw), tz=tz, epoch_s=epoch_s, warnings=warnings)
    elif f == "mat":
        h = _parse_mat(bytes(raw), tz=tz, epoch_s=epoch_s, warnings=warnings)
    else:
        try:
            text = bytes(raw).decode("utf-8-sig")
        except UnicodeDecodeError:
            text = bytes(raw).decode("latin-1")
        if f == "json":
            try:
                obj = json.loads(text)
            except ValueError as exc:
                raise ValueError(f"invalid JSON: {exc}") from exc
            h = _parse_json(obj, start=start, tz=tz, night_date=night_date, epoch_s=epoch_s,
                            numeric_scheme=numeric_scheme, warnings=warnings)
        else:
            h = _parse_delimited(text, start=start, tz=tz, night_date=night_date,
                                 epoch_s=epoch_s, numeric_scheme=numeric_scheme,
                                 warnings=warnings)
    if not h.epochs:
        raise ValueError("no epochs found")
    if h.scored == 0:
        raise ValueError("no scored (wake/light/deep/rem) epochs found")
    return h


# --------------------------------------------------------------------------- storage
def local_naive(unix: float) -> str:
    """The naive-LOCAL ISO rendering the engine tables use (see ``sleepctl.storage.schema``)."""
    return datetime.fromtimestamp(float(unix)).replace(microsecond=0).isoformat()


def infer_night_date(conn, first_unix: float, last_unix: float) -> str:
    """The controller night these epochs belong to: the ``night_date`` most of the overlapping
    ``raw_samples`` rows carry (a day sleeper's night is not the noon-cutoff date of its first
    epoch), else the noon cutoff of the first epoch."""
    lo, hi = local_naive(first_unix), local_naive(last_unix)
    try:
        row = conn.execute(
            "SELECT night_date, COUNT(*) AS n FROM raw_samples WHERE ts >= ? AND ts <= ? "
            "AND night_date IS NOT NULL GROUP BY night_date ORDER BY n DESC LIMIT 1",
            (lo, hi)).fetchone()
        if row is not None and row[0]:
            return str(row[0])
    except Exception:
        pass
    return (datetime.fromtimestamp(first_unix) - timedelta(hours=12)).date().isoformat()


def store_hypnogram(conn, hyp: Hypnogram, *, night_date: Optional[str] = None,
                    source: Optional[str] = None) -> dict:
    """Replace ``night_date``'s imported epochs with ``hyp``'s; returns an import summary."""
    first, last = hyp.span()
    nd = night_date or infer_night_date(conn, first, last)
    src = (source or hyp.device or "eeg_headband").strip()[:80]
    now = datetime.now().replace(microsecond=0).isoformat()
    conn.execute("DELETE FROM eeg_hypnogram WHERE night_date = ?", (nd,))
    conn.executemany(
        "INSERT OR REPLACE INTO eeg_hypnogram (night_date, epoch_unix, epoch_ts, epoch_s, stage,"
        " raw_stage, source, imported_ts) VALUES (?,?,?,?,?,?,?,?)",
        [(nd, float(t), local_naive(t), float(hyp.epoch_s), st, raw[:40], src, now)
         for t, st, raw in hyp.epochs])
    conn.commit()
    minutes = {c: 0.0 for c in CLASSES + (UNKNOWN,)}
    for _, st, _ in hyp.epochs:
        minutes[st] = minutes.get(st, 0.0) + hyp.epoch_s / 60.0
    return {"night_date": nd, "source": src, "format": hyp.fmt, "epochs": len(hyp.epochs),
            "scored_epochs": hyp.scored, "epoch_s": hyp.epoch_s,
            "start": local_naive(first), "end": local_naive(last),
            "start_utc": datetime.fromtimestamp(first, timezone.utc).isoformat(),
            "minutes": {k: round(v, 1) for k, v in minutes.items()},
            "warnings": list(hyp.warnings)}


def import_hypnogram(conn, data, *, night_date: Optional[str] = None,
                     source: Optional[str] = None, **kw) -> dict:
    """Parse + store in one step (``kw`` as :func:`parse_hypnogram`)."""
    hyp = parse_hypnogram(data, night_date=night_date, **kw)
    return store_hypnogram(conn, hyp, night_date=night_date, source=source)


def load_epochs(conn, night_date: str) -> List[Tuple[float, float, str]]:
    """``(epoch_unix, epoch_s, stage)`` for a night, ascending."""
    try:
        rows = conn.execute(
            "SELECT epoch_unix, epoch_s, stage FROM eeg_hypnogram WHERE night_date = ? "
            "ORDER BY epoch_unix ASC", (night_date,)).fetchall()
    except Exception:
        return []
    return [(float(r[0]), float(r[1] or EPOCH_S), str(r[2])) for r in rows]


def imported_nights(conn) -> List[dict]:
    """Every imported night, newest first: date, source, epoch counts."""
    try:
        rows = conn.execute(
            "SELECT night_date, MIN(source), COUNT(*), "
            "SUM(CASE WHEN stage IN ('awake','light','deep','rem') THEN 1 ELSE 0 END), "
            "MIN(epoch_ts), MAX(epoch_ts), MAX(imported_ts) "
            "FROM eeg_hypnogram GROUP BY night_date ORDER BY night_date DESC").fetchall()
    except Exception:
        return []
    return [{"night_date": r[0], "source": r[1], "epochs": r[2], "scored_epochs": r[3],
             "start": r[4], "end": r[5], "imported_ts": r[6]} for r in rows]


def delete_night(conn, night_date: str) -> int:
    cur = conn.execute("DELETE FROM eeg_hypnogram WHERE night_date = ?", (night_date,))
    conn.commit()
    return cur.rowcount or 0


__all__ = ["Hypnogram", "map_stage", "parse_hypnogram", "store_hypnogram", "import_hypnogram",
           "load_epochs", "imported_nights", "infer_night_date", "delete_night", "local_naive",
           "CLASSES", "EPOCH_S"]
