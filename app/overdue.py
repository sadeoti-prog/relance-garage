"""Overdue detection and escalation logic.

This module is the arithmetic core of the product. It answers four questions,
entirely in Python, on integer cents:

  1. which invoices are late, and by how many days;
  2. what is actually still owed on each of them;
  3. which escalation level that delay corresponds to;
  4. what penalties, if any, the garage is entitled to claim.

Step 3 hands the results to the language model as finished strings. The model
never adds, multiplies or compares a figure — it only writes prose around
numbers that were decided here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .config import (
    LEVEL_CHANNEL,
    LEVEL_THRESHOLD_DAYS,
    LEVELS,
    PENALTY_FROM_LEVEL,
    RECOVERY_FEE_CENTS,
    today as today_fn,
)
from .money import prorata_annual

# Levels sorted once, highest first: used to resolve a delay into a level.
_LEVELS_DESC = tuple(sorted(LEVELS, key=lambda level: LEVEL_THRESHOLD_DAYS[level], reverse=True))

# A follow-up already recorded at a level means that level is handled, whatever
# the owner decided — validating, sending or skipping all close it.
HANDLED_STATUSES = (
    "pending", "validated", "sent", "skipped", "handed_over", "rejected",
)


# ---------------------------------------------------------------------------
# Pure functions — no database, no I/O, trivially testable
# ---------------------------------------------------------------------------

def days_overdue(due_date: date | str, reference: date) -> int:
    """Positive when late, zero on the due date, negative before it."""
    if isinstance(due_date, str):
        due_date = date.fromisoformat(due_date)
    return (reference - due_date).days


def level_for_days(days: int) -> int:
    """The escalation level a delay has reached. 0 means no follow-up yet."""
    for level in _LEVELS_DESC:
        if days >= LEVEL_THRESHOLD_DAYS[level]:
            return level
    return 0


def next_level_after(level: int) -> int | None:
    """The level that comes after this one, or None at the top of the ladder."""
    higher = [candidate for candidate in LEVELS if candidate > level]
    return min(higher) if higher else None


def date_of_level(due_date: date | str, level: int) -> date:
    """The day an invoice reaches a given level."""
    if isinstance(due_date, str):
        due_date = date.fromisoformat(due_date)
    return due_date + timedelta(days=LEVEL_THRESHOLD_DAYS[level])


def amount_due_cents(total_ttc_cents: int, amount_paid_cents: int) -> int:
    """What is still owed. A deposit already received is deducted."""
    return max(0, total_ttc_cents - amount_paid_cents)


def late_penalty_cents(due_cents: int, days: int, level: int,
                       rate_bp: int | None, terms_confirmed: bool) -> int:
    """Interest accrued on the outstanding balance.

    Three conditions, all required:

    * the invoice has reached `PENALTY_FROM_LEVEL` — at the first level the
      garage assumes an oversight and keeps the exchange commercial;
    * this customer has a penalty rate on file;
    * the garage has confirmed the terms that customer actually accepted.

    Without a confirmed rate the system claims nothing. It does not fall back
    on a default: a rate nobody validated has no business on an invoice.
    """
    if level < PENALTY_FROM_LEVEL:
        return 0
    if not terms_confirmed or not rate_bp:
        return 0
    return prorata_annual(due_cents, rate_bp, days)


def recovery_fee_cents(customer_kind: str, level: int,
                       kind_confirmed: bool = True) -> int:
    """Fixed recovery indemnity, business customers only.

    Article L441-10 of the code de commerce provides for it between
    professionals. It is never applied to a private individual.

    Unlike the interest rate, this amount is set by law rather than by the
    contract, so it does not depend on the CGV confirmation — but it does
    depend on the customer really being a professional. An imported customer's
    kind is guessed from its name, and a guess is not a basis for claiming
    money: "Menuiserie Martin" may be M. Martin, a private individual, and
    charging him 40 € we have no right to puts the garage in the wrong.
    """
    if customer_kind != "professionnel" or level < PENALTY_FROM_LEVEL:
        return 0
    if not kind_confirmed:
        return 0
    return RECOVERY_FEE_CENTS


# ---------------------------------------------------------------------------
# The evaluated state of one invoice
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OverdueInvoice:
    # identity
    invoice_id: int
    number: str
    job_label: str
    issue_date: str
    due_date: str
    payment_terms_days: int
    status: str

    # customer
    customer_id: int
    customer_name: str
    customer_kind: str
    civility: str
    contact_name: str
    email: str
    phone: str
    customer_since: int
    is_regular: bool
    customer_address_line: str

    # penalty terms as agreed with this customer
    penalty_rate_bp: int | None
    cgv_reference: str | None
    penalty_terms_confirmed: bool
    penalty_blocked: bool          # would apply, but the terms are unconfirmed
    kind_confirmed: bool           # has a human confirmed particulier/professionnel
    recovery_fee_blocked: bool     # indemnity withheld because the kind is a guess

    # vehicle
    plate: str
    make: str
    model: str

    # money, all integer cents, all computed above
    total_ttc_cents: int
    amount_paid_cents: int
    amount_due_cents: int
    penalty_cents: int
    recovery_fee_cents: int
    total_claimed_cents: int

    # timing
    days_overdue: int
    level: int
    next_level: int | None
    next_level_date: str | None
    days_to_next_level: int | None

    # workflow
    channel: str
    highest_followup_level: int
    pending_followup_id: int | None
    followup_is_due: bool

    @property
    def is_overdue(self) -> bool:
        return self.days_overdue > 0

    @property
    def vehicle_label(self) -> str:
        """Empty when the file carried no vehicle: an import may not have one."""
        return f"{self.make} {self.model}".strip()

    @property
    def has_vehicle(self) -> bool:
        return bool(self.vehicle_label or self.plate)

    @property
    def has_deposit(self) -> bool:
        return self.amount_paid_cents > 0 and self.status == "partial"


def evaluate(row, reference: date, highest_followup_level: int = 0,
             pending_followup_id: int | None = None) -> OverdueInvoice:
    """Compute the full state of one invoice. Pure: a mapping in, a value out."""
    days = days_overdue(row["due_date"], reference)
    level = level_for_days(days)

    due = amount_due_cents(row["total_ttc_cents"], row["amount_paid_cents"])
    rate_bp = row["late_penalty_rate_bp"]
    confirmed = bool(row["penalty_terms_confirmed"])
    penalty = late_penalty_cents(due, days, level, rate_bp, confirmed)
    kind_confirmed = bool(row["kind_confirmed"])
    fee = recovery_fee_cents(row["customer_kind"], level, kind_confirmed)
    blocked = level >= PENALTY_FROM_LEVEL and not (confirmed and rate_bp)
    fee_blocked = (
        level >= PENALTY_FROM_LEVEL
        and row["customer_kind"] == "professionnel"
        and not kind_confirmed
    )

    following = next_level_after(level)
    next_date = date_of_level(row["due_date"], following) if following else None

    # A level is due when the invoice has reached it and nothing has been
    # drafted at that level or beyond. Skipping a level closes it for good:
    # the owner said no, the system does not ask twice.
    is_due = (
        row["status"] != "paid"
        and level >= 1
        and highest_followup_level < level
    )

    return OverdueInvoice(
        invoice_id=row["id"],
        number=row["number"],
        job_label=row["job_label"],
        issue_date=row["issue_date"],
        due_date=row["due_date"],
        payment_terms_days=row["payment_terms_days"],
        status=row["status"],
        customer_id=row["customer_id"],
        customer_name=row["customer_name"],
        customer_kind=row["customer_kind"],
        civility=row["civility"] or "",
        contact_name=row["contact_name"] or row["customer_name"],
        email=row["email"],
        phone=row["phone"],
        customer_since=row["customer_since"],
        is_regular=bool(row["is_regular"]),
        customer_address_line=(
            f"{row['address']}, {row['postal_code']} {row['city']}"
        ),
        penalty_rate_bp=rate_bp,
        cgv_reference=row["cgv_reference"],
        penalty_terms_confirmed=confirmed,
        penalty_blocked=blocked,
        kind_confirmed=kind_confirmed,
        recovery_fee_blocked=fee_blocked,
        plate=row["plate"] or "",
        make=row["make"] or "",
        model=row["model"] or "",
        total_ttc_cents=row["total_ttc_cents"],
        amount_paid_cents=row["amount_paid_cents"],
        amount_due_cents=due,
        penalty_cents=penalty,
        recovery_fee_cents=fee,
        total_claimed_cents=due + penalty + fee,
        days_overdue=days,
        level=level,
        next_level=following,
        next_level_date=next_date.isoformat() if next_date else None,
        days_to_next_level=(next_date - reference).days if next_date else None,
        channel=LEVEL_CHANNEL.get(level, "email"),
        highest_followup_level=highest_followup_level,
        pending_followup_id=pending_followup_id,
        followup_is_due=is_due,
    )


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

_INVOICE_QUERY = """
    SELECT i.id, i.number, i.job_label, i.issue_date, i.due_date,
           i.payment_terms_days, i.status, i.total_ttc_cents, i.amount_paid_cents,
           i.customer_id,
           c.name AS customer_name, c.kind AS customer_kind, c.civility,
           c.contact_name, c.email, c.phone, c.customer_since, c.is_regular,
           c.late_penalty_rate_bp, c.cgv_reference, c.penalty_terms_confirmed,
           c.kind_confirmed,
           c.address, c.postal_code, c.city,
           v.plate, v.make, v.model
    FROM invoices i
    JOIN customers c ON c.id = i.customer_id
    LEFT JOIN vehicles v ON v.id = i.vehicle_id
    {where}
    ORDER BY i.due_date
"""


def _followup_state(
    conn: sqlite3.Connection, reference: date
) -> tuple[dict[int, int], dict[int, int]]:
    """Highest level handled, plus a draft calculated for ``reference``.

    Unreviewed drafts are day-specific: their wording and late-payment interest
    can contain the overdue-day count.  Yesterday's pending draft therefore
    cannot hide the fact that a fresh calculation is needed today.
    """
    durable_statuses = tuple(status for status in HANDLED_STATUSES if status != "pending")
    placeholders = ",".join("?" * len(durable_statuses))
    reference_iso = reference.isoformat()
    highest = {
        row["invoice_id"]: row["level"]
        for row in conn.execute(
            f"""
            SELECT invoice_id, MAX(level) AS level
            FROM followups
            WHERE status IN ({placeholders})
               OR (status = 'pending' AND substr(created_at, 1, 10) = ?)
            GROUP BY invoice_id
            """,
            (*durable_statuses, reference_iso),
        )
    }
    pending = {
        row["invoice_id"]: row["id"]
        for row in conn.execute(
            """
            SELECT id, invoice_id
              FROM followups
             WHERE status = 'pending'
               AND substr(created_at, 1, 10) = ?
            """,
            (reference_iso,),
        )
    }
    return highest, pending


def load_all(conn: sqlite3.Connection, reference: date | None = None,
             include_paid: bool = False) -> list[OverdueInvoice]:
    """Every invoice, evaluated. Settled ones are excluded by default."""
    reference = reference or today_fn()
    where = "" if include_paid else "WHERE i.status != 'paid'"
    highest, pending = _followup_state(conn, reference)

    return [
        evaluate(row, reference, highest.get(row["id"], 0), pending.get(row["id"]))
        for row in conn.execute(_INVOICE_QUERY.format(where=where))
    ]


def load_overdue(conn: sqlite3.Connection,
                 reference: date | None = None) -> list[OverdueInvoice]:
    """Only invoices past their due date, most urgent first."""
    invoices = [item for item in load_all(conn, reference) if item.is_overdue]
    invoices.sort(key=lambda item: (-item.level, -item.days_overdue))
    return invoices


def followups_due(conn: sqlite3.Connection,
                  reference: date | None = None) -> list[OverdueInvoice]:
    """Invoices that need a new draft right now, worst debt first."""
    due = [item for item in load_all(conn, reference) if item.followup_is_due]
    due.sort(key=lambda item: (-item.level, -item.amount_due_cents))
    return due


# ---------------------------------------------------------------------------
# Aggregates for the dashboard
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LevelBucket:
    level: int
    count: int
    amount_due_cents: int
    penalty_cents: int
    recovery_fee_cents: int

    @property
    def extra_cents(self) -> int:
        """Everything claimed on top of the invoice itself."""
        return self.penalty_cents + self.recovery_fee_cents


@dataclass(frozen=True)
class Portfolio:
    reference_date: str
    invoice_count: int
    settled_count: int
    outstanding_cents: int          # everything not yet collected
    overdue_cents: int              # the part that is actually late
    not_due_cents: int
    overdue_count: int
    not_due_count: int
    oldest_days_overdue: int
    buckets: tuple[LevelBucket, ...]
    followups_due_count: int
    followups_due_cents: int
    penalty_blocked_count: int      # overdue invoices whose terms are unconfirmed


def portfolio(conn: sqlite3.Connection, reference: date | None = None) -> Portfolio:
    """The numbers the dashboard shows large. Computed here, once."""
    reference = reference or today_fn()
    items = load_all(conn, reference)

    counts = conn.execute(
        "SELECT COUNT(*) AS total, SUM(status = 'paid') AS settled FROM invoices"
    ).fetchone()

    overdue = [item for item in items if item.is_overdue]
    not_due = [item for item in items if not item.is_overdue]

    buckets = []
    for level in (0, *LEVELS):
        group = [item for item in overdue if item.level == level]
        if not group and level == 0:
            continue
        buckets.append(
            LevelBucket(
                level=level,
                count=len(group),
                amount_due_cents=sum(item.amount_due_cents for item in group),
                penalty_cents=sum(item.penalty_cents for item in group),
                recovery_fee_cents=sum(item.recovery_fee_cents for item in group),
            )
        )

    pending = [item for item in items if item.followup_is_due]

    return Portfolio(
        reference_date=reference.isoformat(),
        invoice_count=counts["total"],
        settled_count=counts["settled"] or 0,
        outstanding_cents=sum(item.amount_due_cents for item in items),
        overdue_cents=sum(item.amount_due_cents for item in overdue),
        not_due_cents=sum(item.amount_due_cents for item in not_due),
        overdue_count=len(overdue),
        not_due_count=len(not_due),
        oldest_days_overdue=max((item.days_overdue for item in overdue), default=0),
        buckets=tuple(buckets),
        followups_due_count=len(pending),
        followups_due_cents=sum(item.amount_due_cents for item in pending),
        penalty_blocked_count=sum(1 for item in overdue if item.penalty_blocked),
    )
