"""Kafka vs Pulsar vs memory: same code path, same load, same measurements.

    python -m scale.bench --backend kafka  --n 20000
    python -m scale.bench --backend pulsar --n 20000

Measures the queue only (no Redis/DB/calls): publish throughput, consume throughput, and
publish->consume latency (p50/p95/p99). Latency is measured while the consumer keeps up with
the producer, so it reflects broker + client overhead, not a deliberately built backlog.
Both clients run on one machine against a single-node broker: compare the two numbers to each
other, do not read them as production capacity.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid

from dotenv import load_dotenv

from .config import get_settings
from .queues import make_backend


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


async def run(backend_name: str, n: int, size: int, batch: int, consumers: int) -> dict:
    s = get_settings()
    q = make_backend(backend_name, kafka_bootstrap=s.kafka_bootstrap, pulsar_url=s.pulsar_url, partitions=s.topic_partitions)
    await q.start()
    topic, group = f"bench-{uuid.uuid4().hex[:8]}", "bench"
    await q.ensure_topic(topic, groups=(group,))
    pad = "x" * max(0, size - 80)
    latencies: list[float] = []
    received = 0
    done = asyncio.Event()
    t_first_consume = t_last_consume = 0.0

    async def consume():
        nonlocal received, t_first_consume, t_last_consume
        c = await q.subscribe(topic, group)
        try:
            while received < n:
                msgs = await c.poll(500, 1.0)
                if not msgs:
                    continue
                now = time.time()
                if not t_first_consume:
                    t_first_consume = time.perf_counter()
                for m in msgs:
                    latencies.append(now - json.loads(m.value)["t"])
                await c.ack(msgs)
                received += len(msgs)
                t_last_consume = time.perf_counter()
            done.set()
        finally:
            await c.close()

    cons = [asyncio.create_task(consume()) for _ in range(consumers)]
    await asyncio.sleep(1.0)  # let consumers join the group before publishing
    t0 = time.perf_counter()
    sent = 0
    while sent < n:
        k = min(batch, n - sent)
        items = [(f"k{(sent + i) % 5000}", json.dumps({"t": time.time(), "i": sent + i, "p": pad}).encode()) for i in range(k)]
        await q.publish_batch(topic, items)
        sent += k
    t_pub = time.perf_counter() - t0
    await asyncio.wait_for(done.wait(), timeout=300)
    t_total = t_last_consume - t0
    for c in cons:
        c.cancel()
    await q.stop()
    return {
        "backend": backend_name,
        "messages": n,
        "msg_bytes": size,
        "consumers": consumers,
        "publish_msgs_per_s": round(n / t_pub),
        "end_to_end_msgs_per_s": round(n / t_total),
        "latency_ms_p50": round(pct(latencies, 0.50) * 1000, 1),
        "latency_ms_p95": round(pct(latencies, 0.95) * 1000, 1),
        "latency_ms_p99": round(pct(latencies, 0.99) * 1000, 1),
        "latency_ms_mean": round(statistics.mean(latencies) * 1000, 1),
    }


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=["memory", "kafka", "pulsar"])
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--size", type=int, default=300, help="approx message bytes (a real job is ~250)")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--consumers", type=int, default=4)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    res = asyncio.run(run(a.backend, a.n, a.size, a.batch, a.consumers))
    print(json.dumps(res, indent=2))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=2)


if __name__ == "__main__":
    main()
