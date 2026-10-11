"""SQLite access and schema.

Enum-like columns keep English codes (paid / unpaid / partial). Everything the
customer or the garage owner actually reads is stored in French.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS customers (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL,
    kind               TEXT    NOT NULL CHECK (kind IN ('particulier','professionnel')),
    -- Whether a human confirmed that kind. An imported customer's kind is
    -- guessed from its name, and a guess must never reach the statutory
    -- recovery indemnity: "Menuiserie Martin" may well be M. Martin himself.
    kind_confirmed     INTEGER NOT NULL DEFAULT 0,
    contact_name       TEXT,
    civility           TEXT,
    email              TEXT    NOT NULL,
    phone              TEXT    NOT NULL,
    address            TEXT    NOT NULL,
    postal_code        TEXT    NOT NULL,
    city               TEXT    NOT NULL,
    siret              TEXT,
    payment_terms_days INTEGER NOT NULL,
    customer_since     INTEGER NOT NULL,
    is_regular         INTEGER NOT NULL DEFAULT 0,
    -- Penalty terms belong to the contract signed with THIS customer. They
    -- differ between business and private customers, and nothing is claimed
    -- until penalty_terms_confirmed is set by the garage.
    late_penalty_rate_bp    INTEGER,
    cgv_reference           TEXT,
    penalty_terms_confirmed INTEGER NOT NULL DEFAULT 0,
    -- Normalised name, the key a re-import matches on. Kept in the table so
    -- matching is a lookup rather than a scan.
    match_key          TEXT,
    source             TEXT    NOT NULL DEFAULT 'demo',
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customers_match ON customers(match_key);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);

CREATE TABLE IF NOT EXISTS vehicles (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    -- Imported files often carry no vehicle at all: both the plate and the
    -- description are optional. SQLite allows repeated NULLs under UNIQUE,
    -- so several vehicles may legitimately have no plate.
    plate       TEXT    UNIQUE,
    make        TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    year        INTEGER,
    mileage_km  INTEGER
);

CREATE TABLE IF NOT EXISTS invoices (
    id                 INTEGER PRIMARY KEY,
    number             TEXT    NOT NULL UNIQUE,
    customer_id        INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    -- Nullable: a garage export may not mention the vehicle.
    vehicle_id         INTEGER REFERENCES vehicles(id) ON DELETE SET NULL,
    job_code           TEXT    NOT NULL,
    job_label          TEXT    NOT NULL,
    issue_date         TEXT    NOT NULL,
    due_date           TEXT    NOT NULL,
    payment_terms_days INTEGER NOT NULL,
    total_ht_cents     INTEGER NOT NULL,
    vat_rate_bp        INTEGER NOT NULL,
    vat_cents          INTEGER NOT NULL,
    total_ttc_cents    INTEGER NOT NULL,
    amount_paid_cents  INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL CHECK (status IN ('paid','unpaid','partial')),
    paid_date          TEXT,
    payment_method     TEXT,
    source             TEXT    NOT NULL DEFAULT 'demo',   -- 'demo' | 'import'
    imported_at        TEXT,
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoices_status   ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_due_date ON invoices(due_date);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_number ON invoices(number);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id                  INTEGER PRIMARY KEY,
    invoice_id          INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    position            INTEGER NOT NULL,
    label               TEXT    NOT NULL,
    kind                TEXT    NOT NULL CHECK (kind IN ('part','labour','fee')),
    quantity_milli      INTEGER NOT NULL,
    unit                TEXT    NOT NULL,
    unit_price_ht_cents INTEGER NOT NULL,
    total_ht_cents      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lines_invoice ON invoice_lines(invoice_id);

-- Manual corrections are explicit and traceable; never silently overwritten
-- by an older accounting export. No bank transaction is performed here.
CREATE TABLE IF NOT EXISTS payment_changes (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    previous_paid_cents INTEGER NOT NULL,
    amount_paid_cents INTEGER NOT NULL,
    payment_date TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payment_changes_invoice ON payment_changes(invoice_id, id);

-- Follow-ups. Populated from step 3 onwards; every figure below is computed
-- in Python and frozen at drafting time, the model only writes subject/body.
CREATE TABLE IF NOT EXISTS followups (
    id                  INTEGER PRIMARY KEY,
    invoice_id          INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    level               INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),
    channel             TEXT    NOT NULL,
    -- 'rejected' is set by the number guardrail: a draft containing a figure
    -- that was never given to the model never reaches the validation queue.
    -- 'obsolete' means the invoice was settled while a follow-up was still
    -- open. Closed automatically at re-import: a forgotten draft chasing a
    -- customer who has already paid is the same class of damage as a wrong
    -- amount.
    -- 'handed_over' means the .eml was downloaded. That is NOT sending: the
    -- owner may have pressed Send, or closed the window. Only his explicit
    -- confirmation moves a row to 'sent'.
    status              TEXT    NOT NULL CHECK (status IN ('pending','validated','skipped','sent','rejected','handed_over','obsolete')),
    subject             TEXT    NOT NULL,
    body                TEXT    NOT NULL,
    days_overdue        INTEGER NOT NULL,
    amount_due_cents    INTEGER NOT NULL,
    penalty_cents       INTEGER NOT NULL DEFAULT 0,
    recovery_fee_cents  INTEGER NOT NULL DEFAULT 0,
    total_claimed_cents INTEGER NOT NULL,
    generator           TEXT    NOT NULL,
    guardrail_ok        INTEGER NOT NULL DEFAULT 1,
    guardrail_note      TEXT,
    edited_by_human     INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL,
    decided_at          TEXT,
    handed_over_at      TEXT,
    sent_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_followups_status  ON followups(status);
CREATE INDEX IF NOT EXISTS idx_followups_invoice ON followups(invoice_id, level);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def reset(conn: sqlite3.Connection) -> None:
    """Drop everything and recreate an empty schema."""
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        DROP TABLE IF EXISTS payment_changes;
        DROP TABLE IF EXISTS followups;
        DROP TABLE IF EXISTS invoice_lines;
        DROP TABLE IF EXISTS invoices;
        DROP TABLE IF EXISTS vehicles;
        DROP TABLE IF EXISTS customers;
        """
    )
    conn.commit()
    init_schema(conn)
