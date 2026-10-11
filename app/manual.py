"""Small, transaction-scoped owner actions. No sending or external services."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal

from .config import DEFAULT_PAYMENT_TERM_DAYS, today
from .email_address import is_valid
from .importer.reconcile import normalise_name, normalise_number


class Problem(ValueError):
    def __init__(self, message, field="", status=422):
        super().__init__(message)
        self.field, self.status = field, status


def text(form, key, label, limit=200, required=False):
    raw = str(form.get(key, ""))
    if any(ord(c) < 32 for c in raw):
        raise Problem(f"{label} : retirez les retours à la ligne et caractères invisibles.", key)
    value = " ".join(raw.split())
    if required and not value:
        raise Problem(f"{label} : ce champ est nécessaire.", key)
    if len(value) > limit:
        raise Problem(f"{label} : {limit} caractères maximum.", key)
    return value


def amount(value, field="amount", label="Montant"):
    value = str(value).strip().replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if not re.fullmatch(r"\d{1,9}([,.]\d{1,2})?", value):
        raise Problem(f"{label} : indiquez un montant en euros, par exemple 120,50.", field)
    return int(Decimal(value.replace(",", ".")) * 100)


def day(value, field, label):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
            raise ValueError()
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise Problem(f"{label} : choisissez une date valide.", field)


def fingerprint(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def client_version(row):
    return fingerprint(dict(row))


def payment_version(conn, row):
    revision = conn.execute("SELECT MAX(id) FROM payment_changes WHERE invoice_id=?", (row["id"],)).fetchone()[0]
    return fingerprint([row["amount_paid_cents"], row["status"], row["paid_date"], revision])


def retire_drafts(conn, invoice_ids, reason):
    for invoice_id in invoice_ids:
        conn.execute(
            "UPDATE followups SET status='obsolete', guardrail_note=? "
            "WHERE invoice_id=? AND status IN ('pending','validated','handed_over','rejected')",
            (reason, invoice_id),
        )


def save_client(conn, form, client_id=None):
    values = {key: text(form, key, label, limit, required) for key, label, limit, required in (
        ("name", "Nom du client", 200, True), ("email", "E-mail", 254, False),
        ("phone", "Téléphone", 40, False), ("address", "Adresse", 300, False),
        ("postal_code", "Code postal", 20, False), ("city", "Ville", 100, False),
        ("contact_name", "Personne à contacter", 200, False),
    )}
    if values["email"] and not is_valid(values["email"]):
        raise Problem("E-mail : vérifiez l’adresse, par exemple nom@entreprise.fr.", "email")
    values["email"] = values["email"].lower()
    kind = form.get("kind", "")
    if kind not in {"particulier", "professionnel"}:
        raise Problem("Choisissez Particulier ou Entreprise.", "kind")
    key = normalise_name(values["name"])
    if not key:
        raise Problem("Indiquez un nom de client complet.", "name")
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute("SELECT * FROM customers WHERE id=?", (client_id,)).fetchone() if client_id else None
        if client_id and old is None:
            raise Problem("Client introuvable.", status=404)
        if old and form.get("version") != client_version(old):
            raise Problem("Ce client a été modifié depuis l’ouverture du formulaire. Rechargez sa fiche avant de continuer.", status=409)
        for candidate in conn.execute("SELECT id, name, match_key FROM customers"):
            if candidate["id"] != client_id and (candidate["match_key"] or normalise_name(candidate["name"])) == key:
                raise Problem(f"Le client « {candidate['name']} » existe déjà. Retrouvez sa fiche dans Clients pour ajouter une facture ou modifier ses coordonnées.", "name", 409)
        if old:
            changed = any((old[k] or "") != v for k, v in values.items()) or old["kind"] != kind
            if changed:
                conn.execute(
                    "UPDATE customers SET name=?, email=?, phone=?, address=?, postal_code=?, city=?, contact_name=?, kind=?, kind_confirmed=1, match_key=?, "
                    "penalty_terms_confirmed=CASE WHEN kind=? THEN penalty_terms_confirmed ELSE 0 END WHERE id=?",
                    (*values.values(), kind, key, kind, client_id),
                )
                ids = [r[0] for r in conn.execute("SELECT id FROM invoices WHERE customer_id=?", (client_id,))]
                retire_drafts(conn, ids, "Coordonnées du client modifiées : vérifiez un nouveau message.")
        else:
            cursor = conn.execute(
                "INSERT INTO customers(name,email,phone,address,postal_code,city,contact_name,kind,kind_confirmed,match_key,payment_terms_days,customer_since,source,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,?,?,?,'manual',?)",
                (*values.values(), kind, key, DEFAULT_PAYMENT_TERM_DAYS, today().year, datetime.now().isoformat(timespec="seconds")),
            )
            client_id = cursor.lastrowid
        conn.commit()
        return client_id
    except Exception:
        conn.rollback()
        raise


def create_invoice(conn, form):
    number = text(form, "number", "Numéro de facture", 80, True)
    label = text(form, "job_label", "Libellé", 200) or "Prestation"
    total = amount(form.get("total", ""), "total", "Total TTC")
    if total <= 0:
        raise Problem("Le total TTC doit être supérieur à zéro.", "total")
    issued = day(form.get("issue_date"), "issue_date", "Date de facture")
    due = day(form.get("due_date"), "due_date", "Échéance")
    if due < issued:
        raise Problem("L’échéance ne peut pas précéder la date de facture.", "due_date")
    if issued > today():
        raise Problem("La date de facture ne peut pas être dans le futur.", "issue_date")
    try:
        customer_id = int(form.get("customer_id", ""))
    except (TypeError, ValueError):
        raise Problem("Choisissez un client.", "customer_id")
    state = form.get("payment_state", "unpaid")
    if state not in {"paid", "unpaid"}:
        raise Problem("Choisissez Payée ou À régler.", "payment_state")
    payment_date = day(form.get("payment_date"), "payment_date", "Date du paiement") if state == "paid" else None
    if payment_date and payment_date > today():
        raise Problem("Un paiement reçu ne peut pas être daté dans le futur.", "payment_date")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not conn.execute("SELECT id FROM customers WHERE id=?", (customer_id,)).fetchone():
            raise Problem("Ce client n’existe plus. Choisissez un client dans la liste.", "customer_id")
        if any(normalise_number(r[0]) == normalise_number(number) for r in conn.execute("SELECT number FROM invoices")):
            raise Problem("Cette facture existe déjà. Aucune nouvelle facture n’a été créée. Retrouvez-la dans Factures.", "number", 409)
        paid = total if state == "paid" else 0
        now = datetime.now().isoformat(timespec="seconds")
        # Tracking an existing invoice, not issuing an accounting document:
        # no tax breakdown is collected or inferred. These required legacy
        # fields are placeholders; reminders use only the entered TTC balance.
        cursor = conn.execute(
            "INSERT INTO invoices(number,customer_id,job_code,job_label,issue_date,due_date,payment_terms_days,total_ht_cents,vat_rate_bp,vat_cents,total_ttc_cents,amount_paid_cents,status,paid_date,source,created_at) "
            "VALUES (?,?,'manual_tracking',?,?,?,?,?,0,0,?,?,?,?,'manual',?)",
            (number, customer_id, label, issued.isoformat(), due.isoformat(), (due-issued).days, total,
             total, paid, state, payment_date.isoformat() if payment_date else None, now),
        )
        invoice_id = cursor.lastrowid
        if paid:
            conn.execute("INSERT INTO payment_changes(invoice_id,previous_paid_cents,amount_paid_cents,payment_date,note,created_at) VALUES (?,0,?,?,?,?)",
                         (invoice_id, paid, payment_date.isoformat(), "Déjà payée lors de l’ajout manuel.", now))
        conn.commit()
        return invoice_id
    except Exception:
        conn.rollback()
        raise


def update_payment(conn, invoice_id, form):
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise Problem("Facture introuvable.", status=404)
        if form.get("version") != payment_version(conn, row):
            raise Problem("Le paiement a changé depuis l’ouverture de cette page. Rechargez la facture avant de continuer.", status=409)
        action = form.get("payment_state")
        if action not in {"paid", "unpaid", "partial"}:
            raise Problem("Choisissez l’état du paiement.", "payment_state")
        total = row["total_ttc_cents"]
        paid = total if action == "paid" else 0
        if action == "partial":
            paid = amount(form.get("amount_paid", ""), "amount_paid", "Total déjà reçu")
            if not 0 < paid < total:
                raise Problem("Pour un paiement partiel, le total reçu doit être supérieur à zéro et inférieur au total de la facture.", "amount_paid")
        payment_date = day(form.get("payment_date"), "payment_date", "Date du paiement") if paid else None
        if payment_date and payment_date > today():
            raise Problem("Un paiement reçu ne peut pas être daté dans le futur.", "payment_date")
        note = text(form, "note", "Note", 300)
        previous = conn.execute("SELECT payment_date,note FROM payment_changes WHERE invoice_id=? ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
        previous_date = previous["payment_date"] if previous else row["paid_date"]
        previous_note = previous["note"] if previous else ""
        date_iso = payment_date.isoformat() if payment_date else None
        if (paid == row["amount_paid_cents"] and action == row["status"]
                and date_iso == previous_date and note == previous_note):
            conn.rollback()
            return False
        conn.execute("UPDATE invoices SET amount_paid_cents=?,status=?,paid_date=?,payment_method=? WHERE id=?",
                     (paid, action, payment_date.isoformat() if action == "paid" else None, "manual" if paid else None, invoice_id))
        conn.execute("INSERT INTO payment_changes(invoice_id,previous_paid_cents,amount_paid_cents,payment_date,note,created_at) VALUES (?,?,?,?,?,?)",
                     (invoice_id, row["amount_paid_cents"], paid, payment_date.isoformat() if payment_date else None, note, datetime.now().isoformat(timespec="seconds")))
        retire_drafts(conn, [invoice_id], "Paiement modifié manuellement : les anciens messages ne doivent plus être utilisés.")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
