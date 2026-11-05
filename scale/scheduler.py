"""Scheduler / dialer: bulk-read due EMIs from the DB and publish one call job each.

Each job carries everything the worker needs (name, phone, amount), so workers never query
the DB just to learn who to call. job_id is deterministic (emi + day) so running the scheduler
twice cannot create two different jobs for the same EMI on the same day.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

from .db import Db
from .queues import QueueBackend

log = logging.getLogger("scale.scheduler")


def make_job(e, day: str) -> dict:
    return {
        "job_id": f"{e.emi_id}:{day}",
        "emi_id": e.emi_id,
        "borrower_id": e.borrower_id,
        "name": e.name,
        "phone": e.phone,
        "amount": e.amount,
        "due_date": e.due_date,
        "scheduled_at": time.time(),
    }


async def schedule_due(
    db: Db,
    queue: QueueBackend,
    topic: str,
    *,
    today: date,
    lookback_days: int = 3,
    batch_size: int = 1000,
    limit: int | None = None,
) -> int:
    """Publish a job for every unpaid EMI due within [today-lookback, today]. Returns jobs published."""
    day = today.isoformat()
    due_from = (today - timedelta(days=lookback_days)).isoformat()
    after, total = "", 0
    while True:
        want = batch_size if limit is None else min(batch_size, limit - total)
        if want <= 0:
            break
        rows = await db.due_emis(due_from, day, after_id=after, limit=want)
        if not rows:
            break
        items = [(r.borrower_id, json.dumps(make_job(r, day)).encode()) for r in rows]
        await queue.publish_batch(topic, items)
        total += len(rows)
        after = rows[-1].emi_id
    log.info("scheduled %d jobs for %s", total, day)
    return total
