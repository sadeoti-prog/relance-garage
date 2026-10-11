"""Drafting follow-ups and putting them in the validation queue.

Nothing here sends anything. A draft that passes the number guardrail is stored
as `pending` and waits for the owner. A draft that fails is stored as `rejected`
with the reason attached — it is never queued, and never counted as work done.

Step 5 adds the decisions (validate / edit / skip) on top of this.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from .config import today as today_fn
from .drafting import Draft, build_context, generate
from .overdue import OverdueInvoice, followups_due


@dataclass(frozen=True)
class DraftResult:
    invoice: OverdueInvoice
    draft: Draft
    followup_id: int


@dataclass(frozen=True)
class DraftRun:
    queued: tuple[DraftResult, ...]      # passed the guardrail, awaiting validation
    rejected: tuple[DraftResult, ...]    # blocked, flagged for the owner
    reference_date: str

    @property
    def total(self) -> int:
        return len(self.queued) + len(self.rejected)


def draft_for(item: OverdueInvoice, reference: date | None = None,
              mode: str | None = None, backend=None) -> Draft:
    """Draft the message for one overdue invoice, guardrail included."""
    context = build_context(item, item.customer_address_line, reference)
    return generate(context, mode, backend)


def _store(
    conn: sqlite3.Connection,
    item: OverdueInvoice,
    draft: Draft,
    reference: date,
) -> int:
    """Persist a draft. Its figures are frozen here, copied from the engine."""
    cursor = conn.execute(
        """
        INSERT INTO followups
            (invoice_id, level, channel, status, subject, body, days_overdue,
             amount_due_cents, penalty_cents, recovery_fee_cents,
             total_claimed_cents, generator, guardrail_ok, guardrail_note,
             created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            item.invoice_id, item.level, item.channel,
            "pending" if draft.accepted else "rejected",
            draft.subject, draft.body, item.days_overdue,
            item.amount_due_cents, item.penalty_cents, item.recovery_fee_cents,
            item.total_claimed_cents, draft.generator,
            int(draft.accepted), draft.guardrail.note,
            # The business date may deliberately be frozen for a demo.  Store
            # that date rather than the computer's wall-clock date so a draft
            # is considered current for the same day the calculations used.
            datetime.combine(reference, datetime.now().time()).isoformat(
                timespec="seconds"
            ),
        ),
    )
    return cursor.lastrowid


def create_drafts(conn: sqlite3.Connection, reference: date | None = None,
                  limit: int | None = None, mode: str | None = None,
                  backend=None, invoice_id: int | None = None) -> DraftRun:
    """Draft every follow-up due today and file the results."""
    reference = reference or today_fn()
    queued: list[DraftResult] = []
    rejected: list[DraftResult] = []
    try:
        # Recompute eligibility while holding the writer lock. A simultaneous
        # click or process waits, then sees the draft stored by the first one;
        # neither duplicate rows nor duplicate paid API calls can be created.
        conn.execute("BEGIN IMMEDIATE")
        # A message says "N days late" and may include daily late-payment
        # interest.  Keeping yesterday's unreviewed text would make the invoice
        # page and the reminder disagree.  Retire it before recomputing; no API
        # call happens until the owner explicitly asks to prepare again.
        conn.execute(
            """
            UPDATE followups
               SET status = 'obsolete',
                   guardrail_note = 'Brouillon arrivé à expiration ; il doit être recalculé.'
             WHERE status = 'pending'
               AND substr(created_at, 1, 10) != ?
               AND (? IS NULL OR invoice_id = ?)
            """,
            (reference.isoformat(), invoice_id, invoice_id),
        )
        pending = followups_due(conn, reference)
        if invoice_id is not None:
            pending = [item for item in pending if item.invoice_id == invoice_id]
        if limit is not None:
            pending = pending[:limit]

        for item in pending:
            draft = draft_for(item, reference, mode, backend)
            followup_id = _store(conn, item, draft, reference)
            result = DraftResult(invoice=item, draft=draft, followup_id=followup_id)
            (queued if draft.accepted else rejected).append(result)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return DraftRun(queued=tuple(queued), rejected=tuple(rejected),
                    reference_date=reference.isoformat())


# ---------------------------------------------------------------------------
# Reading the queue
# ---------------------------------------------------------------------------

_QUEUE_QUERY = """
    SELECT f.*, i.number, i.job_label, i.due_date,
           c.name AS customer_name, c.kind AS customer_kind, c.email,
           v.plate, v.make, v.model
    FROM followups f
    JOIN invoices  i ON i.id = f.invoice_id
    JOIN customers c ON c.id = i.customer_id
    LEFT JOIN vehicles v ON v.id = i.vehicle_id
    WHERE f.status = ?
    ORDER BY f.level DESC, f.amount_due_cents DESC
"""


def queue(conn: sqlite3.Connection, status: str = "pending") -> list[sqlite3.Row]:
    """Drafts waiting for a decision, worst debt first."""
    return conn.execute(_QUEUE_QUERY, (status,)).fetchall()


def clear_drafts(conn: sqlite3.Connection, restore_history: bool = True) -> int:
    """Wipe every draft so the demo can be replayed from a clean slate.

    The archive of follow-ups already sent on settled invoices is rebuilt
    afterwards: it is seeded data, not demo actions, and the activity counters
    are computed from it. Wiping it would zero them.
    """
    from .seed import seed_followup_history

    count = conn.execute(
        "SELECT COUNT(*) FROM followups WHERE status != 'sent'"
    ).fetchone()[0]
    conn.execute("DELETE FROM followups")
    conn.commit()

    if restore_history:
        seed_followup_history(conn)
    return count
