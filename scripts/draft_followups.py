"""Draft today's follow-ups and show them.

    python scripts/draft_followups.py                 # draft all, show a summary
    python scripts/draft_followups.py --mock          # no paid API, used by start.bat
    python scripts/draft_followups.py --show          # print every message in full
    python scripts/draft_followups.py --show 4        # print the level 4 letters
    python scripts/draft_followups.py --one FA-2026-1213
    python scripts/draft_followups.py --reset         # clear drafts and start over
    python scripts/draft_followups.py --demo-guardrail  # show the number check blocking a draft
    python scripts/draft_followups.py --prompt FA-2026-1202   # what the API mode would send

Nothing is sent. Every message lands in the validation queue as a preview.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module                                     # noqa: E402
from app import followups as followup_module                        # noqa: E402
from app import overdue as engine                                   # noqa: E402
from app.config import DEMO_MODE, LLM_MODE, today                   # noqa: E402
from app.labels_fr import CHANNEL, LEVEL_NAME                       # noqa: E402
from app.money import format_eur                                    # noqa: E402

WIDTH = 96
BAR = "─" * WIDTH


def print_message(result) -> None:
    item, draft = result.invoice, result.draft
    print()
    print("┌" + "─" * (WIDTH - 2) + "┐")
    print(f"  {item.number}  ·  niveau {item.level} — {LEVEL_NAME[item.level]}"
          f"  ·  {CHANNEL[item.channel]}")
    print(f"  {item.customer_name}  ·  {item.email}")
    print(f"  {format_eur(item.amount_due_cents)} dus  ·  {item.days_overdue} jours de retard")
    print("└" + "─" * (WIDTH - 2) + "┘")
    print()
    print(f"  Objet : {draft.subject}")
    print()
    for line in draft.body.splitlines():
        print(f"  {line}" if line else "")
    print()
    status = "✓ contrôle des chiffres OK" if draft.accepted else "✗ BLOQUÉ"
    print(f"  [{status} — {draft.guardrail.summary()}]")
    if not draft.accepted:
        print(f"  [{draft.guardrail.note}]")
    print(BAR)



def demo_guardrail(conn, reference) -> int:
    """Show the number check doing its job, on a real invoice.

    Runs the same invoice through the normal engine and through a backend that
    invents figures, and prints both verdicts side by side.
    """
    from app.drafting import build_context, generate

    candidates = [i for i in engine.load_all(conn, reference) if i.level >= 2]
    candidates.sort(key=lambda i: -i.amount_due_cents)
    item = candidates[0]
    context = build_context(item, item.customer_address_line, reference)

    print(BAR)
    print("  CONTRÔLE DES CHIFFRES — démonstration")
    print(BAR)
    print(f"  Facture {item.number} — {item.customer_name}")
    print(f"  Données transmises au moteur de rédaction : "
          f"{len(context.sources())} chaînes déjà formatées, aucun nombre brut.")
    print(f"  Montant dû tel que calculé en Python : {format_eur(item.amount_due_cents)}")
    print()

    normal = generate(context)
    print(f"  1. Moteur normal ({normal.generator})")
    print(f"     {'✓ accepté' if normal.accepted else '✗ bloqué'} — {normal.guardrail.summary()}")
    print()

    def inventing(_context):
        return (
            f"Facture {context.invoice_number} — régularisation",
            "Après recalcul, le solde exigible s’élève à 9 876,54 €, "
            "à régler avant le 31/12/2099 sur le compte FR76 1111 2222 3333. "
            "Des pénalités de 22 % l’an courent depuis l’échéance.",
        )

    caught = generate(context, backend=inventing)
    print("  2. Moteur qui invente des chiffres (simulation)")
    print(f"     Message produit : « {caught.body[:88]}... »")
    print(f"     {'✓ accepté' if caught.accepted else '✗ BLOQUÉ'} — {caught.guardrail.summary()}")
    print(f"     Chiffres refusés : {', '.join(caught.guardrail.unknown)}")
    print()
    print("     Ce message n’entre pas dans la file de validation. Il est conservé,")
    print("     signalé au garagiste, et ne compte pas comme une relance préparée.")
    print(BAR)
    return 0


def main(argv: list[str]) -> int:
    conn = db_module.connect()
    reference = today()

    if "--demo-guardrail" in argv:
        return demo_guardrail(conn, reference)

    if "--prompt" in argv:
        from app.anthropic_backend import SYSTEM_PROMPT, build_prompt
        from app.drafting import build_context
        number = argv[argv.index("--prompt") + 1]
        matches = [i for i in engine.load_all(conn, reference) if i.number == number]
        if not matches:
            print(f"Facture {number} introuvable ou déjà réglée.")
            return 1
        context = build_context(matches[0], matches[0].customer_address_line, reference)
        print(BAR)
        print("  CE QUE REÇOIT LE MODÈLE EN MODE API — aucun nombre brut")
        print(BAR)
        print()
        print("  [instructions système]")
        for line in SYSTEM_PROMPT.splitlines():
            print(f"  {line}")
        print()
        print("  [données]")
        for line in build_prompt(context).splitlines():
            print(f"  {line}")
        print()
        return 0

    if "--reset" in argv:
        removed = followup_module.clear_drafts(conn)
        print(f"{removed} brouillon(s) supprimé(s).")

    if "--one" in argv:
        number = argv[argv.index("--one") + 1]
        matches = [i for i in engine.load_all(conn, reference) if i.number == number]
        if not matches:
            print(f"Facture {number} introuvable ou déjà réglée.")
            return 1
        item = matches[0]
        draft = followup_module.draft_for(item, reference)
        print_message(followup_module.DraftResult(item, draft, 0))
        return 0

    mode = "mock" if "--mock" in argv or DEMO_MODE else LLM_MODE
    run = followup_module.create_drafts(conn, reference, mode=mode)

    print(BAR)
    print(f"  RÉDACTION DES RELANCES — {run.reference_date}")
    print(BAR)
    print(f"  Moteur de rédaction : {mode}"
          f"{'  (aucun appel API, aucun crédit consommé)' if mode == 'mock' else ''}")
    print(f"  Mode démonstration  : {'oui — aucun envoi réel' if DEMO_MODE else 'NON'}")
    print()
    print(f"  Messages rédigés            : {run.total}")
    print(f"  Placés en file de validation: {len(run.queued)}")
    print(f"  Bloqués par le contrôle     : {len(run.rejected)}")
    print()

    by_level: dict[int, list] = {}
    for result in run.queued:
        by_level.setdefault(result.invoice.level, []).append(result)

    print(f"  {'Niveau':<26}{'Messages':>10}{'Montant concerné':>22}{'Canal':>26}")
    print("  " + "-" * (WIDTH - 2))
    for level in sorted(by_level, reverse=True):
        group = by_level[level]
        amount = sum(r.invoice.amount_due_cents for r in group)
        print(f"  {LEVEL_NAME[level]:<26}{len(group):>10}{format_eur(amount):>23}"
              f"{CHANNEL[group[0].invoice.channel]:>26}")
    print("  " + "-" * (WIDTH - 2))

    if run.rejected:
        print()
        print("  ⚠  MESSAGES BLOQUÉS — ils ne sont pas entrés dans la file")
        for result in run.rejected:
            print(f"     {result.invoice.number}  {result.draft.guardrail.note}")

    if "--show" in argv:
        position = argv.index("--show")
        wanted = None
        if len(argv) > position + 1 and argv[position + 1].isdigit():
            wanted = int(argv[position + 1])
        print()
        print(BAR)
        print("  APERÇU DES MESSAGES — rien n’est envoyé")
        print(BAR)
        for result in list(run.queued) + list(run.rejected):
            if wanted is None or result.invoice.level == wanted:
                print_message(result)

    print()
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main(sys.argv[1:]))
