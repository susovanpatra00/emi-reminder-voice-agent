"""Per-call state and tool logic, shared by the voice bot and the text evals.

The important guardrails live here in code, not only in the prompt:

* Loan details are not in the system prompt. The only way the model learns
  the amount or due date is a successful ``verify_identity`` call, so it
  cannot leak them to an unverified person even if it wanted to.
* Every tool that touches the loan refuses until the caller is verified.
* Hardship and dispute outcomes always set ``escalate_to_human``, whatever the
  model remembers to do.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .borrowers import Borrower, call_date
from .speech import extract_digits, rupees, spell_digits, spoken_date, spoken_days

MAX_VERIFY_ATTEMPTS = 2

OUTCOMES = [
    "already_paid",
    "will_pay_on_time",
    "promise_to_pay",
    "hardship",
    "dispute",
    "wrong_number",
    "third_party_answered",
    "callback_requested",
    "refused_or_stop_requested",
    "verification_failed",
    "no_response",
    "call_dropped",
]

OUTCOMES_DIR = Path(__file__).resolve().parent.parent / "outcomes"


@dataclass
class OutcomeRecord:
    borrower_id: str
    verified: bool = False
    outcome: str | None = None
    promise_to_pay_date: str | None = None
    payment_link_sent: bool = False
    escalate_to_human: bool = False
    escalation_reason: str | None = None
    callback_time: str | None = None
    language: str = "english"
    notes: str = ""
    call_date: str = ""
    duration_secs: float | None = None


class CallSession:
    def __init__(self, borrower: Borrower, *, link_failure_mode: str | None = None, link_delay_secs: float | None = None):
        self.borrower = borrower
        self.today: date = call_date()
        self.verify_attempts = 0
        self.language = borrower.preferred_language
        self.record = OutcomeRecord(
            borrower_id=borrower.borrower_id,
            language=self.language,
            call_date=self.today.isoformat(),
        )
        self.ended = False
        self.closing_line: str | None = None
        self.tool_log: list[dict[str, Any]] = []
        self._link_attempts = 0
        self._started = time.time()
        # "never" | "always" | "first" (fails once, then works) | "random"
        self.link_failure_mode = link_failure_mode or os.getenv("MOCK_LINK_FAILURE", "never")
        self.link_delay_secs = (
            link_delay_secs if link_delay_secs is not None else float(os.getenv("MOCK_LINK_DELAY", "2.0"))
        )

    # ------------------------------------------------------------------ facts

    @property
    def verified(self) -> bool:
        return self.record.verified

    def loan_facts(self) -> dict[str, str]:
        """Loan details, already in spoken form. Only returned after verification."""
        b = self.borrower
        facts = {
            "loan": b.loan_type,
            # Only needed if they ask which loan; mentioning it unprompted makes the turn long.
            "account_ending_if_asked": spell_digits(b.loan_account_last4),
            "emi_amount": rupees(b.emi_amount),
            "due_date": spoken_date(b.due_date, self.today),
        }
        if b.is_overdue:
            facts["status"] = f"overdue by {spoken_days(b.days_overdue)}"
            facts["total_overdue_amount"] = rupees(b.overdue_amount)
            if b.overdue_amount > b.emi_amount:
                facts["note"] = "more than one EMI is pending"
            if b.late_fee:
                facts["late_fee_already_charged"] = rupees(b.late_fee)
        else:
            days_left = (b.due_date - self.today).days
            facts["status"] = f"due in {spoken_days(days_left)}" if days_left > 1 else "due very soon"
        return facts

    # ------------------------------------------------------------------ tools

    async def handle(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            result = {"error": f"unknown tool {name}"}
        else:
            try:
                result = await handler(**args)
            except TypeError as e:  # bad arguments from the model
                result = {"error": f"bad arguments: {e}"}
        self.tool_log.append({"t": round(time.time() - self._started, 2), "tool": name, "args": args, "result": result})
        return result

    def _needs_verification(self) -> dict[str, Any] | None:
        if not self.verified:
            return {
                "error": "not_verified",
                "instruction": "Do not share or act on any loan details. Verify identity first.",
            }
        return None

    async def _tool_verify_identity(self, method: str, value: str) -> dict[str, Any]:
        if self.verified:
            return {"verified": True, "loan": self.loan_facts()}
        if self.verify_attempts >= MAX_VERIFY_ATTEMPTS:
            return {"verified": False, "attempts_left": 0, "instruction": "Verification locked. Close politely without any loan details."}

        self.verify_attempts += 1
        ok = False
        if method == "phone_last4":
            digits = extract_digits(value)
            ok = len(digits) >= 4 and digits.endswith(self.borrower.phone_last4)
        elif method == "dob":
            try:
                ok = date.fromisoformat(value.strip()) == self.borrower.dob
            except ValueError:
                self.verify_attempts -= 1  # our formatting problem, not the caller's mistake
                return {"verified": False, "error": "dob must be YYYY-MM-DD; ask them to repeat it"}
        else:
            return {"error": "method must be phone_last4 or dob"}

        if ok:
            self.record.verified = True
            return {
                "verified": True,
                "loan": self.loan_facts(),
                "instruction": "Don't say they are verified. Go straight to the reminder: amount and date in one short sentence, then a short question.",
            }

        left = MAX_VERIFY_ATTEMPTS - self.verify_attempts
        if left <= 0:
            self.record.outcome = "verification_failed"
            return {
                "verified": False,
                "attempts_left": 0,
                "instruction": "Do not reveal anything about the loan. Say you can't discuss the account without verification, "
                "suggest they call Lumen Finance customer care, and end the call politely.",
            }
        return {
            "verified": False,
            "attempts_left": left,
            "instruction": "Don't say what was wrong. Ask once more, or offer the other method (date of birth or last four digits).",
        }

    async def _tool_send_payment_link(self, channel: str = "sms") -> dict[str, Any]:
        if err := self._needs_verification():
            return err
        self._link_attempts += 1
        await asyncio.sleep(self.link_delay_secs)  # pretend this is a slow API
        fail = (
            self.link_failure_mode == "always"
            or (self.link_failure_mode == "first" and self._link_attempts == 1)
            or (self.link_failure_mode == "random" and random.random() < 0.3)
        )
        if fail:
            return {
                "sent": False,
                "error": "gateway_timeout",
                "instruction": "Apologise briefly. Offer to try once more, or tell them they can pay in the Lumen app.",
            }
        self.record.payment_link_sent = True
        return {"sent": True, "channel": channel, "to": f"the number ending {spell_digits(self.borrower.phone_last4)}"}

    async def _tool_record_promise_to_pay(self, date_iso: str) -> dict[str, Any]:
        if err := self._needs_verification():
            return err
        try:
            d = date.fromisoformat(date_iso.strip())
        except ValueError:
            return {"error": "date must be YYYY-MM-DD"}
        if d < self.today:
            return {"error": "date_in_past", "instruction": "That date has passed. Gently confirm which date they meant."}
        self.record.promise_to_pay_date = d.isoformat()
        result: dict[str, Any] = {
            "recorded": True,
            "read_back": spoken_date(d, self.today, with_weekday=True),
            "instruction": "Confirm the date back to them in a few words.",
        }
        if (d - self.today).days > 30:
            self._escalate("promise date more than 30 days away")
            result["note"] = "Far-off date. Accept it, don't argue. A colleague will review it."
        return result

    async def _tool_schedule_callback(self, when: str) -> dict[str, Any]:
        self.record.callback_time = when
        return {"scheduled": True, "when": when}

    async def _tool_flag_for_human(self, reason: str, details: str = "") -> dict[str, Any]:
        self._escalate(f"{reason}: {details}".strip(": "))
        return {"flagged": True, "instruction": "Tell them a colleague will call them back. Promise nothing else."}

    async def _tool_set_language(self, language: str) -> dict[str, Any]:
        language = language.lower().strip()
        if language not in ("english", "hindi"):
            return {"error": "only english and hindi are supported", "instruction": "Apologise and continue in English."}
        self.language = language
        self.record.language = language
        return {"language": language, "instruction": f"Continue entirely in {language}."}

    async def _tool_end_call(self, outcome: str, closing_line: str, notes: str = "") -> dict[str, Any]:
        if outcome not in OUTCOMES:
            return {"error": f"outcome must be one of {OUTCOMES}"}
        if not self.verified and outcome in ("already_paid", "will_pay_on_time", "promise_to_pay", "hardship", "dispute"):
            # These outcomes only make sense for the verified borrower.
            outcome = "verification_failed" if self.verify_attempts else outcome
        self.record.outcome = outcome
        self.record.notes = notes
        if outcome in ("hardship", "dispute"):
            self._escalate(outcome)
        self.closing_line = closing_line
        self.ended = True
        return {"ended": True}

    # ---------------------------------------------------------------- helpers

    def _escalate(self, reason: str) -> None:
        self.record.escalate_to_human = True
        if self.record.escalation_reason:
            if reason not in self.record.escalation_reason:
                self.record.escalation_reason += f"; {reason}"
        else:
            self.record.escalation_reason = reason

    def finalize(self, default_outcome: str = "call_dropped") -> OutcomeRecord:
        if not self.record.outcome:
            self.record.outcome = default_outcome
        self.record.duration_secs = round(time.time() - self._started, 1)
        return self.record

    def save(self, directory: Path = OUTCOMES_DIR) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.borrower.borrower_id}_{int(self._started)}.json"
        path.write_text(json.dumps({"record": asdict(self.record), "tool_log": self.tool_log}, indent=2, ensure_ascii=False))
        return path


_GOODBYE_WORDS = (
    "bye", "good day", "good evening", "good night", "take care", "have a great", "have a nice",
    "safe drive", "drive safe",
    "अलविदा", "शुभ", "नमस्ते", "ध्यान रखिए", "ध्यान रखें",
)


def says_goodbye(text: str) -> bool:
    """True if the text already contains a goodbye, so the closing line would repeat it."""
    t = text.lower()
    return any(w in t for w in _GOODBYE_WORDS)


def upcoming_calendar(today: date, days: int = 21) -> str:
    """A small calendar so the model can turn 'next Friday' into a real date."""
    lines = []
    for i in range(days):
        d = today + timedelta(days=i)
        tag = " (today)" if i == 0 else " (tomorrow)" if i == 1 else ""
        lines.append(f"{d.strftime('%a')} {d.isoformat()}{tag}")
    return "\n".join(lines)
