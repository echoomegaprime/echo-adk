"""Atomic SQLite budget admission and exactly-once settlement.

All unfinished reservations count against the current UTC day's allowance, including holds
created before midnight. Charges are booked on the UTC settlement day. An interrupted run
whose final cost is unknown consumes its reserved ceiling, marked as uncertain rather than
reported as a measured provider charge. Monetary arithmetic uses integer microdollars.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Optional

from ..core.types import now_iso

MICRODOLLARS = 1_000_000
BUDGET_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_reservations (
    run_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    admission_day TEXT NOT NULL,
    reserved_micros INTEGER NOT NULL CHECK(reserved_micros >= 0),
    state TEXT NOT NULL CHECK(state IN ('held', 'settled')),
    charged_micros INTEGER NOT NULL DEFAULT 0 CHECK(charged_micros >= 0),
    settlement_day TEXT,
    uncertain INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, scope)
);
CREATE INDEX IF NOT EXISTS budget_holds_scope ON budget_reservations(scope, state);
"""


def money_micros(value: Any) -> int:
    """Round a nonnegative, finite amount upward so precision cannot buy extra authority."""
    if isinstance(value, bool):
        raise ValueError("budget amount must be a finite nonnegative number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("budget amount must be a finite nonnegative number") from None
    if not amount.is_finite() or amount < 0 or amount > 1_000_000_000:
        raise ValueError("budget amount must be a finite nonnegative number")
    return int((amount * MICRODOLLARS).to_integral_value(rounding=ROUND_CEILING))


def dollars(micros: int) -> float:
    return micros / MICRODOLLARS


class BudgetAdmissionError(Exception):
    def __init__(self, scope: str, *, cap: int, spent: int, held: int, requested: int) -> None:
        self.scope = scope
        self.cap_usd = dollars(cap)
        self.spent_usd = dollars(spent)
        self.reserved_usd = dollars(held)
        self.requested_usd = dollars(requested)
        label = "service" if scope == "service" else "bot"
        super().__init__(
            f"{label} daily cost cap {self.cap_usd:.6f} USD would be exceeded: "
            f"spent {self.spent_usd:.6f}, reserved {self.reserved_usd:.6f}, requested {self.requested_usd:.6f}"
        )


class BudgetReservations:
    """Store mixin; callers share Store.transaction() and its one SQLite connection."""

    def _init_budget_ledger(self) -> None:
        self._db.executescript(BUDGET_SCHEMA)
        # Upgrade existing 0.1.0 state without losing earlier spend. Keep usd as a compatibility
        # projection for existing diagnostics; all new admission arithmetic uses micros.
        with self.transaction():
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(spend)")}
            if "micros" not in columns:
                self._db.execute("ALTER TABLE spend ADD COLUMN micros INTEGER NOT NULL DEFAULT 0")
                for day, scope, usd in self._db.execute("SELECT day, scope, usd FROM spend").fetchall():
                    self._db.execute("UPDATE spend SET micros=? WHERE day=? AND scope=?", (money_micros(usd), day, scope))
            if "uncertain_micros" not in columns:
                self._db.execute("ALTER TABLE spend ADD COLUMN uncertain_micros INTEGER NOT NULL DEFAULT 0")
                self._db.execute(
                    "UPDATE spend SET uncertain_micros=COALESCE((SELECT SUM(charged_micros) FROM budget_reservations r "
                    "WHERE r.scope=spend.scope AND r.settlement_day=spend.day AND r.uncertain=1),0)"
                )

    def _spent_micros(self, scope: str, day: str) -> int:
        row = self._db.execute("SELECT micros FROM spend WHERE day=? AND scope=?", (day, scope)).fetchone()
        return int(row[0]) if row else 0

    def _held_micros(self, scope: str, *, excluding: Optional[str] = None) -> int:
        # Deliberately no day filter: midnight must not erase outstanding commitments.
        row = self._db.execute(
            "SELECT COALESCE(SUM(reserved_micros),0) FROM budget_reservations WHERE scope=? AND state='held' AND (? IS NULL OR run_id<>?)",
            (scope, excluding, excluding),
        ).fetchone()
        return int(row[0])

    def _add_spend_locked(self, scope: str, amount: int, day: str, *, uncertain: bool = False) -> int:
        self._db.execute(
            "INSERT INTO spend(day,scope,usd,micros,uncertain_micros) VALUES (?,?,?,?,?) "
            "ON CONFLICT(day,scope) DO UPDATE SET "
            "micros=spend.micros+excluded.micros, usd=(spend.micros+excluded.micros)/1000000.0, "
            "uncertain_micros=spend.uncertain_micros+excluded.uncertain_micros",
            (day, scope, dollars(amount), amount, amount if uncertain else 0),
        )
        return self._spent_micros(scope, day)

    def reserve_budget(self, run_id: str, amount_usd: float, limits: Mapping[str, float]) -> dict[str, Any]:
        """Reserve every scope together, or reserve none. Safe across SQLite connections.

        Revalidating an existing held reservation excludes its own hold, but still checks
        today's settled spend and every other unfinished job. A settled run cannot restart.
        """
        amount = money_micros(amount_usd)
        caps = {scope: money_micros(cap) for scope, cap in limits.items()}
        if "service" not in caps:
            raise ValueError("every run requires a service budget reservation")
        day = now_iso()[:10]
        with self.transaction():
            existing = self._db.execute("SELECT scope,state,reserved_micros FROM budget_reservations WHERE run_id=?", (run_id,)).fetchall()
            if existing and (any(row[1] != "held" or row[2] != amount for row in existing) or {row[0] for row in existing} != set(caps)):
                raise ValueError("a run's settled or mismatched reservation cannot be reused")
            for scope, cap in caps.items():
                spent = self._spent_micros(scope, day)
                held = self._held_micros(scope, excluding=run_id)
                if amount and spent + held + amount > cap:
                    raise BudgetAdmissionError(scope, cap=cap, spent=spent, held=held, requested=amount)
            if not existing:
                self._db.executemany(
                    "INSERT INTO budget_reservations(run_id,scope,admission_day,reserved_micros,state) VALUES (?,?,?,?,'held')",
                    [(run_id, scope, day, amount) for scope in caps],
                )
            return self.budget_accounting(run_id)

    def admit_run(self, doc: dict[str, Any], limits: Mapping[str, float]) -> None:
        """Persist the run and its holds in one transaction; a crash leaves both or neither."""
        with self.transaction():
            if self.get_run(doc["run_id"]) is not None:
                raise ValueError("run already exists")
            doc["budget_accounting"] = self.reserve_budget(doc["run_id"], doc["budget"]["max_cost_usd"], limits)
            self.put_run(doc)

    def settle_budget(self, doc: dict[str, Any], actual_usd: float, *, uncertain: bool = False) -> dict[str, Any]:
        """Charge service and bot once, release unused holds, and persist the final run atomically.

        Unknown charges consume the larger of measured-so-far cost and the admission ceiling.
        Legacy unfinished rows without holds are accounted conservatively on recovery too.
        """
        actual = money_micros(actual_usd)
        run_id = doc["run_id"]
        day = now_iso()[:10]
        with self.transaction():
            rows = self._db.execute("SELECT scope,state,reserved_micros FROM budget_reservations WHERE run_id=?", (run_id,)).fetchall()
            if not rows:
                ceiling = money_micros(doc["budget"]["max_cost_usd"])
                scopes = ["service"] + ([f"bot:{doc['bot_id']}"] if doc.get("bot_id") else [])
                self._db.executemany(
                    "INSERT INTO budget_reservations(run_id,scope,admission_day,reserved_micros,state) VALUES (?,?,?,?,'held')",
                    [(run_id, scope, doc["started_at"][:10], ceiling) for scope in scopes],
                )
                rows = [(scope, "held", ceiling) for scope in scopes]
            for scope, state, reserved in rows:
                if state != "held":
                    continue
                charge = max(actual, reserved) if uncertain else actual
                self._add_spend_locked(scope, charge, day, uncertain=uncertain)
                self._db.execute(
                    "UPDATE budget_reservations SET state='settled',charged_micros=?,settlement_day=?,uncertain=? "
                    "WHERE run_id=? AND scope=? AND state='held'",
                    (charge, day, int(uncertain), run_id, scope),
                )
            doc["budget_accounting"] = self.budget_accounting(run_id)
            self.put_run(doc)
            return doc["budget_accounting"]

    def budget_accounting(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT admission_day,reserved_micros,state,charged_micros,settlement_day,uncertain "
                "FROM budget_reservations WHERE run_id=? AND scope='service'",
                (run_id,),
            ).fetchone()
        if row is None:
            return {"state": "unreserved"}
        return {
            "state": row[2],
            "admission_day": row[0],
            "reserved_usd": dollars(row[1]) if row[2] == "held" else 0.0,
            "ceiling_usd": dollars(row[1]),
            "charged_usd": dollars(row[3]),
            "settlement_day": row[4],
            "uncertain_cost": bool(row[5]),
            "overrun_usd": dollars(max(0, row[3] - row[1])),
        }

    def budget_status(self, scope: str, cap_usd: float) -> dict[str, Any]:
        day = now_iso()[:10]
        cap = money_micros(cap_usd)
        with self.transaction():
            spent = self._spent_micros(scope, day)
            held = self._held_micros(scope)
            row = self._db.execute("SELECT uncertain_micros FROM spend WHERE scope=? AND day=?", (scope, day)).fetchone()
            uncertain = int(row[0]) if row else 0
        return {
            "day": day,
            "timezone": "UTC",
            "daily_cap_usd": dollars(cap),
            "spent_today_usd": dollars(spent),
            "reserved_usd": dollars(held),
            "available_usd": dollars(max(0, cap - spent - held)),
            "uncertain_charged_today_usd": dollars(int(uncertain)),
            "accounting": "utc_settlement_day_with_all_unfinished_holds",
        }

    def forget_budget(self, run_id: str) -> None:
        """Remove a deleted run's receipt, never its aggregated service spend."""
        with self.transaction():
            held = self._db.execute("SELECT 1 FROM budget_reservations WHERE run_id=? AND state='held' LIMIT 1", (run_id,)).fetchone()
            if held:
                raise ValueError("cannot forget a run with an unsettled budget")
            self._db.execute("DELETE FROM budget_reservations WHERE run_id=?", (run_id,))
