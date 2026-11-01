"""Stops the model's private reasoning from being spoken.

In a voice test the model once wrote its thinking as reply text before a tool call:
"The caller said they are Priya and spoke Hindi-ish... Should I switch to Hindi? ...
so yes, call set_language." Everything the LLM writes goes to the voice, so the caller
heard all of it. The prompt forbids this, but a prompt can't guarantee it.

This processor sits between the LLM and the TTS. It holds text until a sentence ends
(the TTS waits for whole sentences anyway, so this adds no real delay), and if a
sentence reads like notes about the call rather than words to the caller, it drops that
sentence and the rest of that reply.
"""

from __future__ import annotations

import re

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Sentences that talk ABOUT the call instead of TO the caller.
META = re.compile(
    r"\bthe (caller|user|borrower|customer|person) (said|says|is|was|spoke|speaks|confirmed|"
    r"wants|asked|gave|has|did|didn't|seems)\b"
    r"|\b(set_language|verify_identity|send_payment_link|record_promise_to_pay|"
    r"schedule_callback|flag_for_human|end_call)\b"
    r"|\b(move|go|proceed) (on )?to step\b|\bstep \d\b"
    r"|\b(system prompt|my instructions|the instructions)\b",
    re.IGNORECASE,
)

# A sentence ends at . ! ? or the Devanagari danda, followed by whitespace.
SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")


def is_meta(sentence: str) -> bool:
    return bool(META.search(sentence))


class SpokenTextFilter(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._buffer = ""
        self._suppress = False
        self.dropped: list[str] = []

    async def _emit(self, text: str, direction: FrameDirection):
        if not text.strip():
            return
        if self._suppress or is_meta(text):
            if not self._suppress:
                logger.warning(f"Dropped reasoning text instead of speaking it: {text.strip()!r}")
            self._suppress = True
            self.dropped.append(text.strip())
            return
        await self.push_frame(LLMTextFrame(text), direction)

    async def _flush(self, direction: FrameDirection):
        text, self._buffer = self._buffer, ""
        await self._emit(text, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            self._buffer += frame.text
            parts = SENTENCE_END.split(self._buffer)
            # Everything except the last part is a complete sentence.
            for sentence in parts[:-1]:
                await self._emit(sentence + " ", direction)
            self._buffer = parts[-1]
            return

        if isinstance(frame, InterruptionFrame):
            self._buffer = ""
            self._suppress = False
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._suppress = False
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._flush(direction)
            self._suppress = False
        elif direction == FrameDirection.DOWNSTREAM and self._buffer:
            # Anything else going downstream (e.g. a function-call frame): speak what we
            # have so far first, so order is preserved.
            await self._flush(direction)

        await self.push_frame(frame, direction)
