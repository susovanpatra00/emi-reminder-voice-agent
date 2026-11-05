"""In-process queue for tests. Same semantics we rely on: groups share messages,
unacked messages are redelivered on close()."""
from __future__ import annotations

import asyncio
from collections import defaultdict

from .base import Consumer, Delivery, QueueBackend


class _Group:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()


class MemoryConsumer(Consumer):
    def __init__(self, group: _Group):
        self._g = group
        self._inflight: dict[int, Delivery] = {}
        self._seq = 0

    async def poll(self, max_n: int, timeout_s: float) -> list[Delivery]:
        out: list[Delivery] = []
        try:
            first = await asyncio.wait_for(self._g.q.get(), timeout_s)
        except asyncio.TimeoutError:
            return out
        out.append(first)
        while len(out) < max_n and not self._g.q.empty():
            out.append(self._g.q.get_nowait())
        for d in out:
            self._seq += 1
            d.receipt = self._seq
            self._inflight[self._seq] = d
        return out

    async def ack(self, deliveries: list[Delivery]) -> None:
        for d in deliveries:
            self._inflight.pop(d.receipt, None)

    async def close(self) -> None:
        for d in self._inflight.values():  # redeliver anything not acked
            self._g.q.put_nowait(Delivery(d.key, d.value))
        self._inflight.clear()


class MemoryBackend(QueueBackend):
    name = "memory"

    def __init__(self):
        self._groups: dict[str, dict[str, _Group]] = defaultdict(dict)  # topic -> group -> _Group
        self._backlog: dict[str, list[Delivery]] = defaultdict(list)  # published before any group existed

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def ensure_topic(self, topic: str, groups: tuple[str, ...] = ()) -> None: ...

    async def publish(self, topic: str, key: str, value: bytes) -> None:
        d = Delivery(key, value)
        groups = self._groups[topic]
        if not groups:
            self._backlog[topic].append(d)
        for g in groups.values():
            g.q.put_nowait(Delivery(key, value))

    async def publish_batch(self, topic: str, items: list[tuple[str, bytes]]) -> None:
        for k, v in items:
            await self.publish(topic, k, v)

    async def subscribe(self, topic: str, group: str) -> Consumer:
        g = self._groups[topic].get(group)
        if g is None:
            g = self._groups[topic][group] = _Group()
            for d in self._backlog.pop(topic, []):  # like Kafka "earliest" for a brand-new group
                g.q.put_nowait(d)
        return MemoryConsumer(g)
