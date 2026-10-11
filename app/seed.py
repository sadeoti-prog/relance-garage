"""Fake invoice generator.

Builds the whole demo database: customers, vehicles, invoices and invoice lines.
Deterministic — the same seed always produces the same amounts — while the dates
stay relative to today, so the demo never goes stale.

    python -m app.seed            # regenerate data/garage.db
    python -m app.seed --check    # regenerate and run the sanity checks
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, datetime, timedelta

from . import db as db_module
from .catalog import JOBS_BY_CODE, JobSpec
from .config import (
    DB_PATH,
    LABOUR_RATES_CENTS,
    PAYMENT_TERMS_DAYS,
    SEED,
    VAT_RATE_BP,
    DEMO_EMAIL_DOMAIN,
    today as today_fn,
)
from .demo_plan import PAYMENT_METHODS, PLAN, InvoicePlan
from .demo_roster import CUSTOMERS, CUSTOMERS_BY_KEY, CustomerSpec
from .money import format_eur, line_total, vat_of

# The brief fixes the plausible invoice range for a garage.
MIN_INVOICE_TTC_CENTS = 8_000        # 80,00 €
MAX_INVOICE_TTC_CENTS = 240_000      # 2 400,00 €

# Invoice numbering starts high enough to look like an established workshop.
FIRST_INVOICE_SEQ = 1180


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------

def _retail_price_cents(rng: random.Random, lo_eur: float, hi_eur: float) -> int:
    """Pick a price in the range, then push it to a retail-looking ending."""
    lo, hi = round(lo_eur * 100), round(hi_eur * 100)
    raw = rng.randint(lo, hi)
    whole, ending = divmod(raw, 100)
    ending = rng.choice((90, 90, 50, 95, 0))
    value = whole * 100 + ending
    return max(lo, min(hi, value))


def _luhn_check_digit(number: str) -> int:
    total = 0
    for index, char in enumerate(reversed(number)):
        digit = int(char)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10


def _make_siret(rng: random.Random) -> str:
    """A 14-digit SIRET that passes the Luhn check, like the real ones."""
    body = "".join(str(rng.randint(0, 9)) for _ in range(13))
    return body + str(_luhn_check_digit(body))


def _email(spec: CustomerSpec) -> str:
    return f"{spec.email_local}@{DEMO_EMAIL_DOMAIN}"


# ---------------------------------------------------------------------------
# Invoice building
# ---------------------------------------------------------------------------

def build_lines(job: JobSpec, rng: random.Random) -> list[dict]:
    """Turn a job into priced invoice lines. All arithmetic is integer cents."""
    lines: list[dict] = []
    position = 1

    for part in job.parts:
        unit_price = _retail_price_cents(rng, part.price_min, part.price_max)
        lines.append(
            {
                "position": position,
                "label": part.label,
                "kind": "fee" if part.unit == "forfait" else "part",
                "quantity_milli": part.qty_milli,
                "unit": part.unit,
                "unit_price_ht_cents": unit_price,
                "total_ht_cents": line_total(unit_price, part.qty_milli),
            }
        )
        position += 1

    hours_milli = rng.randrange(job.hours_min_milli, job.hours_max_milli + 1, 100)
    rate = LABOUR_RATES_CENTS[job.trade]
    lines.append(
        {
            "position": position,
            "label": job.labour_label,
            "kind": "labour",
            "quantity_milli": hours_milli,
            "unit": "h",
            "unit_price_ht_cents": rate,
            "total_ht_cents": line_total(rate, hours_milli),
        }
    )
    return lines


def totals_for(lines: list[dict]) -> tuple[int, int, int]:
    total_ht = sum(line["total_ht_cents"] for line in lines)
    vat = vat_of(total_ht, VAT_RATE_BP)
    return total_ht, vat, total_ht + vat


def build_invoice_amounts(
    job: JobSpec,
    rng: random.Random,
    target_min_cents: int | None = None,
    target_max_cents: int | None = None,
) -> tuple[list[dict], int, int, int]:
    """Build lines until the total lands inside the wanted range.

    A plan row may ask for an invoice of a given size (`target_*`). That steers
    which draw is kept, never the figure itself: the amount still comes from the
    catalogue prices and the labour rate.
    """
    low = MIN_INVOICE_TTC_CENTS if target_min_cents is None else max(
        MIN_INVOICE_TTC_CENTS, target_min_cents)
    high = MAX_INVOICE_TTC_CENTS if target_max_cents is None else min(
        MAX_INVOICE_TTC_CENTS, target_max_cents)
    targeted = target_min_cents is not None or target_max_cents is not None

    closest: tuple[int, list[dict], int, int, int] | None = None
    midpoint = (low + high) // 2

    for _ in range(400 if targeted else 40):
        lines = build_lines(job, rng)
        total_ht, vat, total_ttc = totals_for(lines)
        if low <= total_ttc <= high:
            return lines, total_ht, vat, total_ttc
        if MIN_INVOICE_TTC_CENTS <= total_ttc <= MAX_INVOICE_TTC_CENTS:
            distance = abs(total_ttc - midpoint)
            if closest is None or distance < closest[0]:
                closest = (distance, lines, total_ht, vat, total_ttc)

    if targeted and closest is not None:
        # The catalogue cannot reach the requested band exactly; keep the
        # closest plausible invoice rather than inventing a number.
        _, lines, total_ht, vat, total_ttc = closest
        return lines, total_ht, vat, total_ttc

    raise ValueError(
        f"job {job.code!r} cannot produce a total between "
        f"{format_eur(low)} and {format_eur(high)}"
    )


def _partial_payment_cents(total_ttc_cents: int, ratio_bp: int) -> int:
    """An acompte is a round figure: nearest 50 €, never 0 and never the total."""
    raw = (total_ttc_cents * ratio_bp) // 10_000
    rounded = ((raw + 2_500) // 5_000) * 5_000
    return max(5_000, min(total_ttc_cents - 5_000, rounded))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate(conn: sqlite3.Connection, reference_day: date | None = None) -> dict:
    reference_day = reference_day or today_fn()

    # Each invoice draws from its own generator, seeded by who and what it is.
    # Retuning one invoice therefore leaves every other amount untouched — the
    # demo can be adjusted without reshuffling the whole ledger.
    rng = random.Random(f"{SEED}|customers")

    db_module.reset(conn)
    now_iso = datetime.now().isoformat(timespec="seconds")

    # --- customers and vehicles -------------------------------------------
    customer_ids: dict[str, int] = {}
    vehicle_ids: dict[tuple[str, int], int] = {}

    for spec in CUSTOMERS:
        siret = _make_siret(rng) if spec.kind == "professionnel" else None
        cursor = conn.execute(
            """
            INSERT INTO customers
                (name, kind, kind_confirmed, contact_name, civility, email,
                 phone, address, postal_code, city, siret, payment_terms_days,
                 customer_since, is_regular, late_penalty_rate_bp,
                 cgv_reference, penalty_terms_confirmed, created_at)
            VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                spec.name, spec.kind, spec.contact_name, spec.civility,
                _email(spec), spec.phone, spec.address, spec.postal_code,
                spec.city, siret, PAYMENT_TERMS_DAYS[spec.kind],
                spec.customer_since, int(spec.is_regular),
                spec.late_penalty_rate_bp, spec.cgv_reference,
                int(spec.penalty_terms_confirmed), now_iso,
            ),
        )
        customer_ids[spec.key] = cursor.lastrowid

        for index, vehicle in enumerate(spec.vehicles):
            cursor = conn.execute(
                """
                INSERT INTO vehicles (customer_id, plate, make, model, year, mileage_km)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    customer_ids[spec.key], vehicle.plate, vehicle.make,
                    vehicle.model, vehicle.year, vehicle.mileage_km,
                ),
            )
            vehicle_ids[(spec.key, index)] = cursor.lastrowid

    # --- invoices ----------------------------------------------------------
    # Amounts are drawn in plan order (stable), then rows are inserted in
    # chronological order so invoice numbers increase with time.
    prepared: list[
        tuple[InvoicePlan, CustomerSpec, JobSpec, date, date, list[dict],
              int, int, int, random.Random]
    ] = []

    for index, plan in enumerate(PLAN):
        spec = CUSTOMERS_BY_KEY[plan.customer]
        job = JOBS_BY_CODE[plan.job]
        terms = PAYMENT_TERMS_DAYS[spec.kind]
        due_day = reference_day + timedelta(days=plan.due_offset)
        issue_day = due_day - timedelta(days=terms)

        invoice_rng = random.Random(
            f"{SEED}|invoice|{index}|{plan.customer}|{plan.job}|{plan.due_offset}"
        )
        lines, total_ht, vat, total_ttc = build_invoice_amounts(
            job,
            invoice_rng,
            None if plan.target_min_eur is None else round(plan.target_min_eur * 100),
            None if plan.target_max_eur is None else round(plan.target_max_eur * 100),
        )
        prepared.append(
            (plan, spec, job, issue_day, due_day, lines, total_ht, vat, total_ttc,
             invoice_rng)
        )

    prepared.sort(key=lambda row: (row[3], row[4]))

    sequence_by_year: dict[int, int] = {}
    for (plan, spec, job, issue_day, due_day, lines, total_ht, vat, total_ttc,
         invoice_rng) in prepared:
        year = issue_day.year
        if year not in sequence_by_year:
            sequence_by_year[year] = FIRST_INVOICE_SEQ if not sequence_by_year else 1
        number = f"FA-{year}-{sequence_by_year[year]:04d}"
        sequence_by_year[year] += 1

        if plan.status == "paid":
            amount_paid = total_ttc
            paid_date = (reference_day + timedelta(days=plan.paid_offset or 0)).isoformat()
            payment_method = invoice_rng.choice(PAYMENT_METHODS)
        elif plan.status == "partial":
            amount_paid = _partial_payment_cents(total_ttc, plan.paid_ratio_bp)
            paid_date = None
            payment_method = "virement (acompte)"
        else:
            amount_paid = 0
            paid_date = None
            payment_method = None

        cursor = conn.execute(
            """
            INSERT INTO invoices
                (number, customer_id, vehicle_id, job_code, job_label, issue_date,
                 due_date, payment_terms_days, total_ht_cents, vat_rate_bp,
                 vat_cents, total_ttc_cents, amount_paid_cents, status, paid_date,
                 payment_method, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                number,
                customer_ids[spec.key],
                vehicle_ids[(spec.key, plan.vehicle_index)],
                job.code, job.label,
                issue_day.isoformat(), due_day.isoformat(),
                PAYMENT_TERMS_DAYS[spec.kind],
                total_ht, VAT_RATE_BP, vat, total_ttc,
                amount_paid, plan.status, paid_date, payment_method,
                f"{issue_day.isoformat()}T17:30:00",
            ),
        )
        invoice_id = cursor.lastrowid

        conn.executemany(
            """
            INSERT INTO invoice_lines
                (invoice_id, position, label, kind, quantity_milli, unit,
                 unit_price_ht_cents, total_ht_cents)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    invoice_id, line["position"], line["label"], line["kind"],
                    line["quantity_milli"], line["unit"],
                    line["unit_price_ht_cents"], line["total_ht_cents"],
                )
                for line in lines
            ],
        )

    conn.commit()

    history_count = seed_followup_history(conn, reference_day)

    return {
        "followup_history": history_count,
        "customers": len(CUSTOMERS),
        "vehicles": len(vehicle_ids),
        "invoices": len(prepared),
        "reference_day": reference_day.isoformat(),
    }



# ---------------------------------------------------------------------------
# Follow-up history
# ---------------------------------------------------------------------------

def seed_followup_history(conn: sqlite3.Connection,
                          reference_day: date | None = None) -> int:
    """Record the follow-ups that were sent on invoices since settled.

    Only paid invoices are given a history. Overdue ones are left untouched on
    purpose: a follow-up recorded against a live invoice would close that level
    and the demo queue would come up empty.

    Bodies are archival stubs — the dashboard counts these, it does not display
    them. The live queue holds the real messages.
    """
    from .config import LEVEL_CHANNEL, LEVEL_THRESHOLD_DAYS
    from .overdue import level_for_days

    reference_day = reference_day or today_fn()
    created = 0

    rows = conn.execute(
        "SELECT id, number, due_date, paid_date, total_ttc_cents "
        "FROM invoices WHERE status = 'paid' AND paid_date IS NOT NULL"
    ).fetchall()

    for row in rows:
        due = date.fromisoformat(row["due_date"])
        paid = date.fromisoformat(row["paid_date"])
        days_late = (paid - due).days
        level = level_for_days(days_late)
        if level < 1:
            continue          # settled before the first threshold: no follow-up

        sent_on = due + timedelta(days=LEVEL_THRESHOLD_DAYS[level])
        if sent_on > reference_day:
            continue

        conn.execute(
            """
            INSERT INTO followups
                (invoice_id, level, channel, status, subject, body, days_overdue,
                 amount_due_cents, penalty_cents, recovery_fee_cents,
                 total_claimed_cents, generator, guardrail_ok, guardrail_note,
                 created_at, decided_at, sent_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,?)
            """,
            (
                row["id"], level, LEVEL_CHANNEL[level], "sent",
                f"Facture {row['number']} — relance",
                f"Relance de niveau {level} envoyée le {sent_on.isoformat()}. "
                f"Facture réglée le {row['paid_date']}.",
                LEVEL_THRESHOLD_DAYS[level],
                row["total_ttc_cents"], 0, 0, row["total_ttc_cents"],
                "mock",
                f"{sent_on.isoformat()}T09:00:00",
                f"{sent_on.isoformat()}T09:00:00",
                f"{sent_on.isoformat()}T09:00:00",
            ),
        )
        created += 1

    conn.commit()
    return created


# ---------------------------------------------------------------------------
# Sanity checks — the demo must never show an implausible figure
# ---------------------------------------------------------------------------

def check(conn: sqlite3.Connection, reference_day: date | None = None) -> list[str]:
    problems: list[str] = []
    reference_day = reference_day or today_fn()

    rows = conn.execute("SELECT * FROM invoices").fetchall()
    if len(rows) != len(PLAN):
        problems.append(f"expected {len(PLAN)} invoices, found {len(rows)}")

    for row in rows:
        if not (MIN_INVOICE_TTC_CENTS <= row["total_ttc_cents"] <= MAX_INVOICE_TTC_CENTS):
            problems.append(
                f"{row['number']}: total {format_eur(row['total_ttc_cents'])} outside "
                f"the 80 € – 2 400 € range"
            )
        if row["vat_cents"] != vat_of(row["total_ht_cents"], row["vat_rate_bp"]):
            problems.append(f"{row['number']}: VAT does not match the net total")
        if row["total_ht_cents"] + row["vat_cents"] != row["total_ttc_cents"]:
            problems.append(f"{row['number']}: HT + TVA does not equal TTC")
        if row["amount_paid_cents"] > row["total_ttc_cents"]:
            problems.append(f"{row['number']}: paid more than invoiced")
        if row["status"] == "paid" and row["amount_paid_cents"] != row["total_ttc_cents"]:
            problems.append(f"{row['number']}: marked paid but balance is not zero")
        if row["status"] == "unpaid" and row["amount_paid_cents"] != 0:
            problems.append(f"{row['number']}: marked unpaid but has a payment")
        if row["status"] == "partial" and not (0 < row["amount_paid_cents"] < row["total_ttc_cents"]):
            problems.append(f"{row['number']}: partial payment out of bounds")
        if date.fromisoformat(row["issue_date"]) >= date.fromisoformat(row["due_date"]):
            problems.append(f"{row['number']}: due date is not after the issue date")
        if row["paid_date"]:
            paid_on = date.fromisoformat(row["paid_date"])
            if paid_on > reference_day:
                problems.append(f"{row['number']}: paid in the future")
            if paid_on < date.fromisoformat(row["issue_date"]):
                problems.append(f"{row['number']}: paid before the invoice was issued")

    for row in conn.execute("SELECT invoice_id, SUM(total_ht_cents) AS s FROM invoice_lines GROUP BY invoice_id"):
        invoice = conn.execute(
            "SELECT number, total_ht_cents FROM invoices WHERE id = ?", (row["invoice_id"],)
        ).fetchone()
        if row["s"] != invoice["total_ht_cents"]:
            problems.append(f"{invoice['number']}: line total does not match the invoice total")

    # A believable garage ledger is mostly settled. An owner shown a book that
    # is two thirds unpaid does not recognise his own business.
    paid = conn.execute("SELECT COUNT(*) FROM invoices WHERE status = 'paid'").fetchone()[0]
    share = round(100 * paid / len(rows)) if rows else 0
    if not (55 <= share <= 65):
        problems.append(f"expected around 60 % settled invoices, found {share} % ({paid}/{len(rows)})")

    still_due = conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE status IN ('unpaid','partial')"
    ).fetchone()[0]
    if still_due != 28:
        problems.append(f"the overdue book should stay at 28 invoices, found {still_due}")

    plates = conn.execute("SELECT COUNT(DISTINCT plate), COUNT(*) FROM vehicles").fetchone()
    if plates[0] != plates[1]:
        problems.append("duplicate licence plates")

    return problems


def main(argv: list[str]) -> int:
    conn = db_module.connect()
    summary = generate(conn)
    print(
        f"Base générée : {summary['invoices']} factures, "
        f"{summary['customers']} clients, {summary['vehicles']} véhicules, "
        f"{summary['followup_history']} relances archivées "
        f"(date de référence {summary['reference_day']})"
    )
    print(f"Fichier : {DB_PATH}")

    if "--check" in argv:
        problems = check(conn)
        if problems:
            print(f"\n{len(problems)} problème(s) :")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print("\nContrôles de cohérence : OK")
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:  # pragma: no cover
        pass
    raise SystemExit(main(sys.argv[1:]))
