"""Worker: take a job from the queue, re-check the world, then dial or skip.

The paid check happens HERE, right before dialing, not when the job was queued. That is the
whole point: a borrower who pays at 10:02 must not get a reminder call at 10:05 just because
the job was created at 09:00.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from datetime import date

from .cache import Cache
from .caller import Caller
from .db import Db
from .predial import decide
from .queues import Consumer, Delivery, QueueBackend

log = logging.getLogger("scale.worker")


class Worker:
    def __init__(
        self,
        *,
        queue: QueueBackend,
        db: Db,
        cache: Cache,
        caller: Caller,
        jobs_topic: str,
        results_topic: str,
        dlq_topic: str,
        group: str,
        concurrency: int = 50,
        daily_cap: int = 3,
        claim_ttl_s: int = 900,
        retries: int = 3,
        today_fn=lambda: date.today().isoformat(),
    ):
        self.queue, self.db, self.cache, self.caller = queue, db, cache, caller
        self.jobs_topic, self.results_topic, self.dlq_topic, self.group = jobs_topic, results_topic, dlq_topic, group
        self.concurrency, self.daily_cap, self.claim_ttl_s, self.retries = concurrency, daily_cap, claim_ttl_s, retries
        self.today_fn = today_fn
        self.stats: Counter = Counter()

    async def run(self, stop: asyncio.Event, poll_timeout_s: float = 0.5, idle_exit_s: float | None = None) -> None:
        """Process until `stop` is set (or, if idle_exit_s is given, until the queue is idle that long)."""
        consumer = await self.queue.subscribe(self.jobs_topic, self.group)
        tasks: set[asyncio.Task] = set()
        idle_since = time.monotonic()
        try:
            while not stop.is_set():
                free = self.concurrency - len(tasks)
                if free <= 0:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    continue
                # Only fetch as many jobs as we can start NOW, so the pre-dial check is
                # never separated from the dial by a long wait in a local buffer.
                batch = await consumer.poll(free, poll_timeout_s)
                if batch:
                    idle_since = time.monotonic()
                for d in batch:
                    t = asyncio.create_task(self._handle(consumer, d))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                if not batch and not tasks and idle_exit_s is not None and time.monotonic() - idle_since > idle_exit_s:
                    break
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await consumer.close()

    async def _handle(self, consumer: Consumer, d: Delivery) -> None:
        try:
            job = json.loads(d.value)
        except Exception:
            await self._dead_letter(d, "bad_json")
            await consumer.ack([d])
            return
        try:
            result = await self._process_with_retry(job)
        except Exception as e:  # infra kept failing: park it, don't loop forever, don't dial blind
            log.exception("job %s failed after retries", job.get("job_id"))
            await self._dead_letter(d, f"error:{type(e).__name__}")
            await consumer.ack([d])
            self.stats["dead_lettered"] += 1
            return
        await self.queue.publish(self.results_topic, job["job_id"], json.dumps(result).encode())
        await consumer.ack([d])  # ack only after the result is safely queued

    async def _process_with_retry(self, job: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                return await self._process(job)
            except Exception as e:
                last = e
                await asyncio.sleep(0.05 * 2**attempt)
        raise last  # type: ignore[misc]

    async def _process(self, job: dict) -> dict:
        decision = await decide(
            job, self.db, self.cache, today=self.today_fn(), daily_cap=self.daily_cap, claim_ttl_s=self.claim_ttl_s
        )
        if not decision.dial:
            self.stats[decision.reason] += 1
            outcome, reason = decision.reason, decision.reason
        else:
            res = await self.caller.call(job)
            self.stats["dialed"] += 1
            outcome, reason = res.outcome, res.detail
        return {
            "job_id": job["job_id"],
            "emi_id": job["emi_id"],
            "outcome": outcome,
            "reason": reason,
            "finished_at": time.time(),
        }

    async def _dead_letter(self, d: Delivery, why: str) -> None:
        payload = json.dumps({"why": why, "raw": d.value.decode(errors="replace")}).encode()
        await self.queue.publish(self.dlq_topic, d.key or "dlq", payload)
