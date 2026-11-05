"""The behaviours that matter: a borrower who has paid must not be called, however the race falls."""
import asyncio
import json

import httpx
import pytest

from scale.caller import SimulatedCaller
from scale.queues import Delivery
from scale.results_writer import run_results_writer
from scale.scheduler import make_job, schedule_due
from scale.webhook import create_app, sign
from scale.worker import Worker

from conftest import TODAY, seed_borrowers

SECRET = "test-secret"


async def prepare(queue, t):
    await queue.ensure_topic(t["jobs"], groups=(t["group"],))
    await queue.ensure_topic(t["results"], groups=(t["rgroup"],))
    await queue.ensure_topic(t["dlq"])


def make_worker(queue, db, cache, caller, t, **kw):
    return Worker(
        queue=queue, db=db, cache=cache, caller=caller,
        jobs_topic=t["jobs"], results_topic=t["results"], dlq_topic=t["dlq"], group=t["group"],
        concurrency=kw.pop("concurrency", 20), today_fn=lambda: TODAY.isoformat(), **kw,
    )


async def drain(queue, db, t, worker):
    """Run the worker, then the results writer, until both go idle."""
    await worker.run(asyncio.Event(), poll_timeout_s=0.2, idle_exit_s=1.0)
    return await run_results_writer(queue, db, t["results"], t["rgroup"], asyncio.Event(), max_wait_s=0.2, idle_exit_s=1.0)


async def pay_via_webhook(app, emi_id, payment_id, amount=5000, secret=SECRET):
    body = json.dumps({"payment_id": payment_id, "emi_id": emi_id, "amount": amount}).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/webhooks/payment", content=body, headers={"X-Signature": sign(secret, body)})


async def test_unpaid_borrowers_are_called(queue, db, cache, topics):
    await seed_borrowers(db, 20)
    await prepare(queue, topics)
    assert await schedule_due(db, queue, topics["jobs"], today=TODAY) == 20
    caller = SimulatedCaller()
    written = await drain(queue, db, topics, make_worker(queue, db, cache, caller, topics))
    assert len(caller.calls) == 20 and written == 20


async def test_paid_before_scheduling_is_never_queued(queue, db, cache, topics):
    await seed_borrowers(db, 10)
    app = create_app(db, cache, SECRET)
    assert (await pay_via_webhook(app, "e0003", "p1")).status_code == 200
    await prepare(queue, topics)
    assert await schedule_due(db, queue, topics["jobs"], today=TODAY) == 9


async def test_paid_AFTER_queued_skips_the_call(queue, db, cache, topics):
    """The headline case: job is already in the queue, then the borrower pays, then the worker runs."""
    await seed_borrowers(db, 10)
    await prepare(queue, topics)
    await schedule_due(db, queue, topics["jobs"], today=TODAY)  # all 10 queued

    app = create_app(db, cache, SECRET)
    for i in (1, 4, 7):  # three borrowers pay while their jobs sit in the queue
        r = await pay_via_webhook(app, f"e{i:04d}", f"pay{i}")
        assert r.json() == {"status": "applied", "cache_updated": True}

    caller = SimulatedCaller()
    w = make_worker(queue, db, cache, caller, topics)
    await drain(queue, db, topics, w)

    assert sorted(caller.calls) == sorted(f"e{i:04d}:{TODAY}" for i in (0, 2, 3, 5, 6, 8, 9))
    assert w.stats["skipped_paid"] == 3
    counts = await db.outcome_counts()
    assert counts["skipped_paid"] == 3  # the skip is recorded, not silent


async def test_paid_after_queued_with_redis_wiped_falls_back_to_db(queue, db, cache, topics):
    await seed_borrowers(db, 5)
    await prepare(queue, topics)
    await schedule_due(db, queue, topics["jobs"], today=TODAY)
    app = create_app(db, cache, SECRET)
    await pay_via_webhook(app, "e0002", "pay2")
    await cache.r.flushall()  # Redis restarts / evicts everything

    caller = SimulatedCaller()
    await drain(queue, db, topics, make_worker(queue, db, cache, caller, topics))
    assert f"e0002:{TODAY}" not in caller.calls and len(caller.calls) == 4
    assert await cache.paid_state("e0002") is True  # and the cache healed itself


async def test_webhook_db_ok_when_redis_write_fails(db, cache, queue, topics):
    """DB is truth: if Redis is down during the webhook, the payment must still be recorded and
    the worker (Redis miss -> DB) must still skip the call."""
    await seed_borrowers(db, 3)
    await prepare(queue, topics)
    await schedule_due(db, queue, topics["jobs"], today=TODAY)

    class BrokenCache(type(cache)):
        async def mark_paid(self, emi_id):
            from redis.exceptions import ConnectionError as RErr
            raise RErr("redis down")

    broken = BrokenCache(cache.r)
    r = await pay_via_webhook(create_app(db, broken, SECRET), "e0001", "pay1")
    assert r.status_code == 200 and r.json() == {"status": "applied", "cache_updated": False}
    assert await db.is_paid("e0001")

    caller = SimulatedCaller()
    await drain(queue, db, topics, make_worker(queue, db, cache, caller, topics))
    assert f"e0001:{TODAY}" not in caller.calls and len(caller.calls) == 2


async def test_webhook_is_idempotent_and_heals_redis(db, cache):
    await seed_borrowers(db, 2)
    app = create_app(db, cache, SECRET)
    assert (await pay_via_webhook(app, "e0000", "payX")).json()["status"] == "applied"
    await cache.r.flushall()
    r = await pay_via_webhook(app, "e0000", "payX")  # provider retries the same webhook
    assert r.json()["status"] == "duplicate"
    assert await cache.paid_state("e0000") is True  # duplicate still refreshed Redis
    cur = await db.c.execute("SELECT COUNT(*) FROM payments")
    assert (await cur.fetchone())[0] == 1


async def test_webhook_rejects_bad_signature_and_unknown_emi(db, cache):
    await seed_borrowers(db, 1)
    app = create_app(db, cache, SECRET)
    assert (await pay_via_webhook(app, "e0000", "p", secret="wrong")).status_code == 401
    assert not await db.is_paid("e0000") and await cache.paid_state("e0000") is None
    assert (await pay_via_webhook(app, "nope", "p2")).status_code == 404


async def test_webhook_refuses_to_start_without_secret(db, cache):
    with pytest.raises(RuntimeError):
        create_app(db, cache, "")


async def test_duplicate_job_is_dialed_once(queue, db, cache, topics):
    await seed_borrowers(db, 3)
    await prepare(queue, topics)
    await schedule_due(db, queue, topics["jobs"], today=TODAY)
    await schedule_due(db, queue, topics["jobs"], today=TODAY)  # scheduler ran twice
    caller = SimulatedCaller()
    w = make_worker(queue, db, cache, caller, topics)
    await drain(queue, db, topics, w)
    assert len(caller.calls) == 3 and w.stats["skipped_duplicate"] == 3


async def test_concurrent_duplicates_still_dial_once(queue, db, cache, topics):
    """Same job delivered 10x at once (rebalance / retry storm): SET NX lets exactly one through."""
    await seed_borrowers(db, 1)
    await prepare(queue, topics)
    job = make_job((await db.due_emis("2000-01-01", "2100-01-01"))[0], TODAY.isoformat())
    await queue.publish_batch(topics["jobs"], [(job["borrower_id"], json.dumps(job).encode())] * 10)
    caller = SimulatedCaller(duration_s=0.05)
    await drain(queue, db, topics, make_worker(queue, db, cache, caller, topics))
    assert len(caller.calls) == 1


async def test_opted_out_borrower_is_skipped_even_if_queued(queue, db, cache, topics):
    await seed_borrowers(db, 4)
    await prepare(queue, topics)
    await schedule_due(db, queue, topics["jobs"], today=TODAY)
    app = create_app(db, cache, SECRET)
    body = json.dumps({"borrower_id": "b0002"}).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/webhooks/opt-out", content=body, headers={"X-Signature": sign(SECRET, body)})
    assert r.status_code == 200
    caller = SimulatedCaller()
    await drain(queue, db, topics, make_worker(queue, db, cache, caller, topics))
    assert f"e0002:{TODAY}" not in caller.calls and len(caller.calls) == 3


async def test_daily_cap(queue, db, cache, topics):
    """One borrower with 3 EMIs, cap 2 -> only 2 calls."""
    await db.seed([("b1", "Multi", "+911")], [(f"m{i}", "b1", 1000, TODAY.isoformat()) for i in range(3)])
    await prepare(queue, topics)
    await schedule_due(db, queue, topics["jobs"], today=TODAY)
    caller = SimulatedCaller()
    w = make_worker(queue, db, cache, caller, topics, daily_cap=2)
    await drain(queue, db, topics, w)
    assert len(caller.calls) == 2 and w.stats["skipped_daily_cap"] == 1


async def test_unpaid_cache_entry_cannot_overwrite_a_payment(db, cache):
    """Race: worker reads DB (unpaid), webhook pays + sets Redis=1, worker then refills 'unpaid'.
    The refill must not clobber the paid flag."""
    await cache.mark_paid("eX")
    await cache.remember_paid_state("eX", False)
    assert await cache.paid_state("eX") is True


async def test_job_that_waits_in_queue_is_rechecked_at_dial_time(queue, db, cache, topics):
    """Payment lands while the worker is busy with earlier calls; the later job must still be skipped."""
    await seed_borrowers(db, 6)
    await prepare(queue, topics)
    await schedule_due(db, queue, topics["jobs"], today=TODAY)
    app = create_app(db, cache, SECRET)
    caller = SimulatedCaller(duration_s=0.3)
    w = make_worker(queue, db, cache, caller, topics, concurrency=1)  # strictly one call at a time

    async def pay_late():
        await asyncio.sleep(0.5)  # worker is mid-way through the list
        for i in range(6):
            await pay_via_webhook(app, f"e{i:04d}", f"late{i}")

    await asyncio.gather(drain(queue, db, topics, w), pay_late())
    assert 1 <= len(caller.calls) < 6  # the ones after the payment were skipped
    assert w.stats["skipped_paid"] == 6 - len(caller.calls)


async def test_bad_message_goes_to_dlq_not_a_crash(queue, db, cache, topics):
    await prepare(queue, topics)
    await queue.publish(topics["jobs"], "k", b"{not json")
    caller = SimulatedCaller()
    w = make_worker(queue, db, cache, caller, topics)
    await w.run(asyncio.Event(), poll_timeout_s=0.2, idle_exit_s=1.0)
    assert caller.calls == []
    dlq = await queue.subscribe(topics["dlq"], "check")
    msgs = await dlq.poll(5, 1.0)
    assert len(msgs) == 1 and b"bad_json" in msgs[0].value


async def test_redelivered_results_do_not_double_count(db):
    row = ("j1", "e1", "promised_to_pay", "", 1.0)
    assert await db.insert_outcomes([row]) == 1
    assert await db.insert_outcomes([row]) == 0


async def test_high_concurrency_does_not_dead_letter(queue, db, cache, topics):
    """Regression: 500 concurrent jobs once exhausted the Redis pool and jobs were dead-lettered."""
    await seed_borrowers(db, 1500)
    await prepare(queue, topics)
    await schedule_due(db, queue, topics["jobs"], today=TODAY)
    caller = SimulatedCaller(duration_s=0.01)
    w = make_worker(queue, db, cache, caller, topics, concurrency=400)
    await drain(queue, db, topics, w)
    assert len(caller.calls) == 1500 and w.stats["dead_lettered"] == 0
