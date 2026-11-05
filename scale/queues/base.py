"""The only things the scheduler / worker / results writer know about a queue."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Delivery:
    key: Optional[str]
    value: bytes
    receipt: Any = None  # backend-specific handle used by ack()


class Consumer(ABC):
    @abstractmethod
    async def poll(self, max_n: int, timeout_s: float) -> list[Delivery]:
        """Return up to max_n messages; wait at most timeout_s for the first one."""

    @abstractmethod
    async def ack(self, deliveries: list[Delivery]) -> None:
        """Mark messages as processed. Unacked messages may be redelivered (at-least-once)."""

    @abstractmethod
    async def close(self) -> None: ...


class QueueBackend(ABC):
    name = "base"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def ensure_topic(self, topic: str, groups: tuple[str, ...] = ()) -> None:
        """Create the topic (and, where needed, the durable subscriptions in `groups`)."""

    @abstractmethod
    async def publish(self, topic: str, key: str, value: bytes) -> None: ...

    @abstractmethod
    async def publish_batch(self, topic: str, items: list[tuple[str, bytes]]) -> None:
        """Publish many (key, value) pairs; return only when all are durably accepted."""

    @abstractmethod
    async def subscribe(self, topic: str, group: str) -> Consumer: ...
