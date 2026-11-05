"""Run each stage as its own process, exactly as you would in production.

  python -m scale.cli seed --n 5000          # fake borrowers, all due today
  python -m scale.cli schedule               # DB -> queue
  python -m scale.cli worker                 # run as many of these as you like
  python -m scale.cli results                # queue -> DB (batched)
  python -m scale.cli webhook --port 8080    # payment + opt-out endpoints
  python -m scale.cli pay --emi e000042      # send a signed test payment to the webhook
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date

import uvicorn
from dotenv import load_dotenv

from .cache import Cache
from .caller import SimulatedCaller
from .config import get_settings
from .db import Db
from .queues import make_backend
from .results_writer import run_results_writer
from .scheduler import schedule_due
from .webhook import create_app, sign
from .worker import Worker


def _queue(s):
    return make_backend(s.queue_backend, kafka_bootstrap=s.kafka_bootstrap, pulsar_url=s.pulsar_url, partitions=s.topic_partitions)


async def cmd_seed(a, s):
    db = await Db(s.db_path).open()
    today = date.today().isoformat()
    await db.seed(
        [(f"b{i:06d}", f"Borrower {i}", f"+9190000{i:05d}") for i in range(a.n)],
        [(f"e{i:06d}", f"b{i:06d}", 5000 + i % 20000, today) for i in range(a.n)],
    )
    print(f"seeded {a.n} borrowers/EMIs due {today} into {s.db_path}")
    await db.close()


async def cmd_schedule(a, s):
    db, q = await Db(s.db_path).open(), _queue(s)
    await q.start()
    await q.ensure_topic(s.jobs_topic, groups=(s.group,))  # subscription first so Pulsar keeps the jobs
    n = await schedule_due(db, q, s.jobs_topic, today=date.today(), limit=a.limit)
    print(f"published {n} jobs to {s.jobs_topic} on {q.name}")
    await q.stop()
    await db.close()


async def cmd_worker(a, s):
    db, q = await Db(s.db_path).open(), _queue(s)
    conc = a.concurrency or s.worker_concurrency
    cache = Cache.from_url(s.redis_url, max_connections=max(100, 2 * conc), paid_ttl_s=s.paid_ttl_s, unpaid_ttl_s=s.unpaid_ttl_s)
    await q.start()
    await q.ensure_topic(s.results_topic, groups=("emi-results-writer",))
    caller = SimulatedCaller(duration_s=a.call_seconds)
    w = Worker(queue=q, db=db, cache=cache, caller=caller, jobs_topic=s.jobs_topic, results_topic=s.results_topic,
               dlq_topic=s.dlq_topic, group=s.group, concurrency=conc,
               daily_cap=s.daily_call_cap, claim_ttl_s=s.claim_ttl_s)
    await w.run(asyncio.Event(), idle_exit_s=a.idle_exit)
    print("worker done:", dict(w.stats))
    await q.stop()
    await cache.close()
    await db.close()


async def cmd_results(a, s):
    db, q = await Db(s.db_path).open(), _queue(s)
    await q.start()
    n = await run_results_writer(q, db, s.results_topic, "emi-results-writer", asyncio.Event(), idle_exit_s=a.idle_exit)
    print(f"wrote {n} outcomes;", await db.outcome_counts())
    await q.stop()
    await db.close()


async def cmd_webhook(a, s):
    db = await Db(s.db_path).open()
    cache = Cache.from_url(s.redis_url, paid_ttl_s=s.paid_ttl_s)
    app = create_app(db, cache, s.webhook_secret)
    await uvicorn.Server(uvicorn.Config(app, host=a.host, port=a.port, log_level="info")).serve()


async def cmd_pay(a, s):
    import httpx

    body = json.dumps({"payment_id": a.payment_id, "emi_id": a.emi, "amount": a.amount}).encode()
    r = httpx.post(f"http://localhost:{a.port}/webhooks/payment", content=body, headers={"X-Signature": sign(s.webhook_secret, body)})
    print(r.status_code, r.text)


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="scale")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("seed"); p.add_argument("--n", type=int, default=1000); p.set_defaults(fn=cmd_seed)
    p = sub.add_parser("schedule"); p.add_argument("--limit", type=int, default=None); p.set_defaults(fn=cmd_schedule)
    p = sub.add_parser("worker"); p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--call-seconds", type=float, default=0.0, help="simulated call length")
    p.add_argument("--idle-exit", type=float, default=None); p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("results"); p.add_argument("--idle-exit", type=float, default=None); p.set_defaults(fn=cmd_results)
    p = sub.add_parser("webhook"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8080); p.set_defaults(fn=cmd_webhook)
    p = sub.add_parser("pay"); p.add_argument("--emi", required=True); p.add_argument("--payment-id", default="manual-1")
    p.add_argument("--amount", type=int, default=5000); p.add_argument("--port", type=int, default=8080); p.set_defaults(fn=cmd_pay)
    a = ap.parse_args()
    asyncio.run(a.fn(a, get_settings()))


if __name__ == "__main__":
    main()
