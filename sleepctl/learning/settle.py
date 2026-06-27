"""Learn the maintenance settle-nudge sign/magnitude from measured prevention.

The literature is split on direction — cutaneous warming can suppress awakenings (Raymann
2008) while distal cooling drives alertness (Fronczek 2008) — so the right move is learned per
phenotype. We don't yet log the *direction* of each pre-cool, so this is a conservative
revealed-preference rule on the pre-cool efficacy ledger: if cooling pre-empts are preventing
awakenings, keep the evidence-default cool direction; if they consistently fail, explore the
opposite (smaller) direction. Always bounded by the comfort cap.
"""

from __future__ import annotations


def learn_settle_nudge(repo, cfg, min_events: int = 6) -> float:
    base = cfg.tunables.maintenance_settle_nudge_f
    cap = cfg.tunables.maintenance_settle_cap_f
    try:
        eff = repo.precool_efficacy() or {}
    except Exception:
        return base
    n = sum(int(v.get("n", 0) or 0) for v in eff.values())
    prevented = sum(int(v.get("prevented", 0) or 0) for v in eff.values())
    if n < min_events:
        return base                       # not enough evidence -> evidence-default
    rate = prevented / n if n else 0.0
    if rate >= 0.6:
        nudge = base                      # working well: keep the default direction
    elif rate <= 0.35:
        nudge = -base * 0.5               # failing: explore the opposite, smaller
    else:
        nudge = base * 0.7                # marginal: soften
    return max(-cap, min(cap, nudge))
