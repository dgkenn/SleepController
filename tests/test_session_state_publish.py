"""Only a session a restart would DAMAGE may hold back a deploy.

2026-09-21: a WAKE_RECOVERY that failed to end -- the band had been on its charger since
05:28 -- held the day's deploy for over ten hours, including the accelerometer fix that the
previous night's data loss had been waiting for. The watchdog deferred because the state was
not "idle"; the reason the deferral exists only ever covered induction.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_watchdog_defers_on_the_flag_not_on_being_non_idle():
    src = (ROOT / "scripts" / "windows-watchdog.ps1").read_text(encoding="utf-8", errors="replace")
    i = src.index('$sessFile = Join-Path $run "session.state"')
    block = src[i:i + 900]
    assert "-match 'protect'" in block, "the watchdog still defers on any non-idle state"
    assert '$sess -ne "idle"' not in block
    assert "TotalHours -lt 3" in block, "the safety bound is still 12 h"
