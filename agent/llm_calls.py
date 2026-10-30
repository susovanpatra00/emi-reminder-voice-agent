"""Anthropic calls with thinking turned off.

Current Claude models may think before answering by default. For a phone agent that is
the wrong trade: it adds latency, and with a small max_tokens the thinking can use up the
whole budget so the reply comes back EMPTY (that is what caused the silent turns and the
simulated borrower's endless "Hello?" in the evals). The voice bot gets the same effect
from Pipecat, which sends the lowest thinking setting for known models.

If a model refuses to turn thinking off, fall back to the lowest effort and more room.
"""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic, BadRequestError

_NO_DISABLE: set[str] = set()  # models that rejected thinking={"type": "disabled"}


async def create_message(client: AsyncAnthropic, **kw: Any):
    model = kw["model"]
    if model not in _NO_DISABLE:
        try:
            return await client.messages.create(thinking={"type": "disabled"}, **kw)
        except BadRequestError as e:
            if "thinking" not in str(e).lower():
                raise
            _NO_DISABLE.add(model)
    output_config = dict(kw.pop("output_config", None) or {})
    output_config.setdefault("effort", "low")
    kw["max_tokens"] = max(kw.get("max_tokens", 0), 4000)  # leave room for thinking + reply
    return await client.messages.create(output_config=output_config, **kw)


def reply_text(resp) -> str:
    return " ".join(b.text for b in resp.content if b.type == "text").strip()


def why_empty(resp) -> str:
    """Short description of an empty reply, for logs."""
    kinds = ",".join(b.type for b in resp.content) or "no blocks"
    return f"stop_reason={resp.stop_reason} blocks={kinds}"
