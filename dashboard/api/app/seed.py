"""Seed the shared DB with demo nights so the dashboard has content without a Pod.

Idempotent-ish: safe to run on an empty DB. Uses the engine's NightlyUpdater + simulator
summaries so analytics, learning, and trends are populated realistically.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.loop.nightly import NightlyUpdater
from sleepctl.models import ContextRecord, NightSummary

from app.bridge import write_runtime_state
from app.db import get_repo


def seed(nights: int = 21) -> None:
    cfg = AppConfig.default()
    repo = get_repo()
    try:
        if repo.all_nights():
            return  # already seeded
        updater = NightlyUpdater(cfg, repo)
        rng = random.Random(7)
        base = datetime.now() - timedelta(days=nights)
        for i in range(nights):
            d = (base + timedelta(days=i)).date().isoformat()
            short = i % 7 == 6
            repo.save_context(ContextRecord(
                date=d, is_short_sleep_day=short,
                sleep_opportunity_min=300 if short else 480,
                caffeine=(i % 3 == 0), late_night_work=(i % 4 == 0)))
            deep = rng.randint(85, 125)
            updater.run(NightSummary(
                date=d, total_sleep_min=(330 if short else rng.randint(440, 490)),
                deep_min=deep, rem_min=rng.randint(95, 125),
                light_min=200, wake_events=rng.randint(0, 3),
                waso_min=rng.randint(8, 30), sleep_efficiency=round(rng.uniform(0.84, 0.95), 3),
                avg_hr=rng.randint(50, 56), avg_hrv=rng.randint(55, 75),
                avg_respiratory_rate=13, sleep_onset_latency_min=rng.randint(8, 22)))
        # a current live snapshot so Home/Status renders immediately
        write_runtime_state(repo.conn, {
            "state": "IDLE", "objective": "OPTIMIZE", "mode": "auto",
            "target_temp_f": 68.0, "bed_temp_f": 70.2, "room_temp_f": 68.0,
            "stage": "unknown", "confidence": 0.8, "target_level": -58,
            "daemon_alive": True, "extra": {}})
        print(f"seeded {nights} nights")
    finally:
        repo.close()


if __name__ == "__main__":
    seed()
