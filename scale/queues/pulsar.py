"""Pulsar backend (pulsar-client). The client is blocking, so calls run in worker threads.

Semantics we depend on
  * Shared subscription: any number of consumers split one topic, no partition limit on parallelism.
  * Acks are PER MESSAGE, so out-of-order completion is natural (the thing Kafka makes us emulate).
"""
from __future__ import annotations

import asyncio

import pulsar

from .base import Consumer, Delivery, QueueBackend


def _fq(topic: str) -> str:
    return topic if "://" in topic else f"persistent://public/default/{topic}"


class PulsarConsumer(Consumer):
    def __init__(self, consumer: "pulsar.Consumer"):
        self._c = consumer

    def _poll_blocking(self, max_n: int, timeout_s: float) -> list[Delivery]:
        out: list[Delivery] = []
        wait_ms = max(1, int(timeout_s * 1000))
        while len(out) < max_n:
            try:
                m = self._c.receive(timeout_millis=wait_ms)
            except pulsar.Timeout:
                break
            out.append(Delivery(m.partition_key() or None, m.data(), m))
            wait_ms = 5  # got one; only drain what is already waiting
        return out

    async def poll(self, max_n: int, timeout_s: float) -> list[Delivery]:
        return await asyncio.to_thread(self._poll_blocking, max_n, timeout_s)

    async def ack(self, deliveries: list[Delivery]) -> None:
        def _ack():
            for d in deliveries:
                self._c.acknowledge(d.receipt)

        await asyncio.to_thread(_ack)

    async def close(self) -> None:
        await asyncio.to_thread(self._c.close)


class PulsarBackend(QueueBackend):
    name = "pulsar"

    def __init__(self, service_url: str):
        self.url = service_url
        self._client: pulsar.Client | None = None
        self._producers: dict[str, "pulsar.Producer"] = {}

    async def start(self) -> None:
        self._client = await asyncio.to_thread(pulsar.Client, self.url)

    async def stop(self) -> None:
        for p in self._producers.values():
            await asyncio.to_thread(p.flush)
            await asyncio.to_thread(p.close)
        if self._client:
            await asyncio.to_thread(self._client.close)

    async def ensure_topic(self, topic: str, groups: tuple[str, ...] = ()) -> None:
        # Topics auto-create on first use. A subscription must exist BEFORE publishing, otherwise
        # Pulsar may drop messages nobody subscribed to. Subscribing then closing keeps it durable.
        await self._producer(topic)
        for g in groups:
            c = await self.subscribe(topic, g)
            await c.close()

    async def _producer(self, topic: str) -> "pulsar.Producer":
        p = self._producers.get(topic)
        if p is None:
            p = await asyncio.to_thread(
                self._client.create_producer,
                _fq(topic),
                batching_enabled=True,
                batching_max_publish_delay_ms=5,
                block_if_queue_full=True,
                max_pending_messages=10000,
            )
            self._producers[topic] = p
        return p

    async def publish(self, topic: str, key: str, value: bytes) -> None:
        p = await self._producer(topic)
        await asyncio.to_thread(p.send, value, partition_key=key)

    async def publish_batch(self, topic: str, items: list[tuple[str, bytes]]) -> None:
        p = await self._producer(topic)
        loop = asyncio.get_running_loop()
        done: asyncio.Future = loop.create_future()
        state = {"left": len(items), "err": None}

        def cb(res, _msg_id):
            if res != pulsar.Result.Ok and state["err"] is None:
                state["err"] = res
            state["left"] -= 1
            if state["left"] == 0:
                loop.call_soon_threadsafe(done.set_result, None)

        def _send_all():
            for k, v in items:
                p.send_async(v, cb, partition_key=k)
            p.flush()

        if not items:
            return
        await asyncio.to_thread(_send_all)
        await done
        if state["err"] is not None:
            raise RuntimeError(f"pulsar publish failed: {state['err']}")

    async def subscribe(self, topic: str, group: str) -> Consumer:
        c = await asyncio.to_thread(
            self._client.subscribe,
            _fq(topic),
            group,
            consumer_type=pulsar.ConsumerType.Shared,
            initial_position=pulsar.InitialPosition.Earliest,
            receiver_queue_size=1000,
        )
        return PulsarConsumer(c)
