"""The thing that actually places a call. The worker only sees this interface.

SimulatedCaller is what the load/skip tests use (no audio, no STT/LLM/TTS, no cost).
A real implementation would start the Pipecat bot for job["phone"] and return the outcome
recorded by CallSession; nothing else in the scale layer changes.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Protocol


@dataclass
class CallResult:
    outcome: str
    detail: str = ""


class Caller(Protocol):
    async def call(self, job: dict) -> CallResult: ...


class SimulatedCaller:
    OUTCOMES = [("promised_to_pay", 0.45), ("already_paid_claimed", 0.1), ("no_answer", 0.25), ("callback_requested", 0.1), ("escalated", 0.1)]

    def __init__(self, duration_s: float = 0.0, seed: int | None = None):
        self.duration_s = duration_s
        self.calls: list[str] = []  # job_ids actually dialed (tests assert on this)
        self._rng = random.Random(seed)

    async def call(self, job: dict) -> CallResult:
        self.calls.append(job["job_id"])
        if self.duration_s:
            await asyncio.sleep(self.duration_s)
        names, weights = zip(*self.OUTCOMES)
        return CallResult(self._rng.choices(names, weights)[0])
