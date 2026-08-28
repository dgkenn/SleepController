"""iPhone Screen Time PICKUPS as coarse, independent wake evidence.

WHY PICKUPS AND NOT SCREEN TIME USAGE
-------------------------------------
Screen Time's headline charts measure APP USAGE, which requires unlocking and opening something.
This user's actual overnight behaviour is pressing the side button to read the lock-screen clock
and putting the phone down -- no unlock, no app, so no usage. Those charts correctly show a flat
line across the night and say nothing about awakenings.

PICKUPS are a different metric: a pickup is recorded whenever the device wakes from idle,
INCLUDING waking the screen without unlocking. That is exactly the behaviour in question, which
makes it the one Screen Time figure that carries information here.

WHAT IT CAN AND CANNOT SHOW
---------------------------
Pickups are bucketed by HOUR, so this cannot score epochs. What it can do is answer a blunt and
still-useful question: did we call an entire hour asleep during which the phone was demonstrably
picked up? That is a miss, and it needs no recall and no cooperation from the user beyond a
screenshot.

Deliberately one-directional, for the same reason `wake_anchors` is: a pickup proves wake, but
the ABSENCE of a pickup proves nothing -- most awakenings do not involve reaching for the phone.
So this measures our misses and is silent about our false alarms, and it must not be read as an
accuracy figure.

Its resolution is also its weakness and is stated rather than hidden: a pickup at 03:59 and an
awakening at 03:05 share a bucket. So an hour scored "caught" may have been caught for the wrong
minute. It is a screening test -- cheap, independent, and unable to be gamed by the model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

#: Labels meaning the estimator thought the user was asleep.
_ASLEEP = ("light", "deep", "rem")


def _hour_of(ts) -> Optional[int]:
    try:
        return datetime.fromisoformat(str(ts)).hour
    except Exception:
        return None


def evaluate_pickups(rows: Sequence[dict], pickups_by_hour: Dict[int, int],
                     night_hours: Optional[Sequence[int]] = None) -> Dict[str, object]:
    """Compare hourly pickup counts against what we called those hours.

    ``rows`` are the night's samples (each needs ``ts`` and ``stage``). ``pickups_by_hour`` maps
    hour-of-day (0-23) to a pickup count, read off the Screen Time chart.

    Only hours we actually STAGED are judged: an hour with no labelled samples is a sensor gap,
    not a miss, and scoring it either way would be inventing a result.
    """
    staged: Dict[int, List[str]] = {}
    for r in rows:
        h = _hour_of(r.get("ts"))
        st = str(r.get("stage") or "")
        if h is None or st in ("", "None", "unknown"):
            continue
        staged.setdefault(h, []).append(st)

    hours = sorted(h for h, n in pickups_by_hour.items() if n and (
        night_hours is None or h in set(night_hours)))
    caught: List[int] = []
    missed: List[int] = []
    unstaged: List[int] = []
    for h in hours:
        labels = staged.get(h)
        if not labels:
            unstaged.append(h)
            continue
        (caught if any(s == "awake" for s in labels) else missed).append(h)

    judged = len(caught) + len(missed)
    return {
        "hours_with_pickups": hours,
        "caught_hours": caught,
        "missed_hours": missed,
        "unstaged_hours": unstaged,
        "n_judged": judged,
        # P(we called the whole hour asleep | the phone was demonstrably picked up in it)
        "miss_rate": round(len(missed) / judged, 3) if judged else None,
        "labels_in_missed_hours": {
            h: sorted(set(staged.get(h, []))) for h in missed
        },
    }


def format_report(res: Dict[str, object], label: str = "") -> str:
    lines = [f"SCREEN TIME PICKUPS{f' - {label}' if label else ''}"]
    hours = res.get("hours_with_pickups") or []
    if not hours:
        lines.append("  no overnight pickups recorded -- uninformative, not reassuring")
        return "\n".join(lines)
    lines.append(f"  hours containing a pickup: {', '.join(f'{h:02d}' for h in hours)}")
    if res.get("unstaged_hours"):
        lines.append(f"  not staged (sensor gap, not judged): "
                     f"{', '.join(f'{h:02d}' for h in res['unstaged_hours'])}")
    lines.append(f"  we labelled SOME of that hour awake: "
                 f"{', '.join(f'{h:02d}' for h in res.get('caught_hours') or []) or 'none'}")
    lines.append(f"  we called the WHOLE hour asleep (a miss): "
                 f"{', '.join(f'{h:02d}' for h in res.get('missed_hours') or []) or 'none'}")
    if res.get("miss_rate") is not None:
        lines.append(f"  miss rate over {res['n_judged']} judged hour(s): {res['miss_rate']}")
    for h, labs in (res.get("labels_in_missed_hours") or {}).items():
        lines.append(f"    hour {h:02d} was labelled: {', '.join(labs)}")
    lines.append("  NOTE: hour resolution, and one-directional -- a pickup proves wake, but its")
    lines.append("  absence proves nothing, so this measures misses only and is not an accuracy.")
    return "\n".join(lines)
