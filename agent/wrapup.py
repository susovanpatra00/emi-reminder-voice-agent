"""Post-call wrap-up for calls where the caller hung up before the agent closed.

Without this, any call the borrower ends first is recorded as "call_dropped", even if
they clearly said they'd pay. A real collections system still needs the outcome, so a
quick LLM pass reads the transcript and picks one. Facts the tools already recorded
(verification, promise date, link sent, escalation) are kept as they are.
"""

from __future__ import annotations

import json
import os

from anthropic import AsyncAnthropic

from .call_session import OUTCOMES, CallSession
from .llm_calls import create_message

WRAPUP_PROMPT = """\
This is a transcript of an EMI reminder call from Lumen Finance. The caller hung up before the \
agent formally closed the call.

Facts recorded during the call: verified={verified}, promise_to_pay_date={ptp}, \
payment_link_sent={link}, escalated={esc}.

Transcript:
{transcript}

Today is {today}. Pick the single outcome that best describes the call, and write a one-line \
note for the collections team. If the borrower committed to a specific payment date that was \
not recorded, give it as promise_to_pay_date (YYYY-MM-DD); otherwise use an empty string. Only use "call_dropped" if the call ended before the borrower said what they \
would do."""

SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": OUTCOMES},
        "notes": {"type": "string"},
        "promise_to_pay_date": {"type": "string"},
    },
    "required": ["outcome", "notes", "promise_to_pay_date"],
    "additionalProperties": False,
}


async def wrap_up(session: CallSession, transcript: list[tuple[str, str]], client: AsyncAnthropic | None = None) -> None:
    """Fill in outcome and notes from the transcript. transcript = [(speaker, text), ...]."""
    rec = session.record
    text = "\n".join(f"{who.upper()}: {said}" for who, said in transcript if said)
    try:
        client = client or AsyncAnthropic()
        resp = await create_message(
            client,
            model=os.getenv("LLM_MODEL", "claude-haiku-5-5"),
            max_tokens=600,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{
                "role": "user",
                "content": WRAPUP_PROMPT.format(
                    verified=rec.verified, ptp=rec.promise_to_pay_date,
                    link=rec.payment_link_sent, esc=rec.escalate_to_human, transcript=text,
                    today=session.today.strftime("%A %Y-%m-%d"),
                ),
            }],
        )
        data = json.loads(" ".join(b.text for b in resp.content if b.type == "text"))
        outcome = data["outcome"]
        notes = data.get("notes", "")
        ptp = (data.get("promise_to_pay_date") or "").strip()
    except Exception as e:  # never lose the record because the wrap-up failed
        outcome, notes, ptp = "call_dropped", f"wrap-up failed: {type(e).__name__}", ""

    # The verification gate still applies: an unverified call can't have a loan outcome.
    if not rec.verified and outcome in ("already_paid", "will_pay_on_time", "promise_to_pay", "hardship", "dispute"):
        outcome = "verification_failed" if session.verify_attempts else "call_dropped"
    rec.outcome = outcome
    if ptp and rec.verified and not rec.promise_to_pay_date:
        try:
            from datetime import date

            d = date.fromisoformat(ptp)
            if d >= session.today:
                rec.promise_to_pay_date = d.isoformat()
                notes += " (date taken from transcript, not confirmed back to the borrower)"
        except ValueError:
            pass
    rec.notes = f"[caller hung up first] {notes}".strip()
    if outcome in ("hardship", "dispute"):
        session._escalate(outcome)
