"""Show what the escalation engine decides today.

    python scripts/show_overdue.py             # portfolio + queue
    python scripts/show_overdue.py --all       # every unsettled invoice
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module                                    # noqa: E402
from app import overdue as engine                                   # noqa: E402
from app.config import (                                            # noqa: E402
    LEVEL_THRESHOLD_DAYS, PENALTY_FROM_LEVEL, RECOVERY_FEE_CENTS, today,
)
from app.labels_fr import (                                         # noqa: E402
    CHANNEL, LEVEL_INTENT, LEVEL_NAME, format_date, format_delay,
)
from app.money import format_eur                                    # noqa: E402

WIDTH = 96
BAR = "─" * WIDTH


def main(argv: list[str]) -> int:
    conn = db_module.connect()
    reference = today()
    book = engine.portfolio(conn, reference)

    print(BAR)
    print(f"  ENCOURS CLIENT AU {format_date(reference)}")
    print(BAR)
    settled_share = round(100 * book.settled_count / book.invoice_count)
    print(f"  Reste à encaisser        {format_eur(book.outstanding_cents):>16}"
          f"     sur {book.invoice_count} factures ({settled_share} % réglées)")
    print(f"    dont échu              {format_eur(book.overdue_cents):>16}"
          f"     {book.overdue_count} factures, la plus ancienne à J+{book.oldest_days_overdue}")
    print(f"    dont non échu          {format_eur(book.not_due_cents):>16}"
          f"     {book.not_due_count} factures")
    print()

    print(f"  {'Niveau':<32}{'Factures':>9}{'Montant dû':>16}{'Pénalités + IFR':>18}{'Seuil':>10}")
    print("  " + "-" * (WIDTH - 2))
    for bucket in book.buckets:
        threshold = (f"J+{LEVEL_THRESHOLD_DAYS[bucket.level]}" if bucket.level else "—")
        extra = format_eur(bucket.extra_cents) if bucket.extra_cents else "—"
        print(f"  {LEVEL_NAME[bucket.level]:<32}{bucket.count:>9}"
              f"{format_eur(bucket.amount_due_cents):>17}{extra:>19}{threshold:>10}")
    print("  " + "-" * (WIDTH - 2))
    print(f"  {'Total échu':<32}{book.overdue_count:>9}"
          f"{format_eur(book.overdue_cents):>17}"
          f"{format_eur(sum(b.extra_cents for b in book.buckets)):>19}")
    print()
    print(f"  Pénalités de retard : au taux propre à chaque client, à partir du "
          f"niveau {PENALTY_FROM_LEVEL}, et seulement si ses conditions sont confirmées.")
    print(f"  Indemnité forfaitaire de recouvrement : {format_eur(RECOVERY_FEE_CENTS)} "
          f"pour les clients professionnels uniquement.")
    if book.penalty_blocked_count:
        print(f"  ⚠  {book.penalty_blocked_count} facture(s) échue(s) sans conditions de "
              f"pénalité confirmées : aucune pénalité n’est réclamée dessus.")
    print()

    queue = engine.followups_due(conn, reference)
    print(BAR)
    print(f"  RELANCES À PRÉPARER AUJOURD’HUI — {len(queue)} "
          f"({format_eur(book.followups_due_cents)})")
    print(BAR)
    if not queue:
        print("  Rien à relancer aujourd’hui.")
    current_level = None
    for item in queue:
        if item.level != current_level:
            current_level = item.level
            print()
            print(f"  ▸ NIVEAU {item.level} — {LEVEL_NAME[item.level].upper()} "
                  f"· {CHANNEL[item.channel]}")
            print(f"    {LEVEL_INTENT[item.level]}")
            print()
        claims = []
        if item.penalty_cents:
            claims.append(f"+ pénalités {format_eur(item.penalty_cents)}")
        if item.recovery_fee_cents:
            claims.append(f"+ indemnité {format_eur(item.recovery_fee_cents)}")
        if claims:
            claims.append(f"= {format_eur(item.total_claimed_cents)} réclamés")
        if item.has_deposit:
            claims.append(f"· acompte de {format_eur(item.amount_paid_cents)} déjà encaissé")
        print(f"    {item.number:<14}{item.customer_name[:26]:<28}"
              f"{item.vehicle_label[:18]:<20}{format_delay(item.days_overdue):>20}"
              f"{format_eur(item.amount_due_cents):>13}")
        if claims:
            print(f"    {'':<14}" + "   ".join(claims))

    if "--all" in argv:
        print()
        print(BAR)
        print("  TOUTES LES FACTURES NON SOLDÉES")
        print(BAR)
        print(f"  {'Facture':<14}{'Client':<28}{'Échéance':>12}{'État':>34}"
              f"{'Dû':>14}")
        print("  " + "-" * (WIDTH - 2))
        for item in engine.load_all(conn, reference):
            if item.followup_is_due:
                state = f"→ niveau {item.level} à préparer"
            elif item.days_overdue <= 0:
                state = format_delay(item.days_overdue)
            elif item.level == 0:
                state = f"surveillance, niveau 1 dans {item.days_to_next_level} j"
            else:
                state = f"niveau {item.level} traité"
            print(f"  {item.number:<14}{item.customer_name[:26]:<28}"
                  f"{format_date(item.due_date):>12}{state:>34}"
                  f"{format_eur(item.amount_due_cents):>15}")

    print()
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main(sys.argv[1:]))
