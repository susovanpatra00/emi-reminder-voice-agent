"""Mock borrower data and the 'today' the demo world runs on."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "borrowers.json"

# The mock data is written against a fixed call date so evals are reproducible
# ("ten days overdue" stays true). Override with CALL_DATE=YYYY-MM-DD.
DEFAULT_CALL_DATE = "2026-10-30"


def call_date() -> date:
    return date.fromisoformat(os.getenv("CALL_DATE", DEFAULT_CALL_DATE))


@dataclass(frozen=True)
class Borrower:
    borrower_id: str
    name: str
    gender: str
    dob: date
    phone_last4: str
    loan_type: str
    loan_account_last4: str
    emi_amount: int
    due_date: date
    days_overdue: int
    overdue_amount: int
    late_fee: int
    preferred_language: str

    @property
    def first_name(self) -> str:
        return self.name.split()[0]

    @property
    def is_overdue(self) -> bool:
        return self.days_overdue > 0


def load_borrowers(path: Path = DATA_PATH) -> dict[str, Borrower]:
    rows = json.loads(path.read_text())
    out = {}
    for r in rows:
        r = dict(r)
        r["dob"] = date.fromisoformat(r["dob"])
        r["due_date"] = date.fromisoformat(r["due_date"])
        out[r["borrower_id"]] = Borrower(**r)
    return out


def get_borrower(borrower_id: str) -> Borrower:
    borrowers = load_borrowers()
    if borrower_id not in borrowers:
        raise KeyError(f"Unknown borrower {borrower_id}. Known: {', '.join(borrowers)}")
    return borrowers[borrower_id]
