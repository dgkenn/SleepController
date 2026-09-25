"""Where the night-time neutral sits: the comfort sweep's reading, moved by what the sleeper says.

The comfort sweep (2026-08-28) measured a neutral of 69.0 F. It was measured AWAKE, lying on
the bed for a few minutes per step, and the bed has no surface sensor, so nothing since has
been able to check it against a whole night asleep. The user's own reports have: cold at
68 F, cold again at 69 F, a manual rescue to 80 F on 2026-09-19, and "the bed wakes me up in
the middle of the night because it's so cold" on 2026-09-22. The pooled record agrees. Over
every commanded maintenance tick, awakenings run 2.4 per 100 ticks at 69 F and 0.7 at 70 F.

So the neutral the controller steers around is the measured one plus two terms:

  * a fixed re-anchor (``comfort_neutral_offset_f``), the correction the evidence above
    already supports; and
  * a closed loop on the morning review's temperature answer. Every "too cold" moves the
    anchor up a degree, every "a bit cold" half a degree, "just right" holds it, and the warm
    answers move it back down. It is an integrator, clipped at every step, so a run of "just
    right" mornings keeps the warmth that got them there instead of drifting back.

Mornings with no review fall back to the note, at half weight: "cold" / "freezing" read as a
bit cold, "too hot" / "sweating" as a bit warm.

The cold side is bounded by the measurement itself: the anchor never goes below the neutral
the sweep measured, and ``maintenance_floor_f`` still holds under everything. The warm side is
bounded by ``comfort_feedback_max_f``. Every term is reported so the move is inspectable.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

#: Degrees F each morning answer moves the anchor.
REVIEW_STEP_F = {"too_cold": 1.0, "bit_cold": 0.5, "right": 0.0,
                 "bit_warm": -0.5, "too_warm": -1.0}
#: A note is a weaker signal than an explicit answer.
NOTE_WEIGHT = 0.5
# There is deliberately NO rolling lookback window. It used to be 60 nights, and because this
# is an integrator that meant earned warmth silently fell off the back: 3 "too cold" mornings
# from 2026-09-22 followed by 87 "just right" ones read neutral 73.0 on 2026-11-21 and 70.0 on
# 2026-11-25 (audit 2026-09-25) -- the bed went three degrees colder with no complaint at all,
# exactly the drift "just right holds it" promises not to do. The integral runs from
# ``comfort_feedback_since``; moving that date is the way to start it over.
#: Default first night the loop counts. Everything said before it is already priced into the
#: fixed re-anchor; counting it again would move the bed twice for one complaint.
FEEDBACK_SINCE = "2026-09-22"

_COLD = re.compile(r"\b(cold|colder|freezing|froze|frozen|chilly|shiver\w*)\b", re.I)
_WARM = re.compile(r"\b(too (hot|warm)|so (hot|warm)|overheat\w*|sweat\w*|boiling)\b", re.I)
#: "not cold", "wasn't too hot" -- a negated mention is not a complaint.
_NEGATED = re.compile(r"(?:\bnot|n't|\bnever|\bno longer)\s+(?:too |so |very )?"
                      r"(cold|colder|freezing|chilly|hot|warm|sweat\w*)\b", re.I)


def note_vote(text: str) -> Optional[str]:
    """``bit_cold`` / ``bit_warm`` / None for a free-text morning note."""
    t = _NEGATED.sub(" ", text or "")
    cold, warm = bool(_COLD.search(t)), bool(_WARM.search(t))
    if cold == warm:
        return None                       # neither, or both ("cold then too hot"): no vote
    return "bit_cold" if cold else "bit_warm"


def _night_votes(repo, now: datetime,
                 first_night: str = FEEDBACK_SINCE) -> List[Tuple[str, str, float]]:
    """``(night_date, source, step_f)`` oldest first: one per night, review over note."""
    since = first_night or ""
    # The night ``now`` falls in, by the noon cutoff every other night key uses. A note may only
    # credit a night that has already ENDED (strictly before this one): tonight has not been
    # slept, so nothing written about it yet can be a verdict on it.
    current_night = (now.date() if now.hour >= 12 else now.date() - timedelta(days=1)).isoformat()
    votes: Dict[str, Tuple[str, float]] = {}
    try:
        rows = repo.conn.execute(
            "SELECT night_date, temperature FROM wake_review WHERE night_date >= ? "
            "ORDER BY night_date ASC", (since,)).fetchall()
    except Exception:
        rows = []
    for night, temp in rows:
        step = REVIEW_STEP_F.get(str(temp or ""))
        if step is not None:
            votes[str(night)] = ("review", step)
    try:
        notes = repo.conn.execute(
            "SELECT date, text FROM notes WHERE date >= ? ORDER BY id ASC", (since,)).fetchall()
    except Exception:
        notes = []
    # One vote per note DATE, not per note. 2026-09-25 audit: two "cold" notes written the same
    # morning each took a night -- the first the night before, the second spilled onto that
    # evening's night, which had not been slept yet -- so one cold morning warmed the bed twice.
    # All of a day's notes are read together: they agree, or (cold and warm both) they are no vote.
    by_date: Dict[object, set] = {}
    for date, text in notes:
        vote = note_vote(str(text or ""))
        if vote is None:
            continue
        try:
            d = datetime.fromisoformat(str(date)).date()
        except Exception:
            continue
        by_date.setdefault(d, set()).add(vote)
    for d in sorted(by_date):
        kinds = by_date[d]
        if len(kinds) != 1:
            continue
        vote = next(iter(kinds))
        # A note filed the morning after belongs to the night before; the evening's own date
        # is the night itself. Prefer the night before; a night that already has a review is
        # answered, and the note is not moved onto a night it may not describe.
        for night in ((d - timedelta(days=1)).isoformat(), d.isoformat()):
            if night >= current_night:
                break                                  # not slept yet: not this note's night
            if night in votes and votes[night][0] == "review":
                break                                  # the explicit answer wins
            if night not in votes:
                votes[night] = ("note", REVIEW_STEP_F[vote] * NOTE_WEIGHT)
                break
    return [(n, src, step) for n, (src, step) in sorted(votes.items()) if n >= since]


def comfort_anchor(repo, cfg, measured_neutral_f: float,
                   now: Optional[datetime] = None) -> Dict[str, object]:
    """The neutral tonight should steer around, with every term that produced it."""
    now = now or datetime.now()
    t = cfg.tunables
    base = float(getattr(t, "comfort_neutral_offset_f", 0.0) or 0.0)
    hi = float(getattr(t, "comfort_feedback_max_f", 4.0) or 0.0)
    lo = min(0.0, base)            # never colder than the neutral the sweep measured
    offset = max(lo, min(hi, base))
    votes = (_night_votes(repo, now, str(getattr(t, "comfort_feedback_since", FEEDBACK_SINCE)))
             if getattr(t, "comfort_feedback_enabled", True) else [])
    for _night, _src, step in votes:
        offset = max(lo, min(hi, offset + step))       # clip every step: no wind-up
    return {
        "measured_neutral_f": round(float(measured_neutral_f), 2),
        "base_offset_f": base,
        "feedback_f": round(offset - base, 2),
        "offset_f": round(offset, 2),
        "neutral_f": round(float(measured_neutral_f) + offset, 2),
        "n_reviews": sum(1 for v in votes if v[1] == "review"),
        "n_notes": sum(1 for v in votes if v[1] == "note"),
        "last_vote": ({"night_date": votes[-1][0], "source": votes[-1][1],
                       "step_f": votes[-1][2]} if votes else None),
    }


def shifted_profile(comfort: Optional[dict], offset_f: float) -> Optional[dict]:
    """The comfort band moved with its neutral, so the clamp, the guardrail and the cold-dwell
    check all bound the same night the neutral now describes. The source is kept and the shift
    recorded, so nothing downstream mistakes this for a fresh sweep."""
    if not isinstance(comfort, dict):
        return comfort
    out = dict(comfort)
    for k in ("neutral_f", "cool_edge_f", "warm_edge_f"):
        if out.get(k) is not None:
            out[k] = round(float(out[k]) + float(offset_f), 2)
    out["anchor_offset_f"] = round(float(offset_f), 2)
    return out
