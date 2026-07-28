"""The silent-failure ledger: what QUIETLY stopped working, and how often.

The control loop is defensive by design — nearly every optional subsystem is wrapped so a failure
degrades that feature instead of killing the night. That is the right call, and it has a cost: a
subsystem can fail on every single tick for eight hours and the only trace is a line on stdout, in
a log the watchdog overwrites on the next restart. Nothing reaches ``/diag``, nothing reaches the
published health snapshot, and the verdict stays HEALTHY — because from the loop's point of view
nothing IS broken. It just isn't doing the thing.

That is the worst failure mode this system has: not a crash, which is loud and self-announcing, but
a feature that is quietly absent while every indicator says fine. The stage estimator throwing every
tick looks exactly like a healthy night with an unremarkable trace.

So: record every swallowed failure, keyed by subsystem, and persist it where a remote reader can
see it. Rate-limited per subsystem so a per-tick failure writes a handful of rows a night rather
than thousands, while still reporting the true count.

Never raises. This runs inside the ``except`` blocks that exist to keep the night alive; a bug here
must not be the thing that ends it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

EVENT_CATEGORY = "degradation"

# Persist at most one row per subsystem per this interval. The in-memory counter keeps counting, so
# the recorded row still carries the TRUE occurrence count -- this only bounds write volume.
PERSIST_INTERVAL_S = 300.0

# A subsystem that has failed at least this many times in the reporting window is called out
# individually rather than folded into the summary line.
NOTABLE_COUNT = 3


@dataclass
class _Entry:
    subsystem: str
    count: int = 0
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    last_error: str = ""
    _last_persist_mono: float = field(default=0.0, repr=False)


class DegradationLedger:
    """Process-wide tally of swallowed subsystem failures."""

    def __init__(self) -> None:
        self._entries: Dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def record(self, subsystem: str, exc: BaseException | str, repo=None) -> None:
        """Note that ``subsystem`` failed. Persists a row at most once per PERSIST_INTERVAL_S."""
        try:
            err = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
            # Coerce the key to a string. ``snapshot()`` sorts the entries, and sorting a dict
            # whose keys mix None (or anything else non-str) with strings raises TypeError --
            # which its own defensive except would swallow into an EMPTY snapshot, silently
            # discarding every subsystem failure recorded that night. A ledger that loses its
            # contents because one caller passed a None name is worse than no ledger, since the
            # whole point is to make silent failures visible.
            subsystem = subsystem if isinstance(subsystem, str) else str(subsystem)
            now = datetime.now()
            mono = time.monotonic()
            with self._lock:
                e = self._entries.get(subsystem)
                if e is None:
                    e = _Entry(subsystem=subsystem, first_ts=now)
                    self._entries[subsystem] = e
                e.count += 1
                e.last_ts = now
                e.last_error = err[:500]
                # Persist on TIME (bounded volume) or when the count crosses a power of ten.
                # Time alone would leave a subsystem that failed 200 times in the first minute
                # recorded as "1x" — under-reporting the severity to exactly the remote reader
                # who can't see the process. The escalation ladder keeps the persisted count
                # within an order of magnitude of the truth for a handful of extra rows.
                due = ((mono - e._last_persist_mono) >= PERSIST_INTERVAL_S
                       or e.count in (1, 10, 100, 1000, 10000))
                if due:
                    e._last_persist_mono = mono
                count, first_ts = e.count, e.first_ts
            if repo is not None and due:
                repo.log_event(
                    EVENT_CATEGORY, "warn", subsystem,
                    f"{subsystem} failed and was skipped ({count}x since "
                    f"{first_ts.isoformat() if first_ts else '?'}): {err[:300]}",
                    {"subsystem": subsystem, "count": count, "error": err[:300]},
                )
        except Exception:
            pass

    def snapshot(self) -> dict:
        """In-process tallies, for the runtime_state payload."""
        try:
            with self._lock:
                return {
                    name: {
                        "count": e.count,
                        "first": e.first_ts.isoformat() if e.first_ts else None,
                        "last": e.last_ts.isoformat() if e.last_ts else None,
                        "error": e.last_error,
                    }
                    for name, e in sorted(self._entries.items())
                }
        except Exception:
            return {}

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()


# The daemon holds one; module-level so any helper can reach it without threading it through.
LEDGER = DegradationLedger()


def record(subsystem: str, exc: BaseException | str, repo=None) -> None:
    LEDGER.record(subsystem, exc, repo=repo)


def recent(repo, hours: float = 24.0) -> dict:
    """Aggregate persisted degradation events into ``{subsystem: {count, last_error, last_ts}}``.

    Reads the PERSISTED count from each row's payload rather than counting rows, because writes are
    rate-limited — counting rows would report a per-tick failure as a handful of incidents."""
    out: dict = {}
    try:
        since = (datetime.now() - timedelta(hours=float(hours))).isoformat()
        rows = repo.recent_events(limit=500, category=EVENT_CATEGORY, since_iso=since)
    except Exception:
        return out
    for r in rows or []:
        try:
            data = r.get("data") or {}
            if isinstance(data, str):
                import json

                data = json.loads(data)
            name = data.get("subsystem") or r.get("code") or "unknown"
            count = int(data.get("count") or 1)
            prev = out.get(name)
            # recent_events is newest-first, so the FIRST row seen for a subsystem carries the
            # highest cumulative count and the most recent error.
            if prev is None:
                out[name] = {"count": count, "last_error": data.get("error") or r.get("message"),
                             "last_ts": r.get("ts")}
            else:
                prev["count"] = max(prev["count"], count)
        except Exception:
            continue
    return out


def summarize(agg: dict) -> tuple[str, str]:
    """``(status, detail)`` for a diagnostics check. ``ok`` when nothing has been skipped."""
    if not agg:
        return "ok", "no subsystem failures recorded"
    notable = {k: v for k, v in agg.items() if v.get("count", 0) >= NOTABLE_COUNT}
    total = sum(v.get("count", 0) for v in agg.values())
    if not notable:
        names = ", ".join(sorted(agg))
        return "info", f"{total} isolated subsystem skip(s): {names}"
    parts = [f"{k} ({v['count']}x)" for k, v in sorted(notable.items(),
                                                       key=lambda kv: -kv[1]["count"])]
    worst = max(notable.values(), key=lambda v: v.get("count", 0))
    detail = (f"{', '.join(parts)} — these features were NOT running. "
              f"Last error: {worst.get('last_error')}")
    return "warn", detail
