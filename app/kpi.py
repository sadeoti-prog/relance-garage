"""The activity counters on the dashboard.

Three numbers the owner cares about, computed in Python like everything else:
how many follow-ups went out, how much came back in after one, and how much
writing time that saved him.

**A rolling thirty days, not a calendar month.** These figures are not
accounting, they are the evidence the tool is doing something. A calendar month
wipes that evidence at midnight on the 1st: an owner opening the dashboard on
3 September would read "0 relance envoyée · 0,00 € encaissé" and conclude the
thing is dead — days after it recovered three thousand euros. Evidence should
not disappear because of the calendar.

The definitions are unchanged. "Recovered" still counts only invoices settled
after a follow-up had gone out — money the system can honestly claim a hand in;
invoices that were simply paid are not counted, even though they would make the
number bigger. "Hours saved" still excludes drafts the guardrail blocked, since
those saved nobody anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .config import MINUTES_PER_MANUAL_FOLLOWUP
from .config import today as today_fn
from .labels_fr import format_date

# Thirty days ending today, today included.
WINDOW_DAYS = 30


@dataclass(frozen=True)
class ActivityReport:
    window_label: str           # "sur les 30 derniers jours"
    window_range: str           # "du 04/08/2026 au 02/09/2026"
    followups_sent: int
    recovered_cents: int
    recovered_count: int
    drafts_prepared: int
    minutes_saved: int

    @property
    def hours_saved_label(self) -> str:
        hours, minutes = divmod(self.minutes_saved, 60)
        if hours and minutes:
            return f"{hours} h {minutes:02d}"
        if hours:
            return f"{hours} h"
        return f"{minutes} min"


def window_bounds(reference: date) -> tuple[str, str]:
    """First day counted, and the exclusive upper bound, as ISO strings.

    Both dates and ISO timestamps compare correctly against these as strings:
    `2026-08-04T09:00` sorts after `2026-08-04`, and before `2026-09-03`.
    """
    first = reference - timedelta(days=WINDOW_DAYS - 1)
    beyond = reference + timedelta(days=1)
    return first.isoformat(), beyond.isoformat()


def last_30_days(conn: sqlite3.Connection,
                 reference: date | None = None) -> ActivityReport:
    reference = reference or today_fn()
    start, end = window_bounds(reference)
    first_day = reference - timedelta(days=WINDOW_DAYS - 1)

    followups_sent = conn.execute(
        "SELECT COUNT(*) FROM followups "
        "WHERE status = 'sent' AND sent_at >= ? AND sent_at < ?",
        (start, end),
    ).fetchone()[0]

    # Settled inside the window, and a follow-up had gone out before payment.
    recovered = conn.execute(
        """
        SELECT COUNT(DISTINCT i.id) AS count,
               COALESCE(SUM(i.total_ttc_cents), 0) AS amount
        FROM invoices i
        WHERE i.status = 'paid'
          AND i.paid_date >= ? AND i.paid_date < ?
          AND EXISTS (
              SELECT 1 FROM followups f
              WHERE f.invoice_id = i.id
                AND f.status = 'sent'
                AND date(f.sent_at) <= i.paid_date
          )
        """,
        (start, end),
    ).fetchone()

    # Every message the system wrote that the owner did not have to. Drafts
    # blocked by the guardrail are excluded: they saved nothing.
    drafts_prepared = conn.execute(
        "SELECT COUNT(*) FROM followups "
        "WHERE status != 'rejected' AND created_at >= ? AND created_at < ?",
        (start, end),
    ).fetchone()[0]

    return ActivityReport(
        window_label=f"sur les {WINDOW_DAYS} derniers jours",
        window_range=f"du {format_date(first_day)} au {format_date(reference)}",
        followups_sent=followups_sent,
        recovered_cents=recovered["amount"],
        recovered_count=recovered["count"],
        drafts_prepared=drafts_prepared,
        minutes_saved=drafts_prepared * MINUTES_PER_MANUAL_FOLLOWUP,
    )


# ---------------------------------------------------------------------------
# Customers whose penalty terms were never confirmed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnconfirmedTerms:
    customer_id: int
    customer_name: str
    customer_kind: str
    cgv_reference: str | None
    invoice_count: int
    amount_due_cents: int
    needs_cgv: bool = True          # penalty rate never confirmed
    needs_kind: bool = False        # particulier/professionnel is only a guess


def unconfirmed_terms(conn: sqlite3.Connection,
                      reference: date | None = None) -> list[UnconfirmedTerms]:
    """Overdue customers with no confirmed penalty terms on file.

    Surfaced on the dashboard so the owner can settle the paperwork. Until he
    does, no interest is claimed on any of their invoices.
    """
    reference = reference or today_fn()
    # Two separate gaps, shown together because the owner fixes them in one
    # sitting: a penalty rate nobody confirmed, and a customer kind the import
    # only guessed. Either one withholds money the system will not claim.
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.kind, c.cgv_reference,
               c.penalty_terms_confirmed, c.kind_confirmed,
               COUNT(i.id) AS invoice_count,
               SUM(i.total_ttc_cents - i.amount_paid_cents) AS amount_due
        FROM customers c
        JOIN invoices i ON i.customer_id = c.id
        WHERE (c.penalty_terms_confirmed = 0 OR c.kind_confirmed = 0)
          AND i.status != 'paid'
          AND i.due_date < ?
        GROUP BY c.id
        ORDER BY amount_due DESC
        """,
        (reference.isoformat(),),
    ).fetchall()

    return [
        UnconfirmedTerms(
            customer_id=row["id"],
            customer_name=row["name"],
            customer_kind=row["kind"],
            cgv_reference=row["cgv_reference"],
            invoice_count=row["invoice_count"],
            amount_due_cents=row["amount_due"] or 0,
            needs_cgv=not row["penalty_terms_confirmed"],
            needs_kind=not row["kind_confirmed"],
        )
        for row in rows
    ]
