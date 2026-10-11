"""Print the generated dataset in the terminal, the way the owner reads it.

    python scripts/show_data.py            # summary + overdue list
    python scripts/show_data.py --lines FA-2026-1204   # detail of one invoice
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module                       # noqa: E402
from app.config import LEVEL_THRESHOLD_DAYS, today    # noqa: E402
from app.money import format_eur, format_qty          # noqa: E402

BAR = "─" * 78


def _days_overdue(due_date: str, reference: date) -> int:
    return (reference - date.fromisoformat(due_date)).days


def _level_of(days: int) -> int:
    level = 0
    for candidate, threshold in sorted(LEVEL_THRESHOLD_DAYS.items()):
        if days >= threshold:
            level = candidate
    return level


LEVEL_NAMES = {
    0: "sous le seuil",
    1: "rappel courtois",
    2: "relance ferme",
    3: "dernier avis",
    4: "mise en demeure",
}


def main(argv: list[str]) -> int:
    conn = db_module.connect()
    reference = today()

    if "--lines" in argv:
        number = argv[argv.index("--lines") + 1]
        return show_invoice(conn, number)

    rows = conn.execute(
        """
        SELECT i.*, c.name AS customer_name, c.kind AS customer_kind,
               v.plate, v.make, v.model
        FROM invoices i
        JOIN customers c ON c.id = i.customer_id
        JOIN vehicles  v ON v.id = i.vehicle_id
        ORDER BY i.due_date
        """
    ).fetchall()

    invoiced = sum(r["total_ttc_cents"] for r in rows)
    collected = sum(r["amount_paid_cents"] for r in rows)
    outstanding = invoiced - collected

    print(BAR)
    print(f"  GARAGE MARTEL & FILS — jeu de démonstration au {reference.isoformat()}")
    print(BAR)
    print(f"  Factures            : {len(rows)}")
    print(f"  Total facturé TTC   : {format_eur(invoiced):>14}")
    print(f"  Encaissé            : {format_eur(collected):>14}")
    print(f"  Reste à encaisser   : {format_eur(outstanding):>14}")
    print()

    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    labels = {"paid": "réglées", "unpaid": "impayées", "partial": "acompte versé"}
    print("  Statuts : " + "   ".join(
        f"{labels[key]} {value}" for key, value in sorted(by_status.items())
    ))

    buckets: dict[int, list] = {0: [], 1: [], 2: [], 3: [], 4: []}
    not_due = []
    for row in rows:
        if row["status"] == "paid":
            continue
        days = _days_overdue(row["due_date"], reference)
        if days <= 0:
            not_due.append(row)
        else:
            buckets[_level_of(days)].append((days, row))

    print()
    print("  Répartition des créances")
    print(f"    non échues                    : {len(not_due):>2}   "
          f"{format_eur(sum(r['total_ttc_cents'] - r['amount_paid_cents'] for r in not_due)):>12}")
    for level in (0, 1, 2, 3, 4):
        items = buckets[level]
        if not items:
            continue
        due = sum(r["total_ttc_cents"] - r["amount_paid_cents"] for _, r in items)
        tag = f"niveau {level} — {LEVEL_NAMES[level]}" if level else f"échues — {LEVEL_NAMES[0]}"
        print(f"    {tag:<30}: {len(items):>2}   {format_eur(due):>12}")

    print()
    print(BAR)
    print("  IMPAYÉES, DE LA PLUS RÉCENTE À LA PLUS ANCIENNE")
    print(BAR)
    header = (f"  {'Facture':<14}{'Client':<28}{'Véhicule':<22}{'Plaque':<11}"
              f"{'Retard':>7}{'Dû':>14}")
    print(header)
    print("  " + "-" * 94)
    overdue = sorted(
        ((_days_overdue(r["due_date"], reference), r) for r in rows if r["status"] != "paid"),
        key=lambda item: item[0],
    )
    for days, row in overdue:
        due = row["total_ttc_cents"] - row["amount_paid_cents"]
        vehicle = f"{row['make']} {row['model']}"
        late = f"J+{days}" if days > 0 else f"J{days}"
        flag = " *" if row["status"] == "partial" else ""
        print(f"  {row['number']:<14}{row['customer_name'][:26]:<28}{vehicle[:20]:<22}"
              f"{row['plate']:<11}{late:>7}{format_eur(due):>15}{flag}")
    print("  " + "-" * 94)
    print("  * un acompte a déjà été encaissé sur cette facture")
    print()
    conn.close()
    return 0


def show_invoice(conn, number: str) -> int:
    invoice = conn.execute(
        """
        SELECT i.*, c.name AS customer_name, c.address, c.postal_code, c.city,
               c.kind AS customer_kind, v.plate, v.make, v.model, v.year, v.mileage_km
        FROM invoices i
        JOIN customers c ON c.id = i.customer_id
        JOIN vehicles  v ON v.id = i.vehicle_id
        WHERE i.number = ?
        """,
        (number,),
    ).fetchone()
    if invoice is None:
        print(f"Facture {number} introuvable.")
        return 1

    print(BAR)
    print(f"  FACTURE {invoice['number']} — {invoice['job_label']}")
    print(BAR)
    print(f"  Client   : {invoice['customer_name']} ({invoice['customer_kind']})")
    print(f"             {invoice['address']}, {invoice['postal_code']} {invoice['city']}")
    print(f"  Véhicule : {invoice['make']} {invoice['model']} ({invoice['year']}) — "
          f"{invoice['plate']} — {invoice['mileage_km']:,} km".replace(",", " "))
    print(f"  Émise le : {invoice['issue_date']}    Échéance : {invoice['due_date']} "
          f"(à {invoice['payment_terms_days']} jours)")
    print()
    print(f"  {'Désignation':<42}{'Qté':>9}{'P.U. HT':>13}{'Total HT':>14}")
    print("  " + "-" * 78)
    units = {"forfait": "fft"}
    for line in conn.execute(
        "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY position",
        (invoice["id"],),
    ):
        qty = f"{format_qty(line['quantity_milli'])} {units.get(line['unit'], line['unit'])}"
        print(f"  {line['label'][:40]:<42}{qty:>9}"
              f"{format_eur(line['unit_price_ht_cents']):>13}"
              f"{format_eur(line['total_ht_cents']):>14}")
    print("  " + "-" * 78)
    vat_label = f"TVA {invoice['vat_rate_bp'] // 100} %"
    print(f"  {'Total HT':>64}{format_eur(invoice['total_ht_cents']):>14}")
    print(f"  {vat_label:>64}{format_eur(invoice['vat_cents']):>14}")
    print(f"  {'TOTAL TTC':>64}{format_eur(invoice['total_ttc_cents']):>14}")
    if invoice["amount_paid_cents"]:
        print(f"  {'Déjà réglé':>64}{format_eur(-invoice['amount_paid_cents']):>14}")
        print(f"  {'RESTE DÛ':>64}"
              f"{format_eur(invoice['total_ttc_cents'] - invoice['amount_paid_cents']):>14}")
    print()
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main(sys.argv[1:]))
