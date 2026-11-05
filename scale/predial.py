"""The decision made by a worker right before it dials: dial or skip.

Order matters:  paid -> opted out -> claim (dedupe) -> daily cap.
Cheap, common skips first; the claim is taken only for jobs that will really be dialed.
Redis trouble on read paths degrades to the DB instead of failing the call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from redis.exceptions import RedisError

from .cache import Cache
from .db import Db

log = logging.getLogger("scale.predial")


@dataclass(frozen=True)
class Decision:
    dial: bool
    reason: str  # "ok" or why we skipped


async def _is_paid(job: dict, db: Db, cache: Cache) -> bool:
    emi_id = job["emi_id"]
    try:
        state = await cache.paid_state(emi_id)
        if state is not None:
            return state
    except RedisError as e:
        log.warning("redis read failed for paid flag, using DB: %s", e)
        return await db.is_paid(emi_id)
    paid = await db.is_paid(emi_id)  # cache miss -> DB is the truth
    try:
        await cache.remember_paid_state(emi_id, paid)
    except RedisError:
        pass
    return paid


async def _is_opted_out(job: dict, db: Db, cache: Cache) -> bool:
    bid = job["borrower_id"]
    try:
        state = await cache.optout_state(bid)
        if state is not None:
            return state
    except RedisError as e:
        log.warning("redis read failed for opt-out flag, using DB: %s", e)
        return await db.is_opted_out(bid)
    opted = await db.is_opted_out(bid)
    try:
        await cache.remember_optout_state(bid, opted)
    except RedisError:
        pass
    return opted


async def decide(job: dict, db: Db, cache: Cache, *, today: str, daily_cap: int, claim_ttl_s: int) -> Decision:
    if await _is_paid(job, db, cache):
        return Decision(False, "skipped_paid")
    if await _is_opted_out(job, db, cache):
        return Decision(False, "skipped_opted_out")
    # Writes to Redis (claim, cap) are NOT swallowed: if we cannot guarantee
    # at-most-once or the cap, we must not dial. The worker retries, then dead-letters.
    if not await cache.claim(job["job_id"], claim_ttl_s):
        return Decision(False, "skipped_duplicate")
    if not await cache.take_daily_slot(job["borrower_id"], today, daily_cap):
        await cache.release_claim(job["job_id"])
        return Decision(False, "skipped_daily_cap")
    return Decision(True, "ok")
