"""Results writer: drain the results topic and write outcomes to the DB in batches.

One INSERT per call would hammer the DB at scale; batching turns thousands of writes into a few.
Rows are keyed by job_id with INSERT OR IGNORE, so a redelivered result cannot double-count.
"""
from __future__ import annotations

import asyncio
import json
import time

from .db import Db
from .queues import QueueBackend


async def run_results_writer(
    queue: QueueBackend,
    db: Db,
    topic: str,
    group: str,
    stop: asyncio.Event,
    *,
    batch_size: int = 500,
    max_wait_s: float = 0.5,
    idle_exit_s: float | None = None,
) -> int:
    consumer = await queue.subscribe(topic, group)
    written, idle_since = 0, time.monotonic()
    try:
        while not stop.is_set():
            batch = await consumer.poll(batch_size, max_wait_s)
            if not batch:
                if idle_exit_s is not None and time.monotonic() - idle_since > idle_exit_s:
                    break
                continue
            idle_since = time.monotonic()
            rows = []
            for d in batch:
                r = json.loads(d.value)
                rows.append((r["job_id"], r["emi_id"], r["outcome"], r.get("reason"), r["finished_at"]))
            written += await db.insert_outcomes(rows)
            await consumer.ack(batch)  # ack only after the DB commit
    finally:
        await consumer.close()
    return written
