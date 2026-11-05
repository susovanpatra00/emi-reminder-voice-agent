import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import os
import shutil
import subprocess
import time
import uuid
from datetime import date

import pytest
import pytest_asyncio
import redis as redis_sync

from scale.cache import Cache
from scale.db import Db
from scale.queues import make_backend

TODAY = date(2026, 11, 5)
REDIS_PORT = 6391


@pytest.fixture(scope="session")
def redis_url():
    """A throwaway REAL redis-server (not a mock). Falls back to REDIS_TEST_URL if given."""
    if os.getenv("REDIS_TEST_URL"):
        yield os.environ["REDIS_TEST_URL"]
        return
    exe = shutil.which("redis-server")
    if not exe:
        pytest.skip("redis-server not installed and REDIS_TEST_URL not set")
    proc = subprocess.Popen(
        [exe, "--port", str(REDIS_PORT), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"redis://localhost:{REDIS_PORT}/0"
    for _ in range(50):
        try:
            redis_sync.Redis.from_url(url).ping()
            break
        except Exception:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest_asyncio.fixture
async def cache(redis_url):
    c = Cache.from_url(redis_url)
    await c.r.flushdb()
    yield c
    await c.r.flushdb()
    await c.close()


@pytest_asyncio.fixture
async def db(tmp_path):
    d = await Db(str(tmp_path / "t.db")).open()
    yield d
    await d.close()


async def seed_borrowers(db: Db, n: int, due: str = TODAY.isoformat()):
    """n borrowers, one EMI each, all due `due`. ids: b000.., e000.."""
    await db.seed(
        [(f"b{i:04d}", f"Borrower {i}", f"+9190000{i:05d}") for i in range(n)],
        [(f"e{i:04d}", f"b{i:04d}", 5000 + i, due) for i in range(n)],
    )


def backend_params():
    params = ["memory"]
    if os.getenv("KAFKA_BOOTSTRAP_TEST"):
        params.append("kafka")
    if os.getenv("PULSAR_URL_TEST"):
        params.append("pulsar")
    return params


@pytest_asyncio.fixture(params=backend_params())
async def queue(request):
    """Every pipeline test runs on each configured backend. Kafka/Pulsar only run if their env var is set."""
    q = make_backend(
        request.param,
        kafka_bootstrap=os.getenv("KAFKA_BOOTSTRAP_TEST", ""),
        pulsar_url=os.getenv("PULSAR_URL_TEST", ""),
        partitions=4,
    )
    await q.start()
    yield q
    await q.stop()


@pytest.fixture
def topics():
    s = uuid.uuid4().hex[:8]  # unique per test so reruns on a real broker don't see old messages
    return {"jobs": f"jobs-{s}", "results": f"results-{s}", "dlq": f"dlq-{s}", "group": f"g-{s}", "rgroup": f"rg-{s}"}
