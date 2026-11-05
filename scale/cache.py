"""Redis layer: fast pre-dial flags. A cache of the DB, never the source of truth.

Keys
  emi:{id}:paid        "1" paid / "0" known-unpaid (short TTL)
  borrower:{id}:optout "1" / "0"
  claim:{job_id}       SET NX lock so a job is dialed at most once
  calls:{borrower}:{d} per-day call counter (daily cap)
"""
from __future__ import annotations

from typing import Optional

import redis.asyncio as aioredis


class Cache:
    def __init__(self, r: aioredis.Redis, paid_ttl_s: int = 7 * 86400, unpaid_ttl_s: int = 3600):
        self.r = r
        self.paid_ttl_s = paid_ttl_s
        self.unpaid_ttl_s = unpaid_ttl_s

    @classmethod
    def from_url(cls, url: str, max_connections: int = 1000, **kw) -> "Cache":
        # The pool must be at least as big as worker concurrency, or jobs fail with
        # "Too many connections" (found by a 20k-job load run, not by the unit tests).
        return cls(aioredis.from_url(url, decode_responses=True, max_connections=max_connections), **kw)

    async def close(self) -> None:
        await self.r.aclose()

    # ---- paid ---------------------------------------------------------
    async def mark_paid(self, emi_id: str) -> None:
        """Called by the webhook AFTER the DB write. Unconditional SET: paid always wins."""
        await self.r.set(f"emi:{emi_id}:paid", "1", ex=self.paid_ttl_s)

    async def paid_state(self, emi_id: str) -> Optional[bool]:
        v = await self.r.get(f"emi:{emi_id}:paid")
        return None if v is None else v == "1"

    async def remember_paid_state(self, emi_id: str, paid: bool) -> None:
        """Refill after a DB read on a miss.

        'paid' may overwrite. 'unpaid' uses SET NX so it can never clobber a '1' that the
        webhook wrote between our DB read and this write (the race that would resurrect a call).
        """
        if paid:
            await self.r.set(f"emi:{emi_id}:paid", "1", ex=self.paid_ttl_s)
        else:
            await self.r.set(f"emi:{emi_id}:paid", "0", ex=self.unpaid_ttl_s, nx=True)

    # ---- opt-out ------------------------------------------------------
    async def mark_opted_out(self, borrower_id: str) -> None:
        await self.r.set(f"borrower:{borrower_id}:optout", "1")

    async def optout_state(self, borrower_id: str) -> Optional[bool]:
        v = await self.r.get(f"borrower:{borrower_id}:optout")
        return None if v is None else v == "1"

    async def remember_optout_state(self, borrower_id: str, opted_out: bool) -> None:
        if opted_out:
            await self.r.set(f"borrower:{borrower_id}:optout", "1")
        else:
            await self.r.set(f"borrower:{borrower_id}:optout", "0", ex=self.unpaid_ttl_s, nx=True)

    # ---- claim lock (idempotency) ------------------------------------------
    async def claim(self, job_id: str, ttl_s: int) -> bool:
        return bool(await self.r.set(f"claim:{job_id}", "1", ex=ttl_s, nx=True))

    async def release_claim(self, job_id: str) -> None:
        await self.r.delete(f"claim:{job_id}")

    # ---- daily cap -----------------------------------------------------------
    async def take_daily_slot(self, borrower_id: str, day: str, cap: int) -> bool:
        key = f"calls:{borrower_id}:{day}"
        n = await self.r.incr(key)
        if n == 1:
            await self.r.expire(key, 2 * 86400)
        if n > cap:
            await self.r.decr(key)
            return False
        return True
