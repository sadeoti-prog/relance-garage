"""Reading mapped rows, and writing them once the owner confirms.

Two halves, deliberately separate:

* `parse_rows` turns a mapped file into values, reporting per line what it
  could not read. It writes nothing.
* `apply` carries out a reconciliation plan inside one transaction, on the real
  database — never the demo one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..config import (
    DEFAULT_PAYMENT_TERM_DAYS, PAYMENT_TERMS_DAYS, VAT_RATE_BP,
    today as today_fn,
)
from ..email_address import is_valid as is_valid_email
from ..money import vat_of
from .convention import AmountConvention, detect_amount_convention
from .reconcile import Reconciliation, normalise_name
from .values import ValueProblem, parse_amount, parse_date, parse_text

# Words that mark a business customer on a French invoice. Used only to pick a
# payment term and a VAT-style label — never to decide penalty terms, which are
# always unconfirmed on import.
_BUSINESS_MARKERS = (
    "sarl", "sas", "sasu", "sa ", "eurl", "sci", "snc", "ets", "société",
    "societe", "entreprise", "cie", "selarl", "scm", "earl", "gaec",
    "association", "boulangerie", "garage", "transports", "ambulances",
    "pressing", "traiteur", "auto-école", "auto-ecole", "menuiserie",
    "plomberie", "toitures", "fromagerie", "boucherie", "taxis",
)


@dataclass
class ParsedRow:
    line: int
    problem: str | None = None
    warnings: list[str] = field(default_factory=list)

    raw_number: str = ""
    raw_customer: str = ""

    number: str = ""
    customer_name: str = ""
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    postal_code: str | None = None
    city: str | None = None
    vehicle: str | None = None
    plate: str | None = None
    job_label: str = "Prestation"
    job_label_provided: bool = False

    issue_date: date | None = None
    due_date: date | None = None
    due_date_inferred: bool = False

    total_ttc_cents: int = 0
    total_ht_cents: int | None = None
    vat_cents: int | None = None
    amount_paid_cents: int = 0
    kind: str = "particulier"
    payment_term_days: int | None = None

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "customer_name": self.customer_name,
            "email": self.email,
            "phone": self.phone,
            "address": self.address,
            "postal_code": self.postal_code,
            "city": self.city,
            "vehicle": self.vehicle,
            "plate": self.plate,
            "job_label": self.job_label,
            "job_label_provided": self.job_label_provided,
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "due_date_inferred": self.due_date_inferred,
            "total_ttc_cents": self.total_ttc_cents,
            "total_ht_cents": self.total_ht_cents,
            "vat_cents": self.vat_cents,
            "amount_paid_cents": self.amount_paid_cents,
            "kind": self.kind,
            "payment_term_days": self.payment_term_days,
        }


def guess_kind(name: str) -> str:
    lowered = f" {(name or '').lower()} "
    return "professionnel" if any(
        marker in lowered for marker in _BUSINESS_MARKERS
    ) else "particulier"


def column_conventions(source, mapping: dict[str, str | None]) -> dict[str, AmountConvention]:
    """One convention per mapped amount column, read from the whole column."""
    conventions = {}
    for key in ("total_ttc", "total_ht", "vat_amount", "amount_paid"):
        header = mapping.get(key)
        if not header:
            continue
        index = source.column(header)
        if index is None:
            continue
        values = [row[index] for row in source.rows if index < len(row)]
        conventions[key] = detect_amount_convention(values)
    return conventions


def parse_rows(source, mapping: dict[str, str | None],
               reference: date | None = None,
               payment_term_days: int | None = None) -> list[ParsedRow]:
    """Turn a mapped file into values. Writes nothing, reports every problem.

    `payment_term_days` is the answer the owner gave on the mapping screen; it
    is used only when the file has no due-date column, and every row that used
    it says so.
    """
    reference = reference or today_fn()
    if payment_term_days is None:
        payment_term_days = DEFAULT_PAYMENT_TERM_DAYS
    conventions = column_conventions(source, mapping)

    def cell(row, key):
        header = mapping.get(key)
        if not header:
            return None
        return source.cell(row, header)

    parsed_rows: list[ParsedRow] = []

    for offset, row in enumerate(source.rows):
        line = offset + 2                      # +1 for the header, +1 for 1-based
        parsed = ParsedRow(line=line)
        parsed.raw_number = parse_text(cell(row, "number")) or ""
        parsed.raw_customer = parse_text(cell(row, "customer_name")) or ""

        number = parse_text(cell(row, "number"))
        customer = parse_text(cell(row, "customer_name"))
        if not number:
            parsed.problem = "Numéro de facture absent."
            parsed_rows.append(parsed)
            continue
        if not customer:
            parsed.problem = "Nom du client absent."
            parsed_rows.append(parsed)
            continue

        parsed.number = number
        parsed.customer_name = customer
        # Guessed from the name, and marked as a guess: it picks a payment term
        # and nothing else. It never reaches the recovery indemnity.
        parsed.kind = guess_kind(customer)
        parsed.payment_term_days = payment_term_days

        try:
            issued = parse_date(cell(row, "issue_date"))
        except ValueProblem as error:
            parsed.problem = f"Date de facture : {error.reason}"
            parsed_rows.append(parsed)
            continue
        parsed.issue_date = issued.value
        if issued.warning:
            parsed.warnings.append(issued.warning)

        try:
            total = parse_amount(cell(row, "total_ttc"), conventions.get("total_ttc"))
        except ValueProblem as error:
            parsed.problem = f"Total TTC : {error.reason}"
            parsed_rows.append(parsed)
            continue
        if total.cents <= 0:
            parsed.problem = f"Total TTC nul ou négatif : {total.cents} centimes."
            parsed_rows.append(parsed)
            continue
        parsed.total_ttc_cents = total.cents
        if total.warning:
            parsed.warnings.append(total.warning)

        # --- due date, computed when the file has none, and said so ---------
        due_raw = cell(row, "due_date")
        if due_raw:
            try:
                due = parse_date(due_raw)
                parsed.due_date = due.value
                if due.warning:
                    parsed.warnings.append(due.warning)
            except ValueProblem as error:
                parsed.problem = f"Échéance : {error.reason}"
                parsed_rows.append(parsed)
                continue
        else:
            terms = payment_term_days
            parsed.due_date = parsed.issue_date + timedelta(days=terms)
            parsed.due_date_inferred = True
            spelled = "à réception" if terms == 0 else f"+ {terms} jours"
            parsed.warnings.append(
                f"Échéance absente du fichier : calculée à date de facture "
                f"{spelled}, soit le {parsed.due_date.strftime('%d/%m/%Y')}."
            )

        if parsed.due_date < parsed.issue_date:
            parsed.problem = "Échéance antérieure à la date de facture."
            parsed_rows.append(parsed)
            continue

        # --- optional amounts ------------------------------------------------
        paid_raw = cell(row, "amount_paid")
        if paid_raw:
            try:
                paid = parse_amount(paid_raw, conventions.get("amount_paid"))
                parsed.amount_paid_cents = max(0, paid.cents)
                if paid.warning:
                    parsed.warnings.append(paid.warning)
            except ValueProblem as error:
                parsed.problem = f"Montant réglé : {error.reason}"
                parsed_rows.append(parsed)
                continue

        if parsed.amount_paid_cents > parsed.total_ttc_cents:
            parsed.warnings.append(
                "Le montant réglé dépasse le total : la facture est traitée "
                "comme soldée."
            )
            parsed.amount_paid_cents = parsed.total_ttc_cents

        for key, target in (("total_ht", "total_ht_cents"), ("vat_amount", "vat_cents")):
            raw = cell(row, key)
            if not raw:
                continue
            try:
                value = parse_amount(raw, conventions.get(key))
                setattr(parsed, target, value.cents)
            except ValueProblem:
                pass                       # optional: a bad HT never blocks a row

        # A total that does not add up is reported, never silently corrected.
        if parsed.total_ht_cents is not None and parsed.vat_cents is not None:
            recomputed = parsed.total_ht_cents + parsed.vat_cents
            if recomputed != parsed.total_ttc_cents:
                parsed.warnings.append(
                    "HT + TVA ne correspond pas au TTC du fichier "
                    f"({recomputed} ≠ {parsed.total_ttc_cents} centimes). "
                    "Le TTC du fichier est conservé tel quel."
                )

        parsed.email = parse_text(cell(row, "email"))
        if parsed.email and not is_valid_email(parsed.email):
            parsed.warnings.append(
                "Adresse e-mail invalide ou contenant plusieurs destinataires : "
                "elle n’est pas enregistrée. Cette relance restera disponible "
                "par courrier."
            )
            parsed.email = None
        parsed.phone = parse_text(cell(row, "phone"))
        parsed.address = parse_text(cell(row, "address"))
        parsed.postal_code = parse_text(cell(row, "postal_code"))
        parsed.city = parse_text(cell(row, "city"))
        parsed.vehicle = parse_text(cell(row, "vehicle"))
        parsed.plate = parse_text(cell(row, "plate"))
        imported_job = parse_text(cell(row, "job_label"))
        parsed.job_label_provided = bool(imported_job)
        parsed.job_label = imported_job or "Prestation"

        if not parsed.email:
            parsed.warnings.append(
                "Pas d’adresse e-mail : ce client ne sera relançable que par "
                "courrier."
            )

        parsed_rows.append(parsed)

    return parsed_rows


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    inserted: int = 0
    updated: int = 0
    # Recognised in the ledger, nothing to change. Reported, never inferred
    # from "0 mise(s) à jour": that reads like a failure.
    unchanged: int = 0
    customers_created: int = 0
    customers_completed: int = 0
    skipped_flagged: int = 0
    rejected: int = 0
    followups_closed: int = 0
    drafts_created: int = 0
    drafts_blocked: int = 0
    draft_warning: str | None = None


def _customer_id_for(conn, data: dict, now: str) -> tuple[int, bool, bool]:
    """Find or create the customer. Returns (id, created, completed).

    Confirmed penalty terms, the CGV reference and the rate are never written
    here on an existing customer: they are the owner's work, not the file's.
    """
    name = data["customer_name"]
    key = normalise_name(name)

    # Matched on the name alone, exactly as the plan announced it. An e-mail
    # address identifies nobody: several genuinely different customers share
    # one, and filing an invoice under the wrong name is the one error the
    # customer himself gets to read.
    row = conn.execute(
        "SELECT * FROM customers WHERE match_key = ?", (key,)
    ).fetchone()

    if row is not None:
        # Fill in blanks only. A value the owner typed is never overwritten by
        # a file, and the penalty columns are not touched at all.
        completions = {}
        for column, value in (
            ("email", data.get("email")), ("phone", data.get("phone")),
            ("address", data.get("address")), ("postal_code", data.get("postal_code")),
            ("city", data.get("city")),
        ):
            if value and not (row[column] or "").strip():
                completions[column] = value
        if completions:
            assignments = ", ".join(f"{column} = ?" for column in completions)
            conn.execute(
                f"UPDATE customers SET {assignments} WHERE id = ?",
                (*completions.values(), row["id"]),
            )
        return row["id"], False, bool(completions)

    kind = data.get("kind", "particulier")
    cursor = conn.execute(
        """
        INSERT INTO customers
            (name, kind, contact_name, civility, email, phone, address,
             postal_code, city, siret, payment_terms_days, customer_since,
             is_regular, late_penalty_rate_bp, cgv_reference,
             penalty_terms_confirmed, match_key, source, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,0,NULL,NULL,0,?,'import',?)
        """,
        (
            name, kind, name, "",
            data.get("email") or "", data.get("phone") or "",
            data.get("address") or "", data.get("postal_code") or "",
            data.get("city") or "",
            data.get("payment_term_days") or PAYMENT_TERMS_DAYS.get(kind, 30),
            int(now[:4]),
            key, now,
        ),
    )
    return cursor.lastrowid, True, False


def _vehicle_id_for(conn, customer_id: int, data: dict) -> int | None:
    """Reuse the vehicle by plate, else create one. None when the file has none."""
    plate = (data.get("plate") or "").strip() or None
    label = (data.get("vehicle") or "").strip()
    if not plate and not label:
        return None

    if plate:
        row = conn.execute(
            "SELECT id FROM vehicles WHERE plate = ?", (plate,)
        ).fetchone()
        if row is not None:
            return row["id"]

    make, _, model = label.partition(" ")
    cursor = conn.execute(
        "INSERT INTO vehicles (customer_id, plate, make, model) VALUES (?,?,?,?)",
        (customer_id, plate, make, model),
    )
    return cursor.lastrowid


def apply(conn: sqlite3.Connection, plan: Reconciliation) -> ApplyResult:
    """Carry out a confirmed plan, in one transaction, on the real database."""
    result = ApplyResult(
        skipped_flagged=len(plan.flagged), rejected=len(plan.rejects),
        unchanged=len(plan.unchanged),
    )
    now = datetime.now().isoformat(timespec="seconds")

    try:
        # Acquire the writer lock before looking up or creating customers. Two
        # imports confirmed at the same time therefore cannot create parallel
        # copies of the same customer.
        conn.execute("BEGIN IMMEDIATE")

        for row in plan.inserts:
            data = row.data
            customer_id, created, completed = _customer_id_for(conn, data, now)
            result.customers_created += int(created)
            result.customers_completed += int(completed)

            vehicle_id = _vehicle_id_for(conn, customer_id, data)

            total = data["total_ttc_cents"]
            # The file gives a gross total; HT and VAT are derived for display
            # only, in integer cents, and never used to alter the total.
            vat = data.get("vat_cents")
            net = data.get("total_ht_cents")
            if net is None or vat is None:
                vat = vat_of(total * 10_000 // (10_000 + VAT_RATE_BP), VAT_RATE_BP)
                net = total - vat

            paid = data["amount_paid_cents"]
            status = "paid" if paid >= total else ("partial" if paid else "unpaid")

            conn.execute(
                """
                INSERT INTO invoices
                    (number, customer_id, vehicle_id, job_code, job_label,
                     issue_date, due_date, payment_terms_days, total_ht_cents,
                     vat_rate_bp, vat_cents, total_ttc_cents, amount_paid_cents,
                     status, paid_date, payment_method, source, imported_at,
                     created_at)
                VALUES (?,?,?,'import',?,?,?,?,?,?,?,?,?,?,?,NULL,'import',?,?)
                """,
                (
                    data["number"], customer_id, vehicle_id, data["job_label"],
                    data["issue_date"], data["due_date"],
                    (date.fromisoformat(data["due_date"])
                     - date.fromisoformat(data["issue_date"])).days,
                    net, VAT_RATE_BP, vat, total, paid, status,
                    # The export tells us the balance, not when it was paid.
                    # Inventing the due date here corrupts recovery KPIs.
                    None,
                    now, now,
                ),
            )
            result.inserted += 1

        for row in plan.updates:
            # The total and customer identity never move here. Explicitly
            # mapped dates and the job label are safe reconciliation fields;
            # they are listed to the owner before confirmation.
            conn.execute(
                """
                UPDATE invoices
                SET issue_date = ?, due_date = ?, job_label = ?,
                    amount_paid_cents = ?, status = ?, paid_date = NULL,
                    payment_terms_days = ?, imported_at = ?
                WHERE id = ?
                """,
                (
                    row.data["issue_date"], row.data["due_date"],
                    row.data["job_label"], row.amount_paid_cents, row.status,
                    (date.fromisoformat(row.data["due_date"])
                     - date.fromisoformat(row.data["issue_date"])).days,
                    now, row.invoice_id,
                ),
            )
            result.updated += 1

        for closed in plan.closed_followups:
            # Not "sent", not "skipped": without object. It never counts as a
            # follow-up that went out.
            conn.execute(
                "UPDATE followups SET status = 'obsolete', decided_at = ? "
                "WHERE id = ? AND status IN "
                "('pending', 'validated', 'handed_over', 'rejected')",
                (now, closed.followup_id),
            )
            result.followups_closed += 1

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return result
