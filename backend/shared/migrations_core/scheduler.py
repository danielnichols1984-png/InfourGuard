"""A lightweight, dependency-free scheduler: an asyncio loop, started once
alongside the host app, that polls for MigrationJobs whose `scheduled_at`
has arrived and kicks off their full run.

No new infrastructure (no Celery, no APScheduler, no Redis) — consistent
with this module's "background task in the same process" execution model.
The tradeoff is the same one already documented for that model: this only
works while the app process itself is running, and a job "scheduled" while
the server happens to be down simply runs whenever it next comes up and
polls past that time, not necessarily exactly on time.
"""
import asyncio
import threading
from datetime import datetime, timezone

from shared.migrations_core.db import SessionLocal
from shared.migrations_core.models import MigrationJob
from shared.migrations_core.service import run_full_migration

POLL_INTERVAL_SECONDS = 30


def _run_due_jobs() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        due_jobs = (
            db.query(MigrationJob)
            .filter(MigrationJob.status == "scheduled", MigrationJob.scheduled_at <= now)
            .all()
        )
        for job in due_jobs:
            # Claim it immediately so the next poll (30s later) doesn't
            # trigger it again while this run is still getting started.
            job.status = "running"
            db.commit()
            threading.Thread(target=run_full_migration, args=(job.id,), daemon=True).start()
    finally:
        db.close()


async def scheduler_loop() -> None:
    while True:
        try:
            _run_due_jobs()
        except Exception as e:
            print(f"[migrations_core scheduler] error checking scheduled jobs: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
