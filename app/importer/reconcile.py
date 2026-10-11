"""The second import.

A garage imports every week, and the second file is mostly the first file
again. Recreating what is already there would destroy the two things the owner
cannot rebuild by hand:

* **the penalty terms he confirmed**, customer by customer, against signed CGV;
* **the follow-up history** — a level he marked *Ignoré* would be proposed
  again, and the activity counters would reset every Monday.

So a re-import is not an import, it is a reconciliation.

  Invoices  match on their number. Same number means update, never insert, and
            an update may only move the payment: amount paid, status, paid
            date. A changed total is not a normal update — it is flagged and
            not applied, because something happened (credit note, re-invoicing)
            that the owner has to look at.

  Customers match on a normalised name — case, accents, punctuation, legal form
            and word order all ignored, so "SARL Bonnet", "Bonnet SARL" and
            "BONNET S.A.R.L" are one customer — with the e-mail as a stronger
            key when the file carries one. Confirmed terms, CGV reference and
            rate are never touched.

  Absences  are never deletions. A file covering a shorter date range is normal;
            the invoices it does not mention are left alone and counted.

Nothing in this module writes. It produces a plan; `apply` in `runner.py`
carries it out only after the owner confirms.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

# Legal forms carry no identity: the same garage customer writes them
# differently on every document.
_LEGAL_FORMS = frozenset({
    "sarl", "sas", "sasu", "sa", "eurl", "sci", "snc", "scop", "scm", "selarl",
    "ei", "eirl", "gie", "sarlu", "sca", "scs", "earl", "gaec", "association",
    "ets", "etablissements", "cie", "et cie", "entreprise", "societe", "ste",
})

# Courtesy titles are not part of a name either.
_TITLES = frozenset({"m", "mr", "mme", "mlle", "monsieur", "madame",
                     "mademoiselle", "dr", "me"})


def normalise_name(name: str) -> str:
    """The key two spellings of the same customer share.

    `SARL Bonnet`, `Bonnet SARL` and `BONNET S.A.R.L` all become `bonnet`.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()

    # Collapse dotted acronyms before anything else: `s.a.r.l` has to become
    # `sarl` in one piece, or the legal-form filter below sees four letters and
    # keeps them. A single initial like `m.` is left alone — it is a title, and
    # the title filter handles it.
    text = re.sub(r"(?:[a-z]\.){2,}", lambda match: match.group().replace(".", ""), text)

    text = re.sub(r"[^a-z0-9]+", " ", text)

    words = [word for word in text.split() if word]
    kept = [
        word for word in words
        if word not in _LEGAL_FORMS and word not in _TITLES
    ]
    if not kept:                       # a name made only of a legal form
        kept = words
    return " ".join(sorted(kept))


def normalise_number(number: str) -> str:
    """Invoice numbers differ only by spacing and case between exports."""
    return re.sub(r"\s+", "", str(number or "")).upper()


def normalise_email(email: str | None) -> str:
    return (email or "").strip().lower()


def normalise_label(value: str | None) -> str:
    """Case/accent/spacing-insensitive comparison for descriptive fields."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def normalise_plate(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


# ---------------------------------------------------------------------------
# What the reconciliation decides about one row
# ---------------------------------------------------------------------------

@dataclass
class RowPlan:
    line: int                        # line number in the file, 1-based
    action: str                      # 'insert' | 'update' | 'unchanged'
                                     #   | 'flag' | 'reject'
    number: str = ""
    customer_name: str = ""
    reasons: list[str] = field(default_factory=list)     # French
    warnings: list[str] = field(default_factory=list)    # French

    invoice_id: int | None = None
    customer_id: int | None = None
    customer_is_new: bool = False

    total_ttc_cents: int = 0
    amount_paid_cents: int = 0
    status: str = "unpaid"
    becomes_settled: bool = False
    existing_total_cents: int | None = None

    data: dict = field(default_factory=dict)
    changed_fields: list[str] = field(default_factory=list)


@dataclass
class Reconciliation:
    rows_read: int = 0
    inserts: list[RowPlan] = field(default_factory=list)
    updates: list[RowPlan] = field(default_factory=list)
    # Invoices found in the ledger with nothing to change. Recognition is a
    # result, not the absence of one: for a garage re-importing the same
    # export every week this is the normal outcome, and a screen that reports
    # only deltas makes "seen, nothing changed" look exactly like "not seen
    # at all" — which is a failure the owner cannot tell apart from success.
    unchanged: list[RowPlan] = field(default_factory=list)
    flagged: list[RowPlan] = field(default_factory=list)
    rejects: list[RowPlan] = field(default_factory=list)

    new_customers: list[str] = field(default_factory=list)
    matched_customers: int = 0
    # Two different names carrying one address. Both customers are created —
    # the name is the identity — but the owner is told, because one of the two
    # addresses is probably wrong and only he can say which.
    shared_emails: list[dict] = field(default_factory=list)
    # Customers who will end up with no e-mail address: their follow-ups can
    # only leave by post.
    customers_without_email: list[str] = field(default_factory=list)
    absent_invoices: int = 0
    absent_examples: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    inferred_due_dates: int = 0
    closed_followups: list = field(default_factory=list)   # ClosedFollowup

    @property
    def settled_count(self) -> int:
        return sum(1 for row in self.updates if row.becomes_settled)

    @property
    def total_written(self) -> int:
        return len(self.inserts) + len(self.updates)

    @property
    def headline(self) -> str:
        """The one line the owner reads before deciding."""
        parts = []
        if self.unchanged:
            parts.append(f"{len(self.unchanged)} déjà connue(s), inchangée(s)")
        parts += [
            f"{len(self.inserts)} nouvelle(s)",
            f"{len(self.updates)} mise(s) à jour"
            + (f", dont {self.settled_count} désormais réglée(s)"
               if self.settled_count else ""),
            f"{len(self.new_customers)} nouveau(x) client(s)",
        ]
        refused = len(self.rejects) + len(self.flagged)
        if refused:
            parts.append(f"{refused} écartée(s)")
        if self.closed_followups:
            parts.append(f"{len(self.closed_followups)} relance(s) sans objet")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# Existing state
# ---------------------------------------------------------------------------

@dataclass
class ClosedFollowup:
    followup_id: int
    invoice_number: str
    customer_name: str
    level: int
    was_status: str                       # pending / validated / handed_over / rejected
    reason: str = "Facture désormais réglée."


@dataclass
class ExistingState:
    invoices: dict[str, sqlite3.Row]      # normalised number -> row
    by_match_key: dict[str, int]          # normalised name -> customer id
    # Not an identity index: the name decides who a customer is. This only
    # serves the warning when two different names share one address.
    by_email: dict[str, int]              # normalised email -> customer id
    customer_names: dict[int, str]
    open_followups: dict[int, list]       # invoice id -> still-open follow-ups
    customer_emails: dict[int, str] = field(default_factory=dict)
    manual_payments: set[int] = field(default_factory=set)


def load_existing(conn: sqlite3.Connection) -> ExistingState:
    invoices = {}
    for row in conn.execute(
        "SELECT i.id, i.number, i.total_ttc_cents, i.amount_paid_cents, "
        "i.status, i.paid_date, i.customer_id, i.issue_date, i.due_date, "
        "i.job_label, i.vehicle_id, v.plate, v.make, v.model "
        "FROM invoices i LEFT JOIN vehicles v ON v.id = i.vehicle_id"
    ):
        invoices[normalise_number(row["number"])] = row

    by_match_key: dict[str, int] = {}
    by_email: dict[str, int] = {}
    names: dict[int, str] = {}
    emails: dict[int, str] = {}
    for row in conn.execute(
        "SELECT id, name, email, match_key FROM customers"
    ):
        names[row["id"]] = row["name"]
        emails[row["id"]] = row["email"] or ""
        key = row["match_key"] or normalise_name(row["name"])
        by_match_key.setdefault(key, row["id"])
        email = normalise_email(row["email"])
        if email:
            by_email.setdefault(email, row["id"])

    # Follow-ups still in play. If the file says the invoice is settled, these
    # have to be closed: a draft sitting in the owner's mailbox that chases a
    # customer who has already paid is the damage this whole step exists to
    # prevent.
    open_followups: dict[int, list] = {}
    for row in conn.execute(
        "SELECT id, invoice_id, level, status FROM followups "
        "WHERE status IN ('pending', 'validated', 'handed_over', 'rejected')"
    ):
        open_followups.setdefault(row["invoice_id"], []).append(
            {"id": row["id"], "level": row["level"], "status": row["status"]}
        )

    manual_payments = {row[0] for row in conn.execute("SELECT DISTINCT invoice_id FROM payment_changes")}
    return ExistingState(invoices, by_match_key, by_email, names,
                         open_followups, emails, manual_payments)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def _status_for(total_cents: int, paid_cents: int) -> str:
    if paid_cents <= 0:
        return "unpaid"
    if paid_cents >= total_cents:
        return "paid"
    return "partial"


def reconcile(parsed_rows, existing: ExistingState) -> Reconciliation:
    """Decide what each parsed row would do, without doing any of it.

    `parsed_rows` are the output of `runner.parse_rows`: each already carries
    its own rejection reason if the file could not be read at that line.
    """
    plan = Reconciliation(rows_read=len(parsed_rows))

    seen_numbers: dict[str, int] = {}
    # A customer created earlier in this same file must be reused by later
    # rows, or one file would create the same customer several times.
    pending_customers: dict[str, str] = {}
    pending_emails: dict[str, str] = {}

    for parsed in parsed_rows:
        if parsed.problem:
            plan.rejects.append(RowPlan(
                line=parsed.line, action="reject",
                number=parsed.raw_number or "",
                customer_name=parsed.raw_customer or "",
                reasons=[parsed.problem],
            ))
            continue

        key = normalise_number(parsed.number)

        if key in seen_numbers:
            plan.rejects.append(RowPlan(
                line=parsed.line, action="reject", number=parsed.number,
                customer_name=parsed.customer_name,
                reasons=[
                    f"Numéro en double dans le fichier : « {parsed.number} » "
                    f"apparaît déjà ligne {seen_numbers[key]}."
                ],
            ))
            continue
        seen_numbers[key] = parsed.line

        if parsed.due_date_inferred:
            plan.inferred_due_dates += 1

        known = existing.invoices.get(key)

        # --- which customer -------------------------------------------------
        name_key = normalise_name(parsed.customer_name)
        email_key = normalise_email(parsed.email)

        customer_id = None
        customer_is_new = False
        warnings = list(parsed.warnings)

        # The name decides who this is. An address does not: a driving school
        # with several vehicles, a fleet run by one manager, or an owner who
        # types his own address when he has not got the customer's all put one
        # address on genuinely different customers.
        #
        # The asymmetry settles it, as it did for the ambiguous separator.
        # Merging wrongly sends a follow-up under the wrong name for someone
        # else's debt, and that customer reads it. Not merging wrongly leaves a
        # duplicate line in the customer list, which nobody ever sees.
        #
        # Normalisation has already absorbed case, accents, punctuation, legal
        # form and word order, so a name that still differs after it is a real
        # difference and not a spelling variant.
        if known is not None:
            invoice_customer = existing.customer_names.get(known["customer_id"], "")
            if normalise_name(invoice_customer) != name_key:
                plan.flagged.append(RowPlan(
                    line=parsed.line, action="flag", number=parsed.number,
                    customer_name=parsed.customer_name,
                    invoice_id=known["id"], customer_id=known["customer_id"],
                    total_ttc_cents=parsed.total_ttc_cents,
                    existing_total_cents=known["total_ttc_cents"],
                    warnings=warnings,
                    reasons=[
                        f"Cette facture est déjà rattachée à « {invoice_customer} », "
                        f"mais le fichier indique « {parsed.customer_name} ». Rien "
                        "n’est modifié : vérifiez le numéro de facture et le client."
                    ],
                    data=parsed.as_dict(),
                ))
                continue
            # An existing invoice owns its customer identity. This avoids an
            # arbitrary same-name duplicate becoming the target of a re-import.
            customer_id = known["customer_id"]
        elif name_key in existing.by_match_key:
            customer_id = existing.by_match_key[name_key]
        elif name_key in pending_customers:
            customer_is_new = True
        else:
            customer_is_new = True
            pending_customers[name_key] = parsed.customer_name
            plan.new_customers.append(parsed.customer_name)

        if customer_id is not None:
            plan.matched_customers += 1

        # A shared address is still worth a word: one of the two is probably a
        # mistake in the file, and only the owner can say which.
        if email_key:
            sharer = None
            known_id = existing.by_email.get(email_key)
            if known_id is not None and known_id != customer_id:
                sharer = existing.customer_names.get(known_id)
            elif email_key in pending_emails and (
                    normalise_name(pending_emails[email_key]) != name_key):
                sharer = pending_emails[email_key]
            if sharer:
                warnings.append(
                    f"« {parsed.customer_name} » et « {sharer} » partagent "
                    f"l’adresse {parsed.email}. Ils sont traités comme deux "
                    f"clients distincts. Si c’est le même, corrigez l’adresse "
                    f"et réimportez."
                )
                plan.shared_emails.append({
                    "line": parsed.line,
                    "number": parsed.number,
                    "name": parsed.customer_name,
                    "other_name": sharer,
                    "email": parsed.email or "",
                })
            pending_emails.setdefault(email_key, parsed.customer_name)

        # --- insert or update ------------------------------------------------
        if known is None:
            plan.inserts.append(RowPlan(
                line=parsed.line, action="insert", number=parsed.number,
                customer_name=parsed.customer_name,
                reasons=[], warnings=warnings,
                customer_id=customer_id, customer_is_new=customer_is_new,
                total_ttc_cents=parsed.total_ttc_cents,
                amount_paid_cents=parsed.amount_paid_cents,
                status=_status_for(parsed.total_ttc_cents, parsed.amount_paid_cents),
                data=parsed.as_dict(),
            ))
            continue

        # A changed total is not a payment update. Flag it, apply nothing:
        # the basis of the debt moved, and touching the payment state of a row
        # whose basis moved is exactly the silent behaviour to avoid.
        if parsed.total_ttc_cents != known["total_ttc_cents"]:
            plan.flagged.append(RowPlan(
                line=parsed.line, action="flag", number=parsed.number,
                customer_name=parsed.customer_name,
                invoice_id=known["id"], customer_id=customer_id,
                total_ttc_cents=parsed.total_ttc_cents,
                existing_total_cents=known["total_ttc_cents"],
                warnings=warnings,
                reasons=[
                    "Le total de cette facture a changé depuis le dernier "
                    "import. Rien n’est modifié : vérifiez s’il s’agit d’un "
                    "avoir ou d’une refacturation."
                ],
                data=parsed.as_dict(),
            ))
            continue

        # Dates drive lateness and reminder level. Treating a corrected due
        # date as "unchanged" would keep the wrong workflow while telling the
        # owner the file was fully recognised. These explicitly mapped fields
        # are safe to reconcile after confirmation; the accounting total and
        # customer identity remain protected above.
        changed_fields: list[str] = []
        if parsed.issue_date.isoformat() != known["issue_date"]:
            changed_fields.append("date de facture")
        if parsed.due_date.isoformat() != known["due_date"]:
            changed_fields.append("échéance")
        if (
            parsed.job_label_provided
            and normalise_label(parsed.job_label) != normalise_label(known["job_label"])
        ):
            changed_fields.append("libellé")

        # A vehicle change can mean a corrected plate, a vehicle sold to
        # another customer, or the wrong invoice number. Do not guess which.
        known_vehicle = " ".join(
            part for part in (known["make"] or "", known["model"] or "") if part
        )
        plate_changed = bool(parsed.plate) and normalise_plate(
            parsed.plate
        ) != normalise_plate(known["plate"])
        vehicle_changed = bool(parsed.vehicle) and normalise_label(
            parsed.vehicle
        ) != normalise_label(known_vehicle)
        if plate_changed or vehicle_changed:
            plan.flagged.append(RowPlan(
                line=parsed.line, action="flag", number=parsed.number,
                customer_name=parsed.customer_name,
                invoice_id=known["id"], customer_id=customer_id,
                total_ttc_cents=parsed.total_ttc_cents,
                existing_total_cents=known["total_ttc_cents"],
                warnings=warnings,
                reasons=[
                    "Le véhicule de cette facture diffère du véhicule déjà "
                    "enregistré. Rien n’est modifié : vérifiez la facture et "
                    "l’immatriculation."
                ],
                data=parsed.as_dict(),
            ))
            continue

        new_status = _status_for(parsed.total_ttc_cents, parsed.amount_paid_cents)

        payment_changed = (
            parsed.amount_paid_cents != known["amount_paid_cents"]
            or new_status != known["status"]
        )
        if payment_changed and known["id"] in existing.manual_payments:
            plan.flagged.append(RowPlan(
                line=parsed.line, action="flag", number=parsed.number,
                customer_name=parsed.customer_name, invoice_id=known["id"],
                customer_id=customer_id, total_ttc_cents=parsed.total_ttc_cents,
                existing_total_cents=known["total_ttc_cents"], warnings=warnings,
                reasons=["Le paiement a été renseigné manuellement et diffère du fichier. "
                         "Rien n’est modifié. Vérifiez le paiement depuis la fiche facture avant de réimporter."],
                data=parsed.as_dict(),
            ))
            continue
        if payment_changed:
            changed_fields.append("règlement")

        # Every material change invalidates an unsent draft frozen from the old
        # amount/dates. Settled invoices close it permanently; other changes
        # let draft creation immediately replace it with a recalculated one.
        if new_status == "paid" or changed_fields:
            reason = (
                "Facture désormais réglée."
                if new_status == "paid"
                else "Montant, date ou libellé mis à jour : la relance sera recalculée."
            )
            for followup in existing.open_followups.get(known["id"], []):
                plan.closed_followups.append(ClosedFollowup(
                    followup_id=followup["id"],
                    invoice_number=parsed.number,
                    customer_name=parsed.customer_name,
                    level=followup["level"],
                    was_status=followup["status"],
                    reason=reason,
                ))

        unchanged = not changed_fields
        if unchanged:
            # Nothing to write — but something to say.
            plan.unchanged.append(RowPlan(
                line=parsed.line, action="unchanged", number=parsed.number,
                customer_name=parsed.customer_name,
                invoice_id=known["id"], customer_id=customer_id,
                customer_is_new=customer_is_new,
                total_ttc_cents=parsed.total_ttc_cents,
                amount_paid_cents=parsed.amount_paid_cents,
                status=new_status, warnings=warnings,
                data=parsed.as_dict(), changed_fields=[],
            ))
            continue

        update_data = parsed.as_dict()
        if not parsed.job_label_provided:
            update_data["job_label"] = known["job_label"]
        plan.updates.append(RowPlan(
            line=parsed.line, action="update", number=parsed.number,
            customer_name=parsed.customer_name,
            invoice_id=known["id"], customer_id=customer_id,
            customer_is_new=customer_is_new,
            total_ttc_cents=parsed.total_ttc_cents,
            amount_paid_cents=parsed.amount_paid_cents,
            status=new_status,
            becomes_settled=(new_status == "paid" and known["status"] != "paid"),
            warnings=warnings,
            data=update_data, changed_fields=changed_fields,
        ))

    # --- what the file does not mention ------------------------------------
    # Never a deletion: the export may simply cover a shorter period.
    absent = [
        row for key, row in existing.invoices.items()
        if key not in seen_numbers
    ]
    plan.absent_invoices = len(absent)
    plan.absent_examples = [row["number"] for row in absent[:5]]

    # Only rows that will actually be written: a customer whose invoice is
    # rejected is not a customer this import creates.
    seen_labels: set[str] = set()
    for row in plan.inserts + plan.updates:
        address = normalise_email(row.data.get("email"))
        if not address and row.customer_id is not None:
            address = normalise_email(existing.customer_emails.get(row.customer_id))
        if address:
            continue
        label = existing.customer_names.get(row.customer_id) or row.customer_name
        if label and label not in seen_labels:
            seen_labels.add(label)
            plan.customers_without_email.append(label)

    return plan
