# Scale layer: scheduler → queue → workers → Redis checks

Turns the single-call voice bot into a pipeline that can place many reminder calls, and never calls
someone who has already paid.

```
 DB (truth) ──► scheduler ──► jobs topic ──► workers ──► caller (Pipecat bot / simulated)
   ▲  ▲                      (Kafka | Pulsar)   │  ▲
   │  │                                          │  └── pre-dial check: paid? opted out? duplicate? daily cap?
   │  └── payment webhook ──► DB first, then Redis ──────────────┘ (Redis miss → ask DB → refill Redis)
   │                                              ▼
   └──────── results writer (batches) ◄── results topic
```

## The rule that answers "paid before the call happened"

1. The payment provider calls `POST /webhooks/payment` (HMAC-signed).
2. The webhook writes the **DB first** (source of truth), then sets `emi:{id}:paid=1` in **Redis**.
3. A worker takes a job off the queue and, **right before dialing**, checks Redis. If the flag is
   missing (Redis restarted, key expired) it asks the DB and refills Redis. Paid → the call is skipped
   and `skipped_paid` is recorded as the outcome.

Why this order and not the reverse: if the webhook crashes between the two writes, Redis simply has no
flag and workers fall back to the DB, which is correct. Redis-first could leave "paid" in the cache with
nothing behind it.

| When the payment lands | What happens |
|---|---|
| Before the scheduler runs | EMI is not selected (`status='pending'` filter); no job exists |
| After the job is queued, before the worker picks it up | Worker's pre-dial check sees paid → skip |
| While the job waits for a free worker slot | Check runs only after a slot is free (worker fetches only as many jobs as it can start now) → skip |
| Redis was wiped / webhook couldn't reach Redis | Redis miss → DB says paid → skip, and Redis is healed |
| Provider retries the same webhook | `payment_id` is unique: second delivery is a no-op but still refreshes Redis |
| **During** a call that already started | Not preventable. The call finishes and its outcome is recorded. |

Other pre-dial checks: opted-out borrower, duplicate job (`SET NX claim:{job_id}`), and a per-borrower daily
call cap. Redis *read* failures degrade to the DB; Redis *write* failures (claim, cap) are retried and then
the job goes to the dead-letter topic rather than dialing without the safety checks.

## Run it

```bash
pip install -r requirements-scale.txt
# add the lines from .env.scale.example to your existing .env (never commit .env)

docker compose -f docker-compose.scale.yml up -d redis kafka     # or: redis pulsar
export QUEUE_BACKEND=kafka                                        # or: pulsar

python -m scale.cli seed --n 5000
python -m scale.cli schedule
python -m scale.cli worker --concurrency 100 --call-seconds 0.2 --idle-exit 5 &
python -m scale.cli results --idle-exit 5 &
python -m scale.cli webhook --port 8080 &
python -m scale.cli pay --emi e000042           # try this BEFORE the worker drains the queue
```

Env vars (all optional except `WEBHOOK_SECRET`): `QUEUE_BACKEND`, `REDIS_URL`, `KAFKA_BOOTSTRAP`,
`PULSAR_URL`, `SCALE_DB_PATH`, `DAILY_CALL_CAP`, `WORKER_CONCURRENCY`.

`SimulatedCaller` stands in for the real call (no STT/LLM/TTS, no cost). To use the Pipecat bot, implement
`Caller.call(job) -> CallResult` in `scale/caller.py`; nothing else changes.

## Tests

```bash
pytest scale_tests                                    # memory queue + a real Redis server it starts itself
KAFKA_BOOTSTRAP_TEST=localhost:9092 pytest scale_tests        # same tests, also on Kafka
PULSAR_URL_TEST=pulsar://localhost:6650 pytest scale_tests    # same tests, also on Pulsar
```

The pipeline tests are parametrized over backends, so Kafka and Pulsar run the *identical* scenarios.
Covered: paid before scheduling, paid after queued, paid with Redis wiped, webhook with Redis down,
idempotent/forged/unknown webhooks, duplicate and concurrent-duplicate jobs, opt-out, daily cap, the
"unpaid refill must not overwrite paid" race, jobs waiting for a slot, bad messages → DLQ, redelivered
results, and a 1,500-job / 400-concurrency run.

## Benchmark

```bash
python -m scale.bench --backend kafka  --n 20000 --out kafka.json
python -m scale.bench --backend pulsar --n 20000 --out pulsar.json
```

Run one broker at a time. Both are single-node on one machine, so compare them to each other only.

**Measured on a MacBook Air (Apple silicon), Docker Desktop with 3.8 GB, single-node brokers, client and broker on the same machine, 20,000 messages of ~300 bytes, 4 consumers, one run each:**

| | Kafka 3.9 (8 partitions) | Pulsar 3.3 standalone |
|---|---|---|
| Publish throughput | ~46,900 msgs/s | ~25,800 msgs/s |
| End-to-end throughput | ~46,100 msgs/s | ~25,500 msgs/s |
| Latency p50 / p95 / p99 | 25 / 105 / 110 ms | 40 / 119 / 124 ms |

How to read this: both are roughly 8,000x faster than the ~3 calls/s that one lakh calls a day needs, so
throughput does not decide the choice. Kafka was ahead here, but this is a single run, and part of the gap
may come from my Pulsar wrapper (the blocking client runs in threads, with a 5 ms batching delay) rather than
the broker itself. Treat it as "both are fast enough", not "Kafka is 2x faster than Pulsar".

## What was and wasn't verified (honest status)

- **Verified here:** all 17 tests on the in-memory queue with a real Redis 7 server; disabling the paid
  check makes 4 of them fail. A 20,000-job in-process run: 2,000 pre-paid → 2,000 `skipped_paid`,
  18,000 dialed, 20,000 outcomes written, 0 dead-lettered (one worker process, ~900 jobs/s of
  pre-dial work on 2 CPUs; simulated 20 ms calls). That run exposed a Redis pool-size bug, now fixed
  and covered by a test.
- **Verified on a real Kafka broker and a real Pulsar broker (on the author's Mac):** the same 29 test
  runs (17 scenarios on memory, plus the 12 pipeline scenarios on each broker) pass on Kafka 3.9 and on
  Pulsar 3.3, with no code changes needed. Benchmark numbers are in the table above.
- Not covered: multi-node brokers, broker failure/restart mid-run, multiple worker processes competing on one
  topic, and long soak runs. All numbers are single-run, single-machine.
- The queue-only memory benchmark number is harness overhead, not a broker measurement.

## Kafka vs Pulsar: what to expect, and what to say

Both fit this job. Differences that matter here (design facts, not measured results):

- **Parallelism:** Kafka consumers in a group are capped by partition count. Pulsar's Shared subscription
  has no such cap.
- **Acks:** Pulsar acks per message, which suits calls that finish out of order. Kafka commits offsets, so
  the Kafka backend only commits up to the lowest unfinished offset; a crash can replay a few finished jobs,
  which the claim lock and idempotent results make harmless.
- **Ops:** Kafka is the more common skill and tooling; Pulsar adds BookKeeper/ZooKeeper-style moving parts.
- **Pulsar gotcha:** a subscription must exist before publishing or messages can be dropped; `ensure_topic(..., groups=...)` creates it.

For one lakh calls a day (~3 calls/s) either broker is far beyond what's needed; the real bottlenecks are
telephony/STT/LLM/TTS concurrency, not the queue.

## Known limits

- At-least-once delivery. If a worker dies after taking the claim but before the call finishes, the claim
  (TTL `CLAIM_TTL_S`, default 15 min) blocks an immediate retry; that job is skipped as a duplicate that day.
- Partial payments are not modelled (any payment marks the EMI paid). Real systems need an amount check.
- SQLite is single-writer; swap `scale/db.py` for Postgres in production. No scheduler pacing/time-windows
  (calling hours, DND) yet. Dead-letter jobs have no replay tool yet.