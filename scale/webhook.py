"""Payment webhook: the "external link" the payment provider calls when a borrower pays.

Order is the design:  verify signature -> write DB (truth) -> write Redis (cache).
 * DB first: if we crash between the two, Redis misses and workers fall back to the DB. Safe.
 * Redis first would be unsafe: a crash would leave Redis saying "paid" with no record behind it.
 * Idempotent on payment_id: providers retry webhooks; the second delivery changes nothing but
   still refreshes Redis, which heals a previous Redis failure.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from .cache import Cache
from .db import Db

log = logging.getLogger("scale.webhook")


class PaymentEvent(BaseModel):
    payment_id: str = Field(min_length=1)
    emi_id: str = Field(min_length=1)
    amount: int = Field(gt=0)


class OptOutEvent(BaseModel):
    borrower_id: str = Field(min_length=1)


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def create_app(db: Db, cache: Cache, secret: str) -> FastAPI:
    if not secret:
        raise RuntimeError("WEBHOOK_SECRET is not set; refusing to start an unauthenticated payment webhook")
    app = FastAPI(title="EMI payment webhook")

    async def _verify(request: Request, signature: str | None) -> bytes:
        body = await request.body()
        if not signature or not hmac.compare_digest(signature, sign(secret, body)):
            raise HTTPException(401, "bad signature")
        return body

    @app.post("/webhooks/payment")
    async def payment(request: Request, x_signature: str | None = Header(default=None)):
        body = await _verify(request, x_signature)
        ev = PaymentEvent.model_validate_json(body)
        status = await db.mark_paid(ev.payment_id, ev.emi_id, ev.amount)  # 1) truth
        if status == "unknown_emi":
            raise HTTPException(404, "unknown emi")
        cache_ok = True
        try:
            await cache.mark_paid(ev.emi_id)  # 2) cache (also on duplicates, to heal Redis)
        except RedisError as e:
            cache_ok = False
            log.error("redis update failed for %s (DB is correct, workers will fall back): %s", ev.emi_id, e)
        return {"status": status, "cache_updated": cache_ok}

    @app.post("/webhooks/opt-out")
    async def opt_out(request: Request, x_signature: str | None = Header(default=None)):
        body = await _verify(request, x_signature)
        ev = OptOutEvent.model_validate_json(body)
        if not await db.set_opted_out(ev.borrower_id):
            raise HTTPException(404, "unknown borrower")
        cache_ok = True
        try:
            await cache.mark_opted_out(ev.borrower_id)
        except RedisError:
            cache_ok = False
        return {"status": "applied", "cache_updated": cache_ok}

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    return app
