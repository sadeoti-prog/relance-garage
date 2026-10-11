"""Put the demo back to its known-good starting point.

One command, run before every rehearsal and every recording, so the story is
identical each time:

    python scripts/reset_demo.py

It regenerates the ledger from the seed, rebuilds the archive of follow-ups
already sent, drafts today's queue, and prints the figures you should see on
screen. If those numbers do not match what the dashboard shows, something is
wrong — check before going live, not during.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import kpi                                                 # noqa: E402
from app import overdue as engine                                   # noqa: E402
from app.demo_reset import reset_demo                               # noqa: E402
from app.config import DEMO_MODE, LLM_MODE, today                   # noqa: E402
from app.labels_fr import LEVEL_NAME, format_date_long              # noqa: E402
from app.money import format_eur                                    # noqa: E402

BAR = "─" * 78


def main() -> int:
    reference = today()
    print(BAR)
    print("  REMISE À ZÉRO DE LA DÉMONSTRATION")
    print(BAR)

    result = reset_demo(reference)
    summary = result.summary
    print(f"  Grand livre régénéré   : {summary['invoices']} factures, "
          f"{summary['customers']} clients")
    print(f"  Relances archivées     : {summary['followup_history']}")

    print(f"  File de validation     : {result.queued} relances prêtes")
    if result.rejected:
        print(f"  Bloquées par contrôle  : {result.rejected}")

    from app import db as db_module
    from app import followups as followup_module
    conn = db_module.connect()

    book = engine.portfolio(conn, reference)
    activity = kpi.last_30_days(conn, reference)

    print()
    print(BAR)
    print(f"  CE QUE L’ÉCRAN DOIT AFFICHER — {format_date_long(reference)}")
    print(BAR)
    print(f"  Reste à encaisser      : {format_eur(book.outstanding_cents)}")
    print(f"  Dont échu              : {format_eur(book.overdue_cents)} "
          f"({book.overdue_count} factures, la plus ancienne à J+{book.oldest_days_overdue})")
    # The queue on screen, not the number of invoices still awaiting a draft:
    # once drafted, nothing is "due" any more, and the two would disagree.
    pending = len(followup_module.queue(conn, "pending"))
    print(f"  Relances à valider     : {pending}")
    for bucket in book.buckets:
        if bucket.level:
            print(f"      niveau {bucket.level} — {LEVEL_NAME[bucket.level]:<20}"
                  f"{bucket.count:>3} · {format_eur(bucket.amount_due_cents)}")
    print(f"  CGV non confirmées     : {len(kpi.unconfirmed_terms(conn, reference))} clients")
    print(f"  30 derniers jours      : {activity.followups_sent} relances envoyées · "
          f"{format_eur(activity.recovered_cents)} encaissés · "
          f"{activity.hours_saved_label} gagnées")
    print()
    print(f"  Mode démo : {'oui, aucun envoi' if DEMO_MODE else 'NON — ATTENTION'}"
          f"   ·   Moteur : {LLM_MODE}")
    print(BAR)
    print()
    print("  Prêt. Lancez start.bat (ou rechargez la page) et suivez le")
    print("  parcours de 90 secondes décrit dans le README.")
    print()

    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main())
