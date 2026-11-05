"""Quick in-process load run: scheduler -> memory queue -> worker (real Redis) -> results writer."""
import asyncio, tempfile, time, os
from datetime import date
from scale.db import Db
from scale.cache import Cache
from scale.caller import SimulatedCaller
from scale.queues import make_backend
from scale.scheduler import schedule_due
from scale.worker import Worker
from scale.results_writer import run_results_writer
TODAY = date(2026, 11, 5)
async def main():
    N = 20000
    db = await Db(os.path.join(tempfile.mkdtemp(), "b.db")).open()
    await db.seed([(f"b{i}", f"n{i}", "+91") for i in range(N)], [(f"e{i}", f"b{i}", 5000, TODAY.isoformat()) for i in range(N)])
    cache = Cache.from_url("redis://localhost:6391/0", max_connections=1000); await cache.r.flushdb()
    q = make_backend("memory"); await q.start()
    t = time.perf_counter(); n = await schedule_due(db, q, "j", today=TODAY); ts = time.perf_counter() - t
    for i in range(0, N, 10): await cache.mark_paid(f"e{i}")
    caller = SimulatedCaller(duration_s=0.02)
    w = Worker(queue=q, db=db, cache=cache, caller=caller, jobs_topic="j", results_topic="r", dlq_topic="d", group="g", concurrency=500, today_fn=lambda: TODAY.isoformat())
    t = time.perf_counter(); await w.run(asyncio.Event(), poll_timeout_s=0.2, idle_exit_s=0.6); tw = time.perf_counter() - t - 0.6
    wr = await run_results_writer(q, db, "r", "rg", asyncio.Event(), max_wait_s=0.2, idle_exit_s=0.5)
    print("scheduled", n, f"{ts:.2f}s ({n/ts:.0f}/s)")
    print("worker", dict(w.stats), f"~{tw:.1f}s ({N/tw:.0f} jobs/s)", "dialed", len(caller.calls))
    print("results written", wr, await db.outcome_counts())
    await cache.close(); await db.close()
asyncio.run(main())
