"""Kafka backend (aiokafka).

Semantics we depend on
  * key -> partition, so parallelism = number of partitions (consumers beyond that sit idle).
  * Acks are OFFSET COMMITS, not per-message. Workers finish jobs out of order, so we only commit
    up to the lowest offset that is still in flight (per partition). A crash can therefore replay a
    few already-finished jobs; the Redis claim + job_id idempotency make that harmless.
"""
from __future__ import annotations

import asyncio

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from .base import Consumer, Delivery, QueueBackend


class KafkaConsumer(Consumer):
    def __init__(self, consumer: AIOKafkaConsumer):
        self._c = consumer
        self._inflight: dict[TopicPartition, set[int]] = {}
        self._max_seen: dict[TopicPartition, int] = {}

    async def poll(self, max_n: int, timeout_s: float) -> list[Delivery]:
        data = await self._c.getmany(timeout_ms=int(timeout_s * 1000), max_records=max_n)
        out: list[Delivery] = []
        for tp, msgs in data.items():
            for m in msgs:
                self._inflight.setdefault(tp, set()).add(m.offset)
                self._max_seen[tp] = max(self._max_seen.get(tp, -1), m.offset)
                out.append(Delivery(m.key.decode() if m.key else None, m.value, (tp, m.offset)))
        return out

    async def ack(self, deliveries: list[Delivery]) -> None:
        touched: set[TopicPartition] = set()
        for d in deliveries:
            tp, off = d.receipt
            self._inflight[tp].discard(off)
            touched.add(tp)
        commit: dict[TopicPartition, int] = {}
        for tp in touched:
            pending = self._inflight[tp]
            commit[tp] = (min(pending) if pending else self._max_seen[tp] + 1)
        if commit:
            await self._c.commit(commit)

    async def close(self) -> None:
        await self._c.stop()


class KafkaBackend(QueueBackend):
    name = "kafka"

    def __init__(self, bootstrap: str, partitions: int = 8):
        self.bootstrap = bootstrap
        self.partitions = partitions
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            acks="all",
            enable_idempotence=True,
            linger_ms=5,
            max_batch_size=256 * 1024,
        )
        await self._producer.start()

    async def stop(self) -> None:
        if self._producer:
            await self._producer.stop()

    async def ensure_topic(self, topic: str, groups: tuple[str, ...] = ()) -> None:
        admin = AIOKafkaAdminClient(bootstrap_servers=self.bootstrap)
        await admin.start()
        try:
            await admin.create_topics([NewTopic(topic, num_partitions=self.partitions, replication_factor=1)])
        except TopicAlreadyExistsError:
            pass
        finally:
            await admin.close()

    async def publish(self, topic: str, key: str, value: bytes) -> None:
        await self._producer.send_and_wait(topic, value=value, key=key.encode())

    async def publish_batch(self, topic: str, items: list[tuple[str, bytes]]) -> None:
        futs = [await self._producer.send(topic, value=v, key=k.encode()) for k, v in items]
        await asyncio.gather(*futs)  # each future resolves when the broker acked (acks=all)

    async def subscribe(self, topic: str, group: str) -> Consumer:
        c = AIOKafkaConsumer(
            topic,
            bootstrap_servers=self.bootstrap,
            group_id=group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await c.start()
        return KafkaConsumer(c)
