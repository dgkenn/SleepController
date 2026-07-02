"""Black-box flight-recorder: a rolling in-memory ring buffer of recent daemon ticks, dumped to
disk on crash or clean shutdown so the lead-up to a failure is never lost.

Pure in-memory + file I/O, no DB dependency -- it must keep working even if the SQLite
connection itself is the thing that's wedged (the same reason ``bridge.write_heartbeat`` writes
a plain file instead of a DB row). Both daemons (``dashboard/daemon/run_daemon.py`` sync sim,
``dashboard/daemon/live_daemon.py`` async live) own one instance each and ``record()`` a
summary of every tick.

Files land in the same ``.run`` directory as the daemon logs/heartbeats:
  - ``blackbox-<crash-ts>.jsonl``  -- dumped when the tick loop hits an unhandled/tick error
  - ``blackbox-latest.jsonl``      -- dumped on every clean shutdown (overwritten each time)
The most recent ``keep`` crash dumps are retained; older ones are pruned automatically.
"""

from __future__ import annotations

import glob
import json
import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional

_CRASH_GLOB = "blackbox-2*.jsonl"   # 'blackbox-2...' -> won't match 'blackbox-latest.jsonl'
_LATEST_NAME = "blackbox-latest.jsonl"


class BlackBoxRecorder:
    """Fixed-size ring buffer of per-tick summaries + on-demand JSONL dumps."""

    def __init__(self, run_dir: str, maxlen: int = 200, keep: int = 5) -> None:
        self.run_dir = run_dir
        self.keep = keep
        self._buf: deque = deque(maxlen=maxlen)

    def record(self, entry: dict) -> None:
        """Append one tick's summary. Best-effort: a bad entry must never break the tick loop
        that's recording it."""
        try:
            e = dict(entry)
            e.setdefault("ts", datetime.now(timezone.utc).isoformat())
            self._buf.append(e)
        except Exception:
            pass

    def _dump(self, path: str) -> Optional[str]:
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                for e in self._buf:
                    fh.write(json.dumps(e, default=str) + "\n")
            return path
        except Exception:
            return None

    def dump_crash(self, now: Optional[datetime] = None) -> Optional[str]:
        """Dump the buffer to a timestamped crash file and prune old ones. Best-effort."""
        ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
        path = self._dump(os.path.join(self.run_dir, f"blackbox-{ts}.jsonl"))
        self._prune()
        return path

    def dump_latest(self) -> Optional[str]:
        """Dump the buffer to the fixed clean-shutdown file (overwritten each call)."""
        return self._dump(os.path.join(self.run_dir, _LATEST_NAME))

    def _prune(self) -> int:
        try:
            files = sorted(glob.glob(os.path.join(self.run_dir, _CRASH_GLOB)))
            excess = files[:-self.keep] if len(files) > self.keep else []
            removed = 0
            for f in excess:
                try:
                    os.remove(f)
                    removed += 1
                except OSError:
                    pass
            return removed
        except Exception:
            return 0


def latest_blackbox_path(run_dir: str) -> Optional[str]:
    """The most recently written blackbox dump (crash dump if any exist, else the
    clean-shutdown dump), or None if neither exists yet."""
    crashes = sorted(glob.glob(os.path.join(run_dir, _CRASH_GLOB)))
    if crashes:
        return crashes[-1]
    latest = os.path.join(run_dir, _LATEST_NAME)
    return latest if os.path.exists(latest) else None
