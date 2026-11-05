"""Queue abstraction: one interface, three backends (memory for tests, Kafka, Pulsar)."""
from __future__ import annotations

from .base import Delivery, QueueBackend, Consumer  # noqa: F401


def make_backend(name: str, *, kafka_bootstrap: str = "", pulsar_url: str = "", partitions: int = 8) -> QueueBackend:
    name = name.lower()
    if name == "memory":
        from .memory import MemoryBackend

        return MemoryBackend()
    if name == "kafka":
        from .kafka import KafkaBackend

        return KafkaBackend(kafka_bootstrap, partitions=partitions)
    if name == "pulsar":
        from .pulsar import PulsarBackend

        return PulsarBackend(pulsar_url)
    raise ValueError(f"unknown queue backend: {name!r} (use memory | kafka | pulsar)")
