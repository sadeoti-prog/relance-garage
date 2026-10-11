"""Exercise the real Anthropic backend once, on a single invoice.

Deliberately narrow: ONE invoice, ONE API call, nothing written to the database.
Enough to prove the path works end to end without spending more than a few
cents. Everything else in the demo keeps running on the free mock engine.

    python scripts/test_api_mode.py              # the heaviest level 4 letter
    python scripts/test_api_mode.py FA-2026-1245 # a specific invoice

Requires ANTHROPIC_API_KEY, in the environment or in a .env file at the root.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module                                     # noqa: E402
from app import overdue as engine                                   # noqa: E402
from app.config import ANTHROPIC_EFFORT, ANTHROPIC_MODEL, today     # noqa: E402
from app.drafting import build_context, generate                    # noqa: E402
from app.money import format_eur                                    # noqa: E402

BAR = "─" * 96


def main(argv: list[str]) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY absente.")
        print("Créez un fichier .env à la racine du projet contenant :")
        print()
        print("    ANTHROPIC_API_KEY=sk-ant-...")
        print()
        print("Ce fichier est ignoré par git et n’est jamais lu ailleurs.")
        return 1

    conn = db_module.connect()
    reference = today()

    candidates = [item for item in engine.load_all(conn, reference) if item.level >= 1]
    if argv:
        candidates = [item for item in candidates if item.number == argv[0]]
        if not candidates:
            print(f"Facture {argv[0]} introuvable ou non relançable.")
            return 1
    else:
        candidates.sort(key=lambda item: (-item.level, -item.amount_due_cents))

    item = candidates[0]
    context = build_context(item, item.customer_address_line, reference)

    print(BAR)
    print("  TEST DU MODE API — un seul appel, rien n’est enregistré")
    print(BAR)
    print(f"  Modèle   : {ANTHROPIC_MODEL} (effort {ANTHROPIC_EFFORT})")
    print(f"  Facture  : {item.number} — {item.customer_name}")
    print(f"  Niveau   : {item.level}  ·  {format_eur(item.amount_due_cents)} dus "
          f"·  {item.days_overdue} jours de retard")
    print(f"  Contexte : {len(context.sources())} chaînes formatées, aucun nombre brut")
    print(BAR)
    print()

    try:
        draft = generate(context, mode="anthropic")
    except Exception as error:                    # noqa: BLE001 - report, don't mask
        print(f"ÉCHEC : {type(error).__name__}")
        print(f"        {error}")
        return 1

    print(f"  Objet : {draft.subject}")
    print()
    for line in draft.body.splitlines():
        print(f"  {line}" if line else "")
    print()
    print(BAR)
    if draft.accepted:
        print(f"  ✓ CONTRÔLE DES CHIFFRES OK — {draft.guardrail.summary()}")
    else:
        print(f"  ✗ BLOQUÉ — {draft.guardrail.summary()}")
        print(f"  Chiffres refusés : {', '.join(draft.guardrail.unknown)}")
        print()
        print("  Le garde-fou a fait son travail : ce message n’aurait pas été")
        print("  mis en file de validation.")
    print(BAR)
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main(sys.argv[1:]))
