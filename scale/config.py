"""All settings come from environment variables (put them in .env, never in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("SCALE_DB_PATH", "scale.db")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    queue_backend: str = os.getenv("QUEUE_BACKEND", "memory")  # memory | kafka | pulsar
    kafka_bootstrap: str = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
    pulsar_url: str = os.getenv("PULSAR_URL", "pulsar://localhost:6650")
    jobs_topic: str = os.getenv("JOBS_TOPIC", "emi-call-jobs")
    results_topic: str = os.getenv("RESULTS_TOPIC", "emi-call-results")
    dlq_topic: str = os.getenv("DLQ_TOPIC", "emi-call-dlq")
    topic_partitions: int = _int("TOPIC_PARTITIONS", 8)
    group: str = os.getenv("CONSUMER_GROUP", "emi-workers")
    daily_call_cap: int = _int("DAILY_CALL_CAP", 3)
    worker_concurrency: int = _int("WORKER_CONCURRENCY", 50)
    claim_ttl_s: int = _int("CLAIM_TTL_S", 900)
    paid_ttl_s: int = _int("PAID_TTL_S", 7 * 86400)
    unpaid_ttl_s: int = _int("UNPAID_TTL_S", 3600)
    # Shared secret the payment provider uses to sign webhooks. Must be set in .env.
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")


def get_settings() -> Settings:
    return Settings()
