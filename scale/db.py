"""SQLite = the source of truth. (Swap for Postgres in production; the methods stay the same.)

Redis is only a cache of facts that live here. If Redis is wiped, nothing is lost.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS borrowers (
    borrower_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    opted_out   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS emis (
    emi_id      TEXT PRIMARY KEY,
    borrower_id TEXT NOT NULL REFERENCES borrowers(borrower_id),
    amount      INTEGER NOT NULL,
    due_date    TEXT NOT NULL,                 -- ISO date
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | paid
    paid_at     REAL,
    payment_id  TEXT
);
CREATE INDEX IF NOT EXISTS ix_emis_due ON emis(status, due_date, emi_id);
CREATE TABLE IF NOT EXISTS payments (
    payment_id  TEXT PRIMARY KEY,             -- idempotency key from the provider
    emi_id      TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS call_outcomes (
    job_id      TEXT PRIMARY KEY,             -- makes redelivered results harmless
    emi_id      TEXT NOT NULL,
    outcome     TEXT NOT NULL,                -- e.g. promised_to_pay, no_answer, skipped
    reason      TEXT,
    finished_at REAL NOT NULL
);
"""


@dataclass
class DueEmi:
    emi_id: str
    borrower_id: str
    name: str
    phone: str
    amount: int
    due_date: str


class Db:
    def __init__(self, path: str):
        self.path = path
        self._c: Optional[aiosqlite.Connection] = None
        self._wlock = asyncio.Lock()  # one connection: keep write transactions from interleaving

    async def open(self) -> "Db":
        self._c = await aiosqlite.connect(self.path)
        await self._c.execute("PRAGMA journal_mode=WAL")
        await self._c.execute("PRAGMA busy_timeout=5000")
        await self._c.executescript(SCHEMA)
        await self._c.commit()
        return self

    async def close(self) -> None:
        if self._c:
            await self._c.close()

    @property
    def c(self) -> aiosqlite.Connection:
        assert self._c is not None, "call open() first"
        return self._c

    # ---- seeding -----------------------------------------------------
    async def seed(self, borrowers: Iterable[tuple], emis: Iterable[tuple]) -> None:
        await self.c.executemany(
            "INSERT OR IGNORE INTO borrowers(borrower_id,name,phone) VALUES (?,?,?)", borrowers
        )
        await self.c.executemany(
            "INSERT OR IGNORE INTO emis(emi_id,borrower_id,amount,due_date) VALUES (?,?,?,?)", emis
        )
        await self.c.commit()

    # ---- scheduler: keyset pagination, never OFFSET ------------------
    async def due_emis(self, due_from: str, due_to: str, after_id: str = "", limit: int = 1000) -> list[DueEmi]:
        cur = await self.c.execute(
            """SELECT e.emi_id, e.borrower_id, b.name, b.phone, e.amount, e.due_date
               FROM emis e JOIN borrowers b USING (borrower_id)
               WHERE e.status='pending' AND b.opted_out=0
                 AND e.due_date BETWEEN ? AND ? AND e.emi_id > ?
               ORDER BY e.emi_id LIMIT ?""",
            (due_from, due_to, after_id, limit),
        )
        return [DueEmi(*r) for r in await cur.fetchall()]

    # ---- pre-dial fallbacks (used on a Redis miss) --------------------
    async def is_paid(self, emi_id: str) -> bool:
        cur = await self.c.execute("SELECT status FROM emis WHERE emi_id=?", (emi_id,))
        row = await cur.fetchone()
        return bool(row and row[0] == "paid")

    async def is_opted_out(self, borrower_id: str) -> bool:
        cur = await self.c.execute("SELECT opted_out FROM borrowers WHERE borrower_id=?", (borrower_id,))
        row = await cur.fetchone()
        return bool(row and row[0])

    async def set_opted_out(self, borrower_id: str, value: bool = True) -> bool:
        async with self._wlock:
            cur = await self.c.execute("UPDATE borrowers SET opted_out=? WHERE borrower_id=?", (int(value), borrower_id))
            await self.c.commit()
            return cur.rowcount > 0

    # ---- payment webhook ----------------------------------------------
    async def mark_paid(self, payment_id: str, emi_id: str, amount: int) -> str:
        """Returns 'applied' | 'duplicate' | 'unknown_emi'. Idempotent on payment_id."""
        async with self._wlock:
            return await self._mark_paid(payment_id, emi_id, amount)

    async def _mark_paid(self, payment_id: str, emi_id: str, amount: int) -> str:
        cur = await self.c.execute("SELECT 1 FROM emis WHERE emi_id=?", (emi_id,))
        if not await cur.fetchone():
            return "unknown_emi"
        now = time.time()
        try:
            await self.c.execute(
                "INSERT INTO payments(payment_id,emi_id,amount,received_at) VALUES (?,?,?,?)",
                (payment_id, emi_id, amount, now),
            )
        except sqlite3.IntegrityError:
            await self.c.rollback()
            return "duplicate"
        await self.c.execute(
            "UPDATE emis SET status='paid', paid_at=?, payment_id=? WHERE emi_id=? AND status!='paid'",
            (now, payment_id, emi_id),
        )
        await self.c.commit()
        return "applied"

    # ---- results: batch insert, idempotent on job_id --------------------
    async def insert_outcomes(self, rows: list[tuple]) -> int:
        """rows: (job_id, emi_id, outcome, reason, finished_at). Returns rows actually inserted."""
        async with self._wlock:
            before = self.c.total_changes
            await self.c.executemany(
                "INSERT OR IGNORE INTO call_outcomes(job_id,emi_id,outcome,reason,finished_at) VALUES (?,?,?,?,?)", rows
            )
            await self.c.commit()
            return self.c.total_changes - before

    async def outcome_counts(self) -> dict[str, int]:
        cur = await self.c.execute("SELECT outcome, COUNT(*) FROM call_outcomes GROUP BY outcome")
        return {k: v for k, v in await cur.fetchall()}
