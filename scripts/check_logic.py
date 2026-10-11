"""Proof that every figure in the product is computed in Python.

Runnable in front of a customer: it checks the escalation thresholds, the money
arithmetic to the cent, and the fact that the ledger in the database agrees with
itself. No language model is involved anywhere in this file.

    python scripts/check_logic.py
"""

from __future__ import annotations

import base64
import contextlib
import email
import email.policy
import json
import smtplib
import socket
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module                                     # noqa: E402
from app import followups as followup_module                        # noqa: E402
from app import kpi                                                 # noqa: E402
from app import guardrail                                           # noqa: E402
from app import overdue as engine                                   # noqa: E402
from app import seed                                                # noqa: E402
from app.drafting import build_context, generate                    # noqa: E402
from app.importer import mapping                                    # noqa: E402
from app.importer import reconcile                                  # noqa: E402
from app.importer import runner                                     # noqa: E402
from app.importer import reader as file_reader                      # noqa: E402
from app.importer.convention import detect_amount_convention        # noqa: E402
from app.importer.values import (                                   # noqa: E402
    ValueProblem, parse_amount, parse_date,
)
from app.config import (                                            # noqa: E402
    DEFAULT_PENALTY_TERMS, LEVEL_THRESHOLD_DAYS, PENALTY_FROM_LEVEL,
    RECOVERY_FEE_CENTS, VAT_RATE_BP,
)
from app.money import (                                             # noqa: E402
    apply_rate_bp, format_eur, line_total, prorata_annual, vat_of,
)

PASSED = 0
FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    global PASSED
    if actual == expected:
        PASSED += 1
    else:
        FAILURES.append(f"{label}\n      attendu : {expected!r}\n      obtenu  : {actual!r}")


def check_true(label: str, condition: bool) -> None:
    check(label, bool(condition), True)


def section(title: str) -> None:
    print(f"\n  {title}")
    print("  " + "-" * 74)


# ---------------------------------------------------------------------------
# 1. Escalation thresholds
# ---------------------------------------------------------------------------

def test_levels() -> None:
    section("Seuils d’escalade")

    for days, expected in (
        (-5, 0), (0, 0), (1, 0), (2, 0),      # not late enough yet
        (3, 1), (5, 1), (9, 1),               # rappel courtois
        (10, 2), (15, 2), (20, 2),            # relance ferme
        (21, 3), (30, 3), (34, 3),            # dernier avis
        (35, 4), (60, 4), (400, 4),           # mise en demeure
    ):
        check(f"J+{days} tombe au niveau {expected}", engine.level_for_days(days), expected)

    # Each threshold is exact: one day earlier is still the level below.
    for level, threshold in LEVEL_THRESHOLD_DAYS.items():
        check(f"seuil du niveau {level} : J+{threshold} déclenche",
              engine.level_for_days(threshold), level)
        check(f"seuil du niveau {level} : J+{threshold - 1} ne déclenche pas",
              engine.level_for_days(threshold - 1) < level, True)

    check("le niveau 1 est suivi du niveau 2", engine.next_level_after(1), 2)
    check("le niveau 3 est suivi du niveau 4", engine.next_level_after(3), 4)
    check("le niveau 4 termine l’échelle", engine.next_level_after(4), None)
    check("une facture à l’heure vise le niveau 1", engine.next_level_after(0), 1)

    check("le niveau 2 est atteint 10 jours après l’échéance",
          engine.date_of_level("2026-08-01", 2), date(2026, 8, 11))
    check("le niveau 4 est atteint 35 jours après l’échéance",
          engine.date_of_level("2026-08-01", 4), date(2026, 9, 5))

    check("retard calculé en jours", engine.days_overdue("2026-08-01", date(2026, 8, 27)), 26)
    check("le jour de l’échéance, le retard est nul",
          engine.days_overdue("2026-08-27", date(2026, 8, 27)), 0)
    check("avant l’échéance, le retard est négatif",
          engine.days_overdue("2026-09-01", date(2026, 8, 27)), -5)


# ---------------------------------------------------------------------------
# 2. Money arithmetic, to the cent
# ---------------------------------------------------------------------------

def test_money() -> None:
    section("Arithmétique monétaire (centimes entiers)")

    check("5,9 h à 62,00 € = 365,80 €", line_total(6_200, 5_900), 36_580)
    check("4 L à 9,90 € = 39,60 €", line_total(990, 4_000), 3_960)
    check("1 u à 437,00 € = 437,00 €", line_total(43_700, 1_000), 43_700)

    check("TVA 20 % sur 1 794,20 € = 358,84 €", vat_of(179_420, VAT_RATE_BP), 35_884)
    check("TVA 20 % sur 100,00 € = 20,00 €", vat_of(10_000, VAT_RATE_BP), 2_000)
    check("arrondi au centime supérieur à la moitié", apply_rate_bp(1, 5_000), 1)
    check("arrondi au centime inférieur sous la moitié", apply_rate_bp(1, 2_000), 0)

    check("15 % l’an sur 1 000,00 € pendant 365 jours = 150,00 €",
          prorata_annual(100_000, 1_500, 365), 15_000)
    check("953,88 € à 15 % pendant 68 jours = 26,66 €",
          prorata_annual(95_388, 1_500, 68), 2_666)
    check("aucun intérêt sur zéro jour", prorata_annual(100_000, 1_500, 0), 0)
    check("aucun intérêt avant l’échéance", prorata_annual(100_000, 1_500, -10), 0)

    check("format français avec séparateur de milliers",
          format_eur(215_304), "2 153,04 €")
    check("format français sous mille", format_eur(9_990), "99,90 €")
    check("format français négatif", format_eur(-50_000), "-500,00 €")

    check("solde dû après acompte", engine.amount_due_cents(100_818, 50_000), 50_818)
    check("solde dû jamais négatif", engine.amount_due_cents(10_000, 15_000), 0)
    check("solde dû sur facture intacte", engine.amount_due_cents(21_530, 0), 21_530)


# ---------------------------------------------------------------------------
# 3. Penalties and recovery indemnity
# ---------------------------------------------------------------------------

def test_penalties() -> None:
    section("Pénalités de retard et indemnité forfaitaire")

    signed = dict(rate_bp=1_500, terms_confirmed=True)

    check("pas de pénalité au niveau 1",
          engine.late_penalty_cents(100_000, 5, 1, **signed), 0)
    check("pas de pénalité sous surveillance",
          engine.late_penalty_cents(100_000, 2, 0, **signed), 0)
    check_true("pénalité à partir du niveau 2",
               engine.late_penalty_cents(100_000, 10, 2, **signed) > 0)
    check("pénalité calculée sur le solde dû, pas sur le total facturé",
          engine.late_penalty_cents(50_818, 24, PENALTY_FROM_LEVEL, **signed),
          prorata_annual(50_818, 1_500, 24))

    # The terms belong to the customer, not to the software.
    check("aucune pénalité si les conditions ne sont pas confirmées",
          engine.late_penalty_cents(100_000, 60, 4, rate_bp=1_500, terms_confirmed=False), 0)
    check("aucune pénalité sans taux au dossier",
          engine.late_penalty_cents(100_000, 60, 4, rate_bp=None, terms_confirmed=True), 0)
    check("aucun repli sur un taux par défaut",
          engine.late_penalty_cents(100_000, 60, 4, rate_bp=None, terms_confirmed=False), 0)
    check("le taux appliqué est celui du client, pas un taux global",
          engine.late_penalty_cents(100_000, 60, 4, rate_bp=1_000, terms_confirmed=True),
          prorata_annual(100_000, 1_000, 60))
    check_true("un particulier et un professionnel n’ont pas le même taux par défaut",
               DEFAULT_PENALTY_TERMS["particulier"]["rate_bp"]
               != DEFAULT_PENALTY_TERMS["professionnel"]["rate_bp"])

    check("aucune indemnité pour un particulier",
          engine.recovery_fee_cents("particulier", 4), 0)
    check("indemnité de 40 € pour un professionnel",
          engine.recovery_fee_cents("professionnel", 2), RECOVERY_FEE_CENTS)
    check("aucune indemnité au niveau 1 même pour un professionnel",
          engine.recovery_fee_cents("professionnel", 1), 0)


# ---------------------------------------------------------------------------
# 4. The workflow rule: one draft per level, a skip is final
# ---------------------------------------------------------------------------

def _row(**overrides) -> dict:
    base = {
        "id": 1, "number": "FA-2026-0001", "job_label": "Vidange moteur",
        "issue_date": "2026-07-01", "due_date": "2026-07-16",
        "payment_terms_days": 15, "status": "unpaid",
        "total_ttc_cents": 20_000, "amount_paid_cents": 0,
        "customer_id": 1, "customer_name": "Nathalie Perrin",
        "customer_kind": "particulier", "civility": "Mme",
        "contact_name": "Nathalie Perrin", "email": "n.perrin@example.com",
        "phone": "06 39 98 14 27", "customer_since": 2017, "is_regular": 1,
        "late_penalty_rate_bp": 1_000, "cgv_reference": "Conditions 2024",
        "penalty_terms_confirmed": 1, "kind_confirmed": 1,
        "plate": "DJ-482-QR", "make": "Renault", "model": "Clio IV",
        "address": "8 rue Émile Zola", "postal_code": "69120",
        "city": "Vaulx-en-Velin",
    }
    base.update(overrides)
    return base


def test_workflow() -> None:
    section("Règle de relance : un brouillon par niveau")

    reference = date(2026, 8, 27)   # 42 days past the due date -> level 4

    fresh = engine.evaluate(_row(), reference, highest_followup_level=0)
    check("une facture à J+42 vise le niveau 4", fresh.level, 4)
    check_true("aucun brouillon encore rédigé : relance à préparer", fresh.followup_is_due)

    handled = engine.evaluate(_row(), reference, highest_followup_level=4)
    check_true("niveau déjà traité : rien à préparer", not handled.followup_is_due)

    # Skipping counts as handled — the owner said no, the system does not insist.
    skipped = engine.evaluate(_row(), reference, highest_followup_level=4)
    check_true("un niveau ignoré ne revient pas", not skipped.followup_is_due)

    climbed = engine.evaluate(_row(), reference, highest_followup_level=3)
    check_true("la facture a franchi un palier : nouveau brouillon", climbed.followup_is_due)

    paid = engine.evaluate(_row(status="paid"), reference, highest_followup_level=0)
    check_true("une facture réglée ne se relance pas", not paid.followup_is_due)

    early = engine.evaluate(_row(due_date="2026-08-26"), reference)
    check("à J+1 la facture reste sous surveillance", early.level, 0)
    check_true("sous surveillance : aucune relance", not early.followup_is_due)

    pro = engine.evaluate(
        _row(customer_kind="professionnel", total_ttc_cents=100_818,
             amount_paid_cents=50_000, status="partial"),
        reference,
    )
    check("solde dû après acompte", pro.amount_due_cents, 50_818)
    check("total réclamé = solde + pénalités + indemnité",
          pro.total_claimed_cents,
          pro.amount_due_cents + pro.penalty_cents + pro.recovery_fee_cents)
    check("l’indemnité forfaitaire s’applique au professionnel",
          pro.recovery_fee_cents, RECOVERY_FEE_CENTS)
    check_true("les conditions du professionnel sont confirmées",
               not pro.penalty_blocked)

    unconfirmed = engine.evaluate(
        _row(late_penalty_rate_bp=None, penalty_terms_confirmed=0,
             cgv_reference="Aucune condition acceptée"),
        reference,
    )
    check("conditions non confirmées : aucune pénalité réclamée",
          unconfirmed.penalty_cents, 0)
    check_true("conditions non confirmées : la facture est signalée",
               unconfirmed.penalty_blocked)
    check("conditions non confirmées : on ne réclame que le principal",
          unconfirmed.total_claimed_cents, unconfirmed.amount_due_cents)


# ---------------------------------------------------------------------------
# 5. The ledger agrees with itself
# ---------------------------------------------------------------------------

def test_ledger() -> None:
    section("Cohérence du grand livre")

    # Owners can now add invoices to their demo. Seed-size assertions belong
    # to a deterministic fixture, never to their live, editable ledger.
    conn = db_module.connect(":memory:")
    seed.generate(conn)
    problems = seed.check(conn)
    check("contrôles du générateur", problems, [])

    money_columns = (
        "total_ht_cents", "vat_cents", "total_ttc_cents", "amount_paid_cents",
    )
    non_integer = []
    for row in conn.execute("SELECT * FROM invoices"):
        for column in money_columns:
            if not isinstance(row[column], int):
                non_integer.append(f"{row['number']}.{column}")
    check("tous les montants sont des entiers (aucun flottant)", non_integer, [])

    mismatched = []
    for item in engine.load_all(conn, include_paid=True):
        if item.total_claimed_cents != (
            item.amount_due_cents + item.penalty_cents + item.recovery_fee_cents
        ):
            mismatched.append(item.number)
        if item.penalty_cents and item.level < PENALTY_FROM_LEVEL:
            mismatched.append(f"{item.number} (pénalité sous le seuil)")
        if item.recovery_fee_cents and item.customer_kind != "professionnel":
            mismatched.append(f"{item.number} (indemnité à un particulier)")
        if item.penalty_cents and not item.penalty_terms_confirmed:
            mismatched.append(f"{item.number} (pénalité sans conditions confirmées)")
        if item.penalty_cents and item.penalty_rate_bp:
            expected = prorata_annual(item.amount_due_cents, item.penalty_rate_bp,
                                      item.days_overdue)
            if item.penalty_cents != expected:
                mismatched.append(f"{item.number} (taux appliqué ≠ taux du client)")
    check("chaque facture évaluée est cohérente", mismatched, [])

    book = engine.portfolio(conn)
    bucket_total = sum(bucket.amount_due_cents for bucket in book.buckets)
    check("la somme des niveaux égale l’encours échu", bucket_total, book.overdue_cents)
    check("encours total = échu + non échu",
          book.overdue_cents + book.not_due_cents, book.outstanding_cents)

    queue = engine.followups_due(conn)
    check("la file de relance ne contient que des factures échues",
          [item.number for item in queue if item.level < 1], [])
    check("aucune facture réglée dans la file",
          [item.number for item in queue if item.status == "paid"], [])
    check("le compteur du tableau de bord suit la file",
          book.followups_due_count, len(queue))

    conn.close()


# ---------------------------------------------------------------------------
# 6. Determinism: the same demo twice
# ---------------------------------------------------------------------------

def test_determinism() -> None:
    section("Reproductibilité du jeu de démonstration")

    reference = date(2026, 8, 27)
    fingerprints = []
    with tempfile.TemporaryDirectory() as folder:
        for run in range(2):
            path = Path(folder) / f"run{run}.db"
            conn = db_module.connect(path)
            seed.generate(conn, reference)
            fingerprints.append(
                conn.execute(
                    "SELECT number, total_ttc_cents FROM invoices ORDER BY number"
                ).fetchall()
            )
            conn.close()

    check("deux générations produisent les mêmes factures",
          [tuple(r) for r in fingerprints[0]], [tuple(r) for r in fingerprints[1]])


# ---------------------------------------------------------------------------
# 7. The number guardrail
# ---------------------------------------------------------------------------

def _context_for(conn, reference: date):
    """A real drafting context, taken from the heaviest overdue invoice."""
    items = [i for i in engine.load_all(conn, reference) if i.level >= 1]
    items.sort(key=lambda i: -i.amount_due_cents)
    item = items[0]
    return item, build_context(item, item.customer_address_line, reference)


def test_guardrail() -> None:
    section("Contrôle des chiffres : rien qui n’ait été fourni")

    # --- normalisation: the same amount written three ways is one number
    nbsp, narrow = chr(0x00A0), chr(0x202F)
    check("espace insécable normalisée",
          guardrail.extract_numbers(f"2{nbsp}153,04 €"), ["2153.04"])
    check("espace fine normalisée",
          guardrail.extract_numbers(f"2{narrow}153,04 €"), ["2153.04"])
    check("espace ordinaire normalisée",
          guardrail.extract_numbers("2 153,04 €"), ["2153.04"])
    check("point décimal accepté comme la virgule",
          guardrail.extract_numbers("2153.04"), ["2153.04"])

    sources = ["Facture FA-2026-1213 du 16/06/2026", "2 153,04 €", "50,43 €"]

    check_true("un message qui reprend les montants fournis passe",
               guardrail.verify("Vous devez 2 153,04 € (dont 50,43 €).", sources).ok)
    check_true("l’écriture sans séparateur passe aussi",
               guardrail.verify("Vous devez 2153,04 €.", sources).ok)

    # --- the cases that matter
    one_cent = guardrail.verify("Vous devez 2 153,05 €.", sources)
    check_true("un centime d’écart est rejeté", not one_cent.ok)
    # Reported as written, not normalised: this string is read by the garage owner.
    check("le montant fautif est nommé tel qu’écrit", one_cent.unknown, ("2 153,05",))

    invented_date = guardrail.verify("À régler avant le 31/12/2027.", sources)
    check_true("une date inventée est rejetée", not invented_date.ok)

    invented_iban = guardrail.verify("Virement au FR76 9999 8888 7777.", sources)
    check_true("un IBAN inventé est rejeté", not invented_iban.ok)

    invented_rate = guardrail.verify("Des pénalités de 12 % s’appliquent.", sources)
    check_true("un taux inventé est rejeté", not invented_rate.ok)
    check("le taux fautif est nommé", invented_rate.unknown, ("12",))

    check_true("un message sans aucun chiffre passe",
               guardrail.verify("Merci de régulariser votre situation.", sources).ok)

    # --- every real draft of the demo passes
    conn = db_module.connect()
    reference = today_reference()
    drafted, failed = 0, []
    for item in engine.load_all(conn, reference):
        if item.level < 1:
            continue
        draft = followup_module.draft_for(item, reference)
        drafted += 1
        if not draft.accepted:
            failed.append(f"{item.number}: {draft.guardrail.unknown}")
    check("tous les messages du jeu de démonstration passent le contrôle", failed, [])
    check_true("des messages ont bien été rédigés", drafted > 0)

    # --- a backend that invents figures is caught, whatever the level
    item, context = _context_for(conn, reference)

    def hallucinating(_context):
        return (
            "Facture impayée",
            "Vous devez 9 876,54 € à régler avant le 31/12/2099, "
            "virement au FR76 1111 2222 3333, pénalités de 22 % l’an.",
        )

    caught = generate(context, backend=hallucinating)
    check_true("un backend qui invente des chiffres est bloqué", not caught.accepted)
    check_true("le montant inventé est signalé", "9 876,54" in caught.guardrail.unknown)
    check_true("le taux inventé est signalé", "22" in caught.guardrail.unknown)
    check_true("l’IBAN inventé est signalé",
               any("1111" in item for item in caught.guardrail.unknown))
    check_true("un motif lisible accompagne le blocage", bool(caught.guardrail.note))
    conn.close()


def test_rejected_never_queued() -> None:
    section("Un message bloqué n’entre jamais dans la file")

    reference = date(2026, 8, 28)

    def hallucinating(_context):
        return "Rappel", "Le solde de 12 345,67 € est exigible."

    with tempfile.TemporaryDirectory() as folder:
        conn = db_module.connect(Path(folder) / "queue.db")
        seed.generate(conn, reference)

        run = followup_module.create_drafts(conn, reference, limit=5,
                                            backend=hallucinating)
        check("aucun message inventé n’est mis en file", len(run.queued), 0)
        check("tous ont été bloqués", len(run.rejected), 5)

        pending = followup_module.queue(conn, "pending")
        check("la file de validation est vide", len(pending), 0)

        rejected = followup_module.queue(conn, "rejected")
        check("les messages bloqués sont conservés et signalés", len(rejected), 5)
        check_true("chaque blocage porte son motif",
                   all(row["guardrail_note"] for row in rejected))
        check_true("aucun blocage n’est marqué comme valide",
                   all(row["guardrail_ok"] == 0 for row in rejected))

        # And the honest counterpart: the real backend fills the queue.
        followup_module.clear_drafts(conn)
        clean = followup_module.create_drafts(conn, reference, limit=5)
        check("le moteur mock remplit la file", len(clean.queued), 5)
        check("le moteur mock ne déclenche aucun blocage", len(clean.rejected), 0)
        conn.close()


def today_reference() -> date:
    from app.config import today
    return today()


# ---------------------------------------------------------------------------
# 8. Reading a real garage's export file
# ---------------------------------------------------------------------------

NBSP = chr(0x00A0)
NARROW = chr(0x202F)
SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _amount(text):
    return parse_amount(text).cents


def PASSED_COUNT(label: str) -> None:
    """Record a check that passed without a value comparison."""
    global PASSED
    PASSED += 1


def _expect_refused(parser, value, label: str) -> None:
    """A value that must not be accepted at all."""
    global PASSED
    try:
        parser(value)
    except ValueProblem:
        PASSED += 1
        return
    FAILURES.append(f"{label} : « {value} » aurait dû être refusé")


def test_import_amounts() -> None:
    section("Import — montants : des chiffres, jamais un flottant")

    # Every notation a French garage export can produce for the same amount.
    for text in ("1 234,56", f"1{NBSP}234,56", f"1{NARROW}234,56", "1234.56",
                 "1.234,56", "1,234.56", f"1{NBSP}234,56 €", "1 234,56 EUR"):
        check(f"« {text} » vaut 1 234,56 €", _amount(text), 123_456)

    check("un entier nu est un montant en euros", _amount("1234"), 123_400)
    check("une seule décimale est complétée", _amount("89,9"), 8_990)
    check("les centimes seuls", _amount("0,05"), 5)

    # The reason this module exists. A float round-trip corrupts 272 of the
    # first 4 000 cent values; the parser has to be exact on all of them.
    wrong_via_float = []
    for cents in range(1, 4_000):
        written = f"{cents // 100}.{cents % 100:02d}"
        if _amount(written) != cents:
            FAILURES.append(f"« {written} » mal converti")
            break
        if int(float(written) * 100) != cents:
            wrong_via_float.append(written)
    else:
        PASSED_COUNT("4 000 montants convertis au centime exact")
    check_true("un flottant se tromperait sur des centaines d’entre eux",
               len(wrong_via_float) > 100)
    check("« 1.15 » vaut 115 centimes, pas 114", _amount("1.15"), 115)
    check("« 0.29 » vaut 29 centimes, pas 28", _amount("0.29"), 29)
    check_true("… là où le flottant donne bien un résultat faux",
               int(float("1.15") * 100) == 114)
    check_true("tous les montants sont des entiers",
               all(isinstance(_amount(t), int) for t in ("1 234,56", "8.07", "0,05")))

    # Credit notes and accountants' notations.
    check("montant négatif", _amount("-1 234,56"), -123_456)
    check("montant entre parenthèses", _amount("(1 234,56)"), -123_456)
    check("signe en fin de nombre", _amount("1 234,56-"), -123_456)

    # Ambiguity is never resolved on a lone value: see test_import_convention.
    _expect_refused(parse_amount, "1.234", "montant ambigu isolé")
    _expect_refused(parse_amount, "12,345", "montant ambigu isolé")
    rounded = parse_amount("12,3456")
    check("quatre décimales sont arrondies au centime", rounded.cents, 1_235)
    check_true("... et l’arrondi est signalé", bool(rounded.warning))
    check_true("un montant net n’est signalé pour rien",
               parse_amount("1 234,56").warning is None)

    for bad in ("", "   ", "pas un montant", "12,34,56 €", "1.23.456",
                "1.234,56.78", None):
        _expect_refused(parse_amount, bad, "montant")
    check("un groupement correct reste accepté", _amount("1.234.567"), 123_456_700)



def test_import_convention() -> None:
    section("Import — la convention se décide par colonne, jamais par valeur")

    # A value alone proves nothing; the column does.
    for text in ("1,234", "1.234", "12,345"):
        _expect_refused(parse_amount, text, "montant ambigu sans colonne")

    # 1. The column settles it as decimal.
    french = ["189,90", "1,234", "98,00", "2 340,00"]
    convention = detect_amount_convention(french)
    check_true("colonne française : convention établie", convention.settled)
    check("colonne française : la virgule est décimale", convention.decimal, ",")
    check("« 1,234 » y vaut donc 1,23 €",
          parse_amount("1,234", convention).cents, 123)
    check("les autres montants ne bougent pas",
          parse_amount("189,90", convention).cents, 18_990)

    # 2. The column settles it as thousands.
    anglo = ["189.90", "1,234", "98.00", "12,345"]
    convention = detect_amount_convention(anglo)
    check_true("colonne anglo-saxonne : convention établie", convention.settled)
    check("colonne anglo-saxonne : le point est décimal", convention.decimal, ".")
    check("« 1,234 » y vaut donc 1 234,00 €",
          parse_amount("1,234", convention).cents, 123_400)
    check("« 12,345 » y vaut 12 345,00 €",
          parse_amount("12,345", convention).cents, 1_234_500)

    # 3. The column settles nothing: refuse, do not guess.
    undecided = ["1,234", "5,678", "2,500"]
    convention = detect_amount_convention(undecided)
    check_true("colonne indécidable : aucune convention", not convention.settled)
    check_true("… et elle bloque l’import", convention.blocks_import)
    for value in undecided:
        _expect_refused(parse_amount, value, "colonne indécidable")
    check_true("le motif du refus est explicite", "ambigu" in _refusal(parse_amount, "1,234"))

    # A column with no ambiguous value at all never blocks anything.
    clean = detect_amount_convention(["189,90", "98,00", "2 340,00"])
    check_true("une colonne sans ambiguïté n’est jamais bloquante",
               not clean.blocks_import)
    check_true("… même chose pour une colonne d’entiers nus",
               not detect_amount_convention(["100", "250", "1000"]).blocks_import)

    # Contradictory columns settle nothing either.
    mixed = detect_amount_convention(["1,50", "2.75", "1,234"])
    check_true("une colonne qui mélange les conventions ne tranche pas",
               not mixed.settled)
    _expect_refused(parse_amount, "1,234", "colonne contradictoire")

    # And the same rule read straight from files.
    if not SAMPLES.exists():
        FAILURES.append("samples/ manquant — lancez scripts/make_samples.py")
        return

    def column(filename, header="Total"):
        source = file_reader.read_path(SAMPLES / filename)
        return source, [source.cell(row, header) for row in source.rows]

    source, values = column("convention-decimale.csv")
    convention = detect_amount_convention(values)
    check_true("fichier décimal : convention établie", convention.settled)
    check("fichier décimal : « 1,234 » vaut 1,23 €",
          parse_amount(values[1], convention).cents, 123)

    source, values = column("convention-milliers.csv")
    convention = detect_amount_convention(values)
    check_true("fichier milliers : convention établie", convention.settled)
    check("fichier milliers : « 1,234 » vaut 1 234,00 €",
          parse_amount(values[1], convention).cents, 123_400)

    source, values = column("convention-indecidable.csv")
    convention = detect_amount_convention(values)
    check_true("fichier indécidable : aucune convention", not convention.settled)
    check_true("fichier indécidable : import bloqué", convention.blocks_import)
    refused = 0
    for value in values:
        try:
            parse_amount(value, convention)
        except ValueProblem:
            refused += 1
    check("fichier indécidable : les trois lignes sont écartées", refused, 3)


def _refusal(parser, value) -> str:
    try:
        parser(value)
    except ValueProblem as error:
        return error.reason
    return ""


def test_import_dates() -> None:
    section("Import — dates : quatre formats, aucune supposition muette")

    for text in ("31/12/2026", "2026-12-31", "31-12-2026", "31.12.2026",
                 "31/12/26", "20261231", "2026-12-31 00:00:00"):
        check(f"« {text} » vaut le 31/12/2026", parse_date(text).value,
              date(2026, 12, 31))

    check("un objet date passe tel quel",
          parse_date(date(2026, 12, 31)).value, date(2026, 12, 31))
    check("un datetime perd son heure",
          parse_date(datetime(2026, 12, 31, 14, 30)).value, date(2026, 12, 31))

    serial = parse_date(46022)
    check("le numéro de série Excel 46022 vaut le 31/12/2025",
          serial.value, date(2025, 12, 31))
    check_true("... et la conversion est signalée", bool(serial.warning))
    check("le même sous forme de texte",
          parse_date("46022").value, date(2025, 12, 31))

    # Day/month order: French by default, American only when nothing else fits,
    # and then it says so.
    check("31/12 est un jour, pas un mois",
          parse_date("31/12/2026").value, date(2026, 12, 31))
    check("01/02 se lit 1er février",
          parse_date("01/02/2026").value, date(2026, 2, 1))
    american = parse_date("12/31/2026")
    check("12/31 ne peut être qu’américain", american.value, date(2026, 12, 31))
    check_true("... et la lecture américaine est signalée", bool(american.warning))
    check_true("une date française n’est signalée pour rien",
               parse_date("31/12/2026").warning is None)

    for bad in ("", "pas une date", "32/13/2026", "2026-02-30", "99", None):
        _expect_refused(parse_date, bad, "date")


def test_import_files() -> None:
    section("Import — encodages, séparateurs et formats de fichier")

    check("un BOM UTF-8 est reconnu",
          file_reader.detect_encoding("N°;Client".encode("utf-8-sig"))[0], "utf-8-sig")
    check("un export Windows-1252 est reconnu",
          file_reader.detect_encoding("N° facture;Échéance".encode("cp1252"))[0], "cp1252")
    check("un fichier UTF-8 simple est reconnu",
          file_reader.detect_encoding("Numéro,Client".encode("utf-8"))[0], "utf-8")
    check_true("aucun octet ne fait planter la détection",
               file_reader.detect_encoding(bytes([0x81, 0x8D, 0xFF, 0x41]))[0] is not None)

    check("le point-virgule français est détecté",
          file_reader.detect_delimiter("a;b;c\n1;2;3\n4;5;6")[0], ";")
    check("la virgule est détectée",
          file_reader.detect_delimiter("a,b,c\n1,2,3\n4,5,6")[0], ",")
    check("la tabulation est détectée",
          file_reader.detect_delimiter("a\tb\tc\n1\t2\t3")[0], "\t")

    # The trap: a French file whose amounts contain commas, split on ';'.
    french = "N°;Client;Total\nF1;Durand;1 234,56\nF2;Martin;89,90"
    check("les virgules des montants ne trompent pas la détection",
          file_reader.detect_delimiter(french)[0], ";")

    if not SAMPLES.exists():
        FAILURES.append("samples/ manquant — lancez scripts/make_samples.py")
        return

    a = file_reader.read_path(SAMPLES / "export-logiciel-A.csv")
    check("export A : encodage", a.encoding, "cp1252")
    check("export A : séparateur", a.delimiter, ";")
    check("export A : 10 colonnes", len(a.headers), 10)
    check("export A : 10 lignes de données", a.row_count, 10)
    check("export A : les accents des en-têtes survivent", a.headers[0], "N° facture")
    check("export A : « Échéance » lisible", a.headers[4], "Échéance")
    check("export A : montant à espace insécable",
          _amount(a.cell(a.rows[6], "Total TTC")), 189_000)

    b = file_reader.read_path(SAMPLES / "export-logiciel-B.csv")
    check("export B : BOM UTF-8", b.encoding, "utf-8-sig")
    check("export B : séparateur virgule", b.delimiter, ",")
    check("export B : le BOM ne colle pas au premier en-tête",
          b.headers[0], "invoice_no")

    c = file_reader.read_path(SAMPLES / "export-logiciel-C.xlsx")
    check("export C : lu comme un classeur Excel", c.kind, "xlsx")
    check("export C : 5 lignes de données", c.row_count, 5)
    check("export C : un nombre Excel garde ses décimales exactes",
          _amount(c.cell(c.rows[0], "Montant TTC")), 128_460)
    check("export C : une date en série est convertie",
          parse_date(c.cell(c.rows[0], "Date")).value, date(2026, 6, 7))
    check_true("export C : les accents des cellules survivent",
               "Série" in (c.cell(c.rows[2], "Vehicule") or ""))


def test_import_touches_nothing() -> None:
    section("Import — lire un fichier ne touche à aucune base")

    from app.config import DB_PATH

    if not DB_PATH.exists():
        FAILURES.append("data/garage.db absente — lancez python -m app.seed")
        return
    if not SAMPLES.exists():
        FAILURES.append("samples/ manquant — lancez scripts/make_samples.py")
        return

    before = DB_PATH.read_bytes()
    for name in ("export-logiciel-A.csv", "export-logiciel-B.csv",
                 "export-logiciel-C.xlsx"):
        path = SAMPLES / name
        if path.exists():
            file_reader.read_path(path)
    after = DB_PATH.read_bytes()

    check("la base de démonstration est intacte après lecture", after, before)
    check_true("... octet pour octet", len(after) == len(before))



def test_import_mapping() -> None:
    section("Import — correspondance des colonnes et profils")

    check("« N° facture » et « num_facture » se normalisent pareil",
          mapping.normalise("N° facture"), "n facture")
    check("les accents disparaissent à la comparaison",
          mapping.normalise("Échéance"), "echeance")
    check("la ponctuation aussi", mapping.normalise("Total_TTC"), "total ttc")

    if not SAMPLES.exists():
        FAILURES.append("samples/ manquant — lancez scripts/make_samples.py")
        return

    # The three exports are named completely differently and must all map.
    for name, expected in (
        ("export-logiciel-A.csv",
         {"number": "N° facture", "customer_name": "Client",
          "issue_date": "Date facture", "total_ttc": "Total TTC"}),
        ("export-logiciel-B.csv",
         {"number": "invoice_no", "customer_name": "customer",
          "issue_date": "issue_date", "total_ttc": "gross_amount"}),
        ("export-logiciel-C.xlsx",
         {"number": "Num", "customer_name": "Nom client",
          "issue_date": "Date", "total_ttc": "Montant TTC"}),
    ):
        source = file_reader.read_path(SAMPLES / name)
        guessed = mapping.guess_mapping(source)
        for key, header in expected.items():
            check(f"{name} : {mapping.FIELDS_BY_KEY[key].label} -> {header}",
                  guessed[key].header, header)
        proposal = {key: item.header for key, item in guessed.items()}
        check_true(f"{name} : les champs obligatoires sont tous devinés",
                   mapping.check_mapping(proposal, source.headers).ok)

    # B has HT, VAT and gross in three columns: the gross one must win.
    source = file_reader.read_path(SAMPLES / "export-logiciel-B.csv")
    guessed = mapping.guess_mapping(source)
    check("le TTC n’est pas confondu avec le HT",
          guessed["total_ttc"].header, "gross_amount")
    check("le HT est reconnu séparément", guessed["total_ht"].header, "net_amount")
    check("la TVA aussi", guessed["vat_amount"].header, "vat_amount")

    # A column that holds no readable amount is never offered as an amount.
    fake = type("F", (), {
        "headers": ("Total TTC", "Client"),
        "rows": (("bonjour", "Durand"), ("merci", "Martin")),
    })()
    guessed = mapping.guess_mapping(fake)
    check_true("une colonne « Total TTC » sans montant n’est pas proposée",
               guessed["total_ttc"].header is None)

    # Validation catches what the owner might do wrong.
    source = file_reader.read_path(SAMPLES / "export-logiciel-A.csv")
    incomplete = mapping.check_mapping({"number": "N° facture"}, source.headers)
    check_true("une correspondance incomplète est refusée", not incomplete.ok)
    check_true("… et nomme les champs manquants", len(incomplete.missing) == 3)

    doubled = mapping.check_mapping(
        {"number": "Client", "customer_name": "Client",
         "issue_date": "Date facture", "total_ttc": "Total TTC"},
        source.headers,
    )
    check_true("une colonne utilisée deux fois est refusée", not doubled.ok)
    check("… et elle est nommée", doubled.duplicates, ("Client",))

    ghost = mapping.check_mapping(
        {"number": "Colonne Fantôme", "customer_name": "Client",
         "issue_date": "Date facture", "total_ttc": "Total TTC"},
        source.headers,
    )
    check_true("une colonne absente du fichier est refusée", not ghost.ok)


def test_import_profiles() -> None:
    section("Import — un profil épargne le deuxième fichier du même logiciel")

    with tempfile.TemporaryDirectory() as folder:
        store = mapping.ProfileStore(Path(folder) / "profils.json")
        source = file_reader.read_path(SAMPLES / "export-logiciel-A.csv")
        guessed = {key: item.header for key, item in mapping.guess_mapping(source).items()}

        check("aucun profil au départ", store.load(), [])
        check("aucune correspondance au départ", store.match(source.headers)[1], "aucun")

        store.save("Logiciel A", source.headers, guessed, source.kind)
        check("le profil est enregistré", len(store.load()), 1)

        found, quality = store.match(source.headers)
        check("le même fichier retrouve son profil", quality, "exact")
        check("… avec la bonne correspondance",
              found.mapping["total_ttc"], "Total TTC")

        # A different software must not pick it up.
        other = file_reader.read_path(SAMPLES / "export-logiciel-B.csv")
        check("un autre logiciel ne déclenche pas ce profil",
              store.match(other.headers)[1], "aucun")

        # A column added by an update still matches, and says it is partial.
        widened = list(source.headers) + ["Commentaire"]
        found, quality = store.match(widened)
        check("une colonne ajoutée reste reconnue, en partiel", quality, "partiel")

        # Saving under the same name updates rather than duplicates.
        store.save("Logiciel A", source.headers, guessed, source.kind)
        check("réenregistrer ne crée pas de doublon", len(store.load()), 1)

        store.record_use("Logiciel A")
        check("l’usage est compté", store.load()[0].use_count, 1)
        check_true("le profil se supprime", store.delete("Logiciel A"))
        check("… et il ne reste rien", store.load(), [])

        # Profiles never live in a database, so a demo reset cannot lose them.
        check_true("un profil est un fichier JSON, pas une table",
                   str(store.path).endswith(".json"))


def test_import_never_touches_demo() -> None:
    section("Import — la base de démonstration reste hors d’atteinte")

    from app.config import DB_PATH, PROFILES_PATH, REAL_DB_PATH

    check_true("la base réelle est un fichier distinct de la démo",
               DB_PATH.resolve() != REAL_DB_PATH.resolve())
    check_true("les profils sont hors des bases",
               PROFILES_PATH.suffix == ".json")

    if not DB_PATH.exists() or not SAMPLES.exists():
        FAILURES.append("base ou samples/ manquants")
        return

    before = DB_PATH.read_bytes()

    with tempfile.TemporaryDirectory() as folder:
        store = mapping.ProfileStore(Path(folder) / "profils.json")
        for name in ("export-logiciel-A.csv", "export-logiciel-B.csv",
                     "export-logiciel-C.xlsx", "convention-indecidable.csv"):
            source = file_reader.read_path(SAMPLES / name)
            guessed = {k: v.header for k, v in mapping.guess_mapping(source).items()}
            mapping.check_mapping(guessed, source.headers)
            store.save(name, source.headers, guessed, source.kind)
            store.match(source.headers)

    check("lire, deviner et enregistrer un profil ne touche pas la démo",
          DB_PATH.read_bytes(), before)



def _import_file(conn, name):
    """Read, map, reconcile and apply one sample file. Returns the plan."""
    source = file_reader.read_path(SAMPLES / name)
    columns = {key: item.header for key, item in mapping.guess_mapping(source).items()}
    rows = runner.parse_rows(source, columns)
    plan = reconcile.reconcile(rows, reconcile.load_existing(conn))
    runner.apply(conn, plan)
    return plan


def test_reconcile_names() -> None:
    section("Import — un client écrit de trois façons reste un seul client")

    key = reconcile.normalise_name("SARL Bonnet")
    check("« Bonnet SARL » a la même clé", reconcile.normalise_name("Bonnet SARL"), key)
    check("« BONNET S.A.R.L » aussi", reconcile.normalise_name("BONNET S.A.R.L"), key)
    check("« Bonnet  sarl » aussi", reconcile.normalise_name("Bonnet  sarl"), key)

    accented = reconcile.normalise_name("Boulangerie Chez Léa")
    check("les accents ne comptent pas",
          reconcile.normalise_name("BOULANGERIE CHEZ LEA"), accented)
    check("la forme juridique non plus",
          reconcile.normalise_name("BOULANGERIE CHEZ LEA S.A.R.L."), accented)

    check("les civilités sont ignorées",
          reconcile.normalise_name("M. Hervé Lecoq"),
          reconcile.normalise_name("LECOQ HERVE"))

    check_true("deux clients différents gardent des clés différentes",
               reconcile.normalise_name("SARL Bonnet")
               != reconcile.normalise_name("SARL Bonnard"))

    check("un numéro de facture se normalise sur les espaces et la casse",
          reconcile.normalise_number(" fa-2026-0413 "), "FA-2026-0413")


def test_reconcile_second_import() -> None:
    section("Import — le deuxième import réconcilie, il n’insère pas")

    if not SAMPLES.exists():
        FAILURES.append("samples/ manquant — lancez scripts/make_samples.py")
        return

    folder = tempfile.mkdtemp()
    conn = db_module.connect(Path(folder) / "reel.db")
    db_module.init_schema(conn)
    try:
        first = _import_file(conn, "export-logiciel-A.csv")
        invoices_after_first = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        customers_after_first = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        check("le premier import crée les factures lisibles", invoices_after_first, 7)
        check("… et leurs clients", customers_after_first, 6)
        check_true("les lignes illisibles sont écartées, pas importées",
                   len(first.rejects) == 3)

        # --- the owner does a week of work by hand ------------------------
        conn.execute(
            "UPDATE customers SET late_penalty_rate_bp = 1200, "
            "cgv_reference = 'CGV pro 2024, signées', penalty_terms_confirmed = 1 "
            "WHERE match_key = ?",
            (reconcile.normalise_name("SARL Menuiserie Thibault"),),
        )
        invoice_id = conn.execute(
            "SELECT id FROM invoices WHERE number = 'FA-2026-0415'"
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO followups
                (invoice_id, level, channel, status, subject, body, days_overdue,
                 amount_due_cents, total_claimed_cents, generator, created_at)
            VALUES (?,2,'email','skipped','Relance','Corps',12,184000,184000,'mock',?)
            """,
            (invoice_id, "2026-08-01T09:00:00"),
        )
        conn.commit()

        # --- the very same file again -------------------------------------
        again = _import_file(conn, "export-logiciel-A.csv")
        check("le même fichier n’insère plus rien", len(again.inserts), 0)
        check("… et ne met rien à jour non plus", len(again.updates), 0)
        check("… et ne crée aucun client", len(again.new_customers), 0)
        check("le nombre de factures est inchangé",
              conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
              invoices_after_first)
        check("le nombre de clients est inchangé",
              conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
              customers_after_first)

        terms = conn.execute(
            "SELECT late_penalty_rate_bp, cgv_reference, penalty_terms_confirmed "
            "FROM customers WHERE match_key = ?",
            (reconcile.normalise_name("SARL Menuiserie Thibault"),),
        ).fetchone()
        check("le taux de pénalité confirmé survit", terms["late_penalty_rate_bp"], 1200)
        check("la référence CGV survit", terms["cgv_reference"], "CGV pro 2024, signées")
        check("la confirmation survit", terms["penalty_terms_confirmed"], 1)

        history = conn.execute(
            "SELECT level, status FROM followups WHERE invoice_id = ?", (invoice_id,)
        ).fetchall()
        check("l’historique de relance survit", len(history), 1)
        check("le niveau ignoré reste ignoré", history[0]["status"], "skipped")

        item = [
            entry for entry in engine.load_all(conn)
            if entry.number == "FA-2026-0415"
        ]
        if item:
            check_true("un niveau ignoré n’est pas reproposé",
                       item[0].highest_followup_level >= 2)

        # --- next week's export -------------------------------------------
        week2 = _import_file(conn, "export-logiciel-A-semaine2.csv")
        check("une facture nouvelle est bien insérée", len(week2.inserts), 1)
        check("deux factures passent à réglée", week2.settled_count, 2)
        check("aucun client de plus", len(week2.new_customers), 0)
        check("le client écrit « BOULANGERIE CHEZ LEA S.A.R.L. » est reconnu",
              conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
              customers_after_first)

        settled = conn.execute(
            "SELECT status, amount_paid_cents FROM invoices WHERE number = 'FA-2026-0417'"
        ).fetchone()
        check("la facture réglée entre les deux fichiers est marquée réglée",
              settled["status"], "paid")
        check("… avec le montant encaissé", settled["amount_paid_cents"], 189_000)

        check("les factures absentes du fichier sont comptées", week2.absent_invoices, 1)
        check_true("… et nommées", "FA-2026-0412" in week2.absent_examples)
        kept = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE number = 'FA-2026-0412'"
        ).fetchone()[0]
        check("… mais jamais supprimées", kept, 1)

        still_there = conn.execute(
            "SELECT COUNT(*) FROM followups WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()[0]
        check("l’historique survit aussi au deuxième import", still_there, 1)
    finally:
        conn.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_reconcile_total_change() -> None:
    section("Import — un total qui change est signalé, jamais appliqué")

    folder = tempfile.mkdtemp()
    conn = db_module.connect(Path(folder) / "reel.db")
    db_module.init_schema(conn)
    try:
        _import_file(conn, "export-logiciel-A.csv")
        before = conn.execute(
            "SELECT total_ttc_cents, amount_paid_cents, status FROM invoices "
            "WHERE number = 'FA-2026-0415'"
        ).fetchone()

        source = file_reader.read_path(SAMPLES / "export-logiciel-A.csv")
        columns = {k: v.header for k, v in mapping.guess_mapping(source).items()}
        rows = runner.parse_rows(source, columns)
        for row in rows:
            if row.number == "FA-2026-0415":
                row.total_ttc_cents = 999_900        # a credit note, say
                row.amount_paid_cents = 999_900

        plan = reconcile.reconcile(rows, reconcile.load_existing(conn))
        check("la facture au total modifié est signalée", len(plan.flagged), 1)
        check("… et non comptée comme mise à jour",
              [r.number for r in plan.updates].count("FA-2026-0415"), 0)
        check_true("… avec les deux montants pour comparaison",
                   plan.flagged[0].existing_total_cents == before["total_ttc_cents"])

        runner.apply(conn, plan)
        after = conn.execute(
            "SELECT total_ttc_cents, amount_paid_cents, status FROM invoices "
            "WHERE number = 'FA-2026-0415'"
        ).fetchone()
        check("le total en base n’a pas bougé",
              after["total_ttc_cents"], before["total_ttc_cents"])
        check("le règlement n’a pas été appliqué non plus",
              after["amount_paid_cents"], before["amount_paid_cents"])
        check("le statut est intact", after["status"], before["status"])
    finally:
        conn.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_reconcile_leaves_demo_alone() -> None:
    section("Import — importer pour de vrai ne touche pas la démonstration")

    from app.config import DB_PATH

    if not DB_PATH.exists():
        FAILURES.append("data/garage.db absente")
        return

    before = DB_PATH.read_bytes()
    folder = tempfile.mkdtemp()
    conn = db_module.connect(Path(folder) / "reel.db")
    db_module.init_schema(conn)
    try:
        _import_file(conn, "export-logiciel-A.csv")
        _import_file(conn, "export-logiciel-A-semaine2.csv")
        _import_file(conn, "export-logiciel-B.csv")
        check("un import complet laisse la démo intacte", DB_PATH.read_bytes(), before)
        check_true("les factures importées sont marquées comme telles",
                   conn.execute(
                       "SELECT COUNT(*) FROM invoices WHERE source = 'import'"
                   ).fetchone()[0] > 0)
    finally:
        conn.close()
        shutil.rmtree(folder, ignore_errors=True)



def test_guessed_kind_claims_nothing() -> None:
    section("Un type de client deviné ne déclenche aucune indemnité")

    # The indemnity is statutory, but it is owed between professionals. An
    # imported "Menuiserie Martin" may well be M. Martin himself.
    check("professionnel confirmé : indemnité due",
          engine.recovery_fee_cents("professionnel", 4, True), RECOVERY_FEE_CENTS)
    check("professionnel seulement deviné : rien n’est réclamé",
          engine.recovery_fee_cents("professionnel", 4, False), 0)
    check("particulier confirmé : rien non plus",
          engine.recovery_fee_cents("particulier", 4, True), 0)
    check("particulier deviné : rien non plus",
          engine.recovery_fee_cents("particulier", 4, False), 0)

    reference = date(2026, 8, 28)
    guessed = engine.evaluate(
        _row(customer_kind="professionnel", kind_confirmed=0,
             customer_name="Menuiserie Martin"),
        reference,
    )
    check("facture d’un professionnel deviné : aucune indemnité",
          guessed.recovery_fee_cents, 0)
    check_true("… et la facture est signalée", guessed.recovery_fee_blocked)
    check("… on ne réclame que le principal et les pénalités",
          guessed.total_claimed_cents,
          guessed.amount_due_cents + guessed.penalty_cents)

    confirmed = engine.evaluate(
        _row(customer_kind="professionnel", kind_confirmed=1), reference
    )
    check("professionnel confirmé : l’indemnité revient",
          confirmed.recovery_fee_cents, RECOVERY_FEE_CENTS)
    check_true("… et rien n’est signalé", not confirmed.recovery_fee_blocked)

    # The demo roster is hand-written, so its kinds are confirmed.
    conn = db_module.connect()
    try:
        unconfirmed_kinds = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE kind_confirmed = 0"
        ).fetchone()[0]
        check("les clients de démonstration ont un type confirmé",
              unconfirmed_kinds, 0)
    finally:
        conn.close()


def test_imported_customer_is_a_guess() -> None:
    section("Import — un client importé arrive deviné, donc sans indemnité")

    folder = tempfile.mkdtemp()
    conn = db_module.connect(Path(folder) / "reel.db")
    db_module.init_schema(conn)
    try:
        _import_file(conn, "export-logiciel-A.csv")

        rows = conn.execute(
            "SELECT name, kind, kind_confirmed, penalty_terms_confirmed "
            "FROM customers"
        ).fetchall()
        check_true("des clients ont bien été importés", len(rows) > 0)
        check("aucun type importé n’est confirmé",
              sum(row["kind_confirmed"] for row in rows), 0)
        check("aucune condition de pénalité n’est confirmée",
              sum(row["penalty_terms_confirmed"] for row in rows), 0)

        pros = [row for row in rows if row["kind"] == "professionnel"]
        check_true("le nom a bien fait deviner des professionnels", len(pros) > 0)

        # Not one euro of indemnity on any imported invoice, whatever the name.
        claimed = [
            item for item in engine.load_all(conn) if item.recovery_fee_cents
        ]
        check("aucune indemnité sur une facture importée", claimed, [])

        blocked = [item for item in engine.load_all(conn) if item.recovery_fee_blocked]
        check_true("les professionnels devinés et échus sont signalés",
                   all(item.customer_kind == "professionnel" for item in blocked))

        flagged = kpi.unconfirmed_terms(conn)
        check_true("ils apparaissent au même endroit que les CGV non confirmées",
                   all(entry.needs_kind or entry.needs_cgv for entry in flagged))
    finally:
        conn.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_payment_term_is_asked() -> None:
    section("Import — le délai de paiement est une réponse, pas une constante")

    from app.config import DEFAULT_PAYMENT_TERM_DAYS, PAYMENT_TERM_CHOICES

    offered = [days for days, _label in PAYMENT_TERM_CHOICES]
    check("les délais proposés", offered, [0, 15, 30, 45, 60])
    check("30 jours est présélectionné", DEFAULT_PAYMENT_TERM_DAYS, 30)

    source = file_reader.read_path(SAMPLES / "export-logiciel-A.csv")
    columns = {k: v.header for k, v in mapping.guess_mapping(source).items()}
    columns["due_date"] = None                    # the file has no due date

    for term in (0, 15, 30, 60):
        rows = runner.parse_rows(source, columns, date(2026, 8, 28), term)
        readable = [row for row in rows if not row.problem]
        check_true(f"délai {term} j : toutes les échéances sont calculées",
                   all(row.due_date_inferred for row in readable))
        first = readable[0]
        check(f"délai {term} j : échéance = date de facture + {term}",
              (first.due_date - first.issue_date).days, term)
        check_true(f"délai {term} j : chaque ligne le dit",
                   any("Échéance absente" in warning for warning in first.warnings))

    # The answer travels with the profile, so it is asked once.
    with tempfile.TemporaryDirectory() as folder:
        store = mapping.ProfileStore(Path(folder) / "profils.json")
        store.save("Logiciel A", source.headers, columns, "csv", 45)
        saved, quality = store.match(source.headers)
        check("le profil retient le délai", saved.payment_term_days, 45)
        check("… et reste reconnu", quality, "exact")


def test_eml_handover() -> None:
    section("Remettre un .eml n’est pas envoyer")

    from app.eml import build_eml

    raw = build_eml(
        "client@example.com",
        "DERNIER AVIS — facture FA-2026-1228 impayée",
        "Bonjour,\n\nSolde dû : 2 153,04 €.\n",
        reply_to="contact@garage-martel.example.com",
    )
    parsed = email.message_from_bytes(raw, policy=email.policy.default)

    check("le destinataire est renseigné", parsed["To"], "client@example.com")
    check("l’objet accentué revient intact après décodage",
          parsed["Subject"], "DERNIER AVIS — facture FA-2026-1228 impayée")
    check_true("… et il est bien encodé MIME dans le fichier",
               b"=?utf-8?" in raw)
    check_true("le corps est en UTF-8", "2 153,04 €" in parsed.get_content())
    check("aucun expéditeur n’est imposé", parsed["From"], None)
    check("X-Unsent ouvre le message en rédaction dans Outlook",
          parsed["X-Unsent"], "1")
    check_true("aucun identifiant de messagerie n’est stocké",
               "password" not in raw.decode("utf-8", "replace").lower())

    # The status chain: validated -> handed over -> sent, and only the last
    # one is a sending.
    folder = tempfile.mkdtemp()
    conn = db_module.connect(Path(folder) / "demo.db")
    try:
        seed.generate(conn, date(2026, 8, 28))
        followup_module.create_drafts(conn, date(2026, 8, 28))
        row = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "email"
        ][0]
        followup_id = row["id"]

        before = kpi.last_30_days(conn, date(2026, 8, 28)).followups_sent

        conn.execute(
            "UPDATE followups SET status = 'validated' WHERE id = ?", (followup_id,)
        )
        conn.execute(
            "UPDATE followups SET status = 'handed_over', handed_over_at = ? "
            "WHERE id = ?",
            ("2026-08-28T10:00:00", followup_id),
        )
        conn.commit()

        handed = kpi.last_30_days(conn, date(2026, 8, 28)).followups_sent
        check("un message remis ne compte pas comme envoyé", handed, before)

        stored = conn.execute(
            "SELECT status, sent_at FROM followups WHERE id = ?", (followup_id,)
        ).fetchone()
        check("… son statut le dit", stored["status"], "handed_over")
        check("… et il n’a pas de date d’envoi", stored["sent_at"], None)
        check("… il reste visible dans la file",
              len(followup_module.queue(conn, "handed_over")), 1)

        # A handed-over level is still a handled level: it is not re-proposed.
        invoice_id = conn.execute(
            "SELECT invoice_id FROM followups WHERE id = ?", (followup_id,)
        ).fetchone()["invoice_id"]
        item = [x for x in engine.load_all(conn, date(2026, 8, 28)) if x.invoice_id == invoice_id]
        if item:
            check_true("un niveau remis n’est pas reproposé",
                       not item[0].followup_is_due)

        conn.execute(
            "UPDATE followups SET status = 'sent', sent_at = ? WHERE id = ?",
            ("2026-08-28T10:05:00", followup_id),
        )
        conn.commit()
        confirmed = kpi.last_30_days(conn, date(2026, 8, 28)).followups_sent
        check("seule la confirmation du garagiste compte comme envoi",
              confirmed, before + 1)
    finally:
        conn.close()
        shutil.rmtree(folder, ignore_errors=True)



def test_gmail_threshold() -> None:
    section("Gmail — un lien trop long n’est jamais proposé")

    from app.eml import (
        SAFE_URL_LENGTH, gmail_compose_empty_url, gmail_compose_url,
        gmail_handover, gmail_url_fits,
    )

    url = gmail_compose_url("client@example.com", "Objet accentué é",
                            "Bonjour,\n\nSolde : 2 153,04 €.")
    check_true("le lien vise bien la fenêtre de rédaction Gmail",
               url.startswith(gmail_compose_url("", "", "").split("&to=")[0]))
    check_true("le destinataire est encodé", "to=client%40example.com" in url)
    check_true("l’objet accentué est encodé", "%C3%A9" in url)
    check_true("le corps est encodé", "body=" in url)
    check_true("les retours à la ligne survivent", "%0A" in url)
    check_true("rien n’est connecté : ni jeton, ni identifiant",
               "token" not in url and "auth" not in url)

    # The rule itself.
    short = ("a@b.fr", "Objet", "Court.")
    check_true("un message court passe par le lien direct", gmail_url_fits(*short))
    long_body = "x" * (SAFE_URL_LENGTH + 500)
    check_true("un message trop long ne passe pas",
               not gmail_url_fits("a@b.fr", "Objet", long_body))

    offered = gmail_handover(*short)
    check_true("le message court reçoit un lien pré-rempli", bool(offered["url"]))
    check_true("… et le repli reste disponible", bool(offered["empty_url"]))

    withheld = gmail_handover("a@b.fr", "Objet", long_body)
    check("un message trop long n’a AUCUN lien pré-rempli", withheld["url"], None)
    check_true("… mais garde une fenêtre vide vers le bon destinataire",
               "to=a%40b.fr" in withheld["empty_url"])
    check_true("… et cette fenêtre vide ne contient pas le message",
               "body=" not in withheld["empty_url"])
    check_true("… la longueur mesurée est rapportée",
               withheld["length"] > withheld["limit"])

    # Exactly at the limit, and one character past it.
    body = "y" * 10
    base = len(gmail_compose_url("a@b.fr", "S", body)) - len(body)
    at_limit = "y" * (SAFE_URL_LENGTH - base)
    check_true("pile à la limite : accepté",
               gmail_url_fits("a@b.fr", "S", at_limit))
    check_true("un caractère de plus : refusé",
               not gmail_url_fits("a@b.fr", "S", at_limit + "y"))

    # And on the real messages of the system.
    conn = db_module.connect()
    try:
        truncated = []
        for item in engine.load_all(conn):
            if item.level < 1:
                continue
            draft = followup_module.draft_for(item)
            offered = gmail_handover(item.email, draft.subject, draft.body)
            if offered["url"] and len(offered["url"]) > SAFE_URL_LENGTH:
                truncated.append(item.number)
        check("aucun lien proposé ne dépasse la limite", truncated, [])

        levels = {}
        for item in engine.load_all(conn):
            if item.level < 1:
                continue
            draft = followup_module.draft_for(item)
            fits = gmail_url_fits(item.email, draft.subject, draft.body)
            levels.setdefault(item.level, []).append(fits)
        check_true("les relances de niveau 1 tiennent toutes dans un lien",
                   all(levels.get(1, [True])))
        check_true("certaines relances longues basculent en copier-coller",
                   any(not fits for fits in levels.get(2, []) + levels.get(3, [])))
    finally:
        conn.close()


def test_stale_drafts() -> None:
    section("Brouillons oubliés — un message remis ne devient jamais dangereux")

    folder = tempfile.mkdtemp()
    conn = db_module.connect(Path(folder) / "demo.db")
    try:
        reference = date(2026, 8, 30)
        seed.generate(conn, reference)
        followup_module.create_drafts(conn, reference)

        row = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "email"
        ][0]
        followup_id, invoice_id = row["id"], row["invoice_id"]

        conn.execute(
            "UPDATE followups SET status = 'handed_over', handed_over_at = ? "
            "WHERE id = ?", ("2026-08-10T09:00:00", followup_id),
        )
        conn.commit()

        check("le message ouvert reste visible dans sa propre liste",
              len(followup_module.queue(conn, "handed_over")), 1)
        check("il n’est pas compté comme envoyé",
              kpi.last_30_days(conn, reference).followups_sent,
              conn.execute(
                  "SELECT COUNT(*) FROM followups WHERE status = 'sent' "
                  "AND sent_at >= '2026-08-01' AND sent_at < '2026-09-01'"
              ).fetchone()[0])

        # The invoice gets paid. The open follow-up must close.
        before_sent = kpi.last_30_days(conn, reference).followups_sent
        conn.execute(
            "UPDATE invoices SET amount_paid_cents = total_ttc_cents, "
            "status = 'paid', paid_date = ? WHERE id = ?",
            ("2026-08-29", invoice_id),
        )
        conn.commit()

        existing = reconcile.load_existing(conn)
        check_true("la relance ouverte est bien repérée comme encore en jeu",
                   invoice_id in existing.open_followups)

        customer_name = conn.execute(
            "SELECT c.name FROM invoices i "
            "JOIN customers c ON c.id = i.customer_id WHERE i.id = ?",
            (invoice_id,),
        ).fetchone()["name"]
        parsed = [
            runner.ParsedRow(
                line=2,
                number=conn.execute(
                    "SELECT number FROM invoices WHERE id = ?", (invoice_id,)
                ).fetchone()["number"],
                customer_name=customer_name,
                issue_date=date(2026, 7, 1), due_date=date(2026, 7, 16),
                total_ttc_cents=conn.execute(
                    "SELECT total_ttc_cents FROM invoices WHERE id = ?", (invoice_id,)
                ).fetchone()["total_ttc_cents"],
            )
        ]
        parsed[0].amount_paid_cents = parsed[0].total_ttc_cents

        plan = reconcile.reconcile(parsed, existing)
        check("la relance devenue sans objet est nommée",
              len(plan.closed_followups), 1)
        check("… avec son statut d’origine",
              plan.closed_followups[0].was_status, "handed_over")
        check_true("… et elle apparaît dans le récapitulatif",
                   "sans objet" in plan.headline)

        result = runner.apply(conn, plan)
        check("la fermeture est comptée", result.followups_closed, 1)

        closed = conn.execute(
            "SELECT status, sent_at FROM followups WHERE id = ?", (followup_id,)
        ).fetchone()
        check("la relance est fermée", closed["status"], "obsolete")
        check("… sans date d’envoi", closed["sent_at"], None)
        check("… elle quitte la file des messages ouverts",
              len(followup_module.queue(conn, "handed_over")), 0)
        check("… et celle des messages à valider",
              [item["id"] for item in followup_module.queue(conn, "pending")
               if item["id"] == followup_id], [])
        check("une relance fermée ne compte jamais comme envoyée",
              kpi.last_30_days(conn, reference).followups_sent, before_sent)

        # A closed level is not a handled level: if the invoice ever reopens,
        # the follow-up may be proposed again.
        check_true("« sans objet » n’est pas un niveau traité",
                   "obsolete" not in engine.HANDLED_STATUSES)
    finally:
        conn.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_second_handover_is_flagged() -> None:
    section("Rouvrir deux fois le même message est signalé")

    folder = tempfile.mkdtemp()
    conn = db_module.connect(Path(folder) / "demo.db")
    try:
        reference = date(2026, 8, 30)
        seed.generate(conn, reference)
        followup_module.create_drafts(conn, reference)
        row = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "email"
        ][0]
        followup_id = row["id"]

        first = conn.execute(
            "SELECT handed_over_at FROM followups WHERE id = ?", (followup_id,)
        ).fetchone()["handed_over_at"]
        check("aucune ouverture au départ", first, None)

        conn.execute(
            "UPDATE followups SET status = 'handed_over', handed_over_at = ? "
            "WHERE id = ?", ("2026-08-30T09:00:00", followup_id),
        )
        conn.commit()

        already = conn.execute(
            "SELECT handed_over_at FROM followups WHERE id = ?", (followup_id,)
        ).fetchone()["handed_over_at"]
        check_true("la date d’ouverture est retenue", bool(already))
        check_true("c’est elle qui permet d’avertir d’un doublon",
                   already.startswith("2026-08-30"))
    finally:
        conn.close()
        shutil.rmtree(folder, ignore_errors=True)



def test_both_handover_routes_agree() -> None:
    section("Les deux voies de remise produisent le même état")

    folder = tempfile.mkdtemp()
    conn = db_module.connect(Path(folder) / "demo.db")
    try:
        reference = date(2026, 8, 30)
        seed.generate(conn, reference)
        followup_module.create_drafts(conn, reference)

        emails = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "email"
        ]
        by_eml, by_gmail = emails[0]["id"], emails[1]["id"]

        for followup_id in (by_eml, by_gmail):
            conn.execute(
                "UPDATE followups SET status = 'validated' WHERE id = ?",
                (followup_id,),
            )
        conn.commit()

        # Whichever route the owner takes, the row must land in exactly the
        # same state: opened, not sent, still waiting for his confirmation.
        for followup_id in (by_eml, by_gmail):
            conn.execute(
                "UPDATE followups SET status = 'handed_over', handed_over_at = ? "
                "WHERE id = ?", ("2026-08-30T09:00:00", followup_id),
            )
        conn.commit()

        rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, status, handed_over_at, sent_at FROM followups "
                "WHERE id IN (?, ?)", (by_eml, by_gmail)
            )
        }
        check("la voie .eml met la relance en « remise »",
              rows[by_eml]["status"], "handed_over")
        check("la voie Gmail aussi", rows[by_gmail]["status"], "handed_over")
        check("les deux portent une date d’ouverture",
              [bool(rows[i]["handed_over_at"]) for i in (by_eml, by_gmail)],
              [True, True])
        check("aucune des deux n’est marquée envoyée",
              [rows[i]["sent_at"] for i in (by_eml, by_gmail)], [None, None])
        check("les deux attendent la confirmation dans la même liste",
              len(followup_module.queue(conn, "handed_over")), 2)
        check("ni l’une ni l’autre n’entre au compteur du mois",
              kpi.last_30_days(conn, reference).followups_sent,
              conn.execute(
                  "SELECT COUNT(*) FROM followups WHERE status = 'sent' "
                  "AND sent_at >= '2026-08-01' AND sent_at < '2026-09-01'"
              ).fetchone()[0])
    finally:
        conn.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_demo_mode_blocks_every_send() -> None:
    section("Mode démonstration : une relance n’ouvre jamais de connexion")

    from app import sending

    settings = sending.SmtpSettings(
        sender="garage@example.com", host="smtp.gmail.com", port=587,
        password="abcdefghijklmnop",
    )

    # Any attempt to open a socket explodes loudly. If the guard were placed
    # after the connection rather than before it, this test would fail.
    attempts = []

    class Tripwire:
        def __init__(self, *args, **kwargs):
            attempts.append(args)
            raise AssertionError("une connexion SMTP a été tentée")

    original_smtp = smtplib.SMTP
    original_ssl = smtplib.SMTP_SSL
    original_demo = sending.DEMO_MODE
    smtplib.SMTP = Tripwire
    smtplib.SMTP_SSL = Tripwire
    try:
        sending.DEMO_MODE = True
        try:
            sending.send(settings, "client@example.com", "Objet", "Corps")
            FAILURES.append("un envoi a été accepté en mode démonstration")
        except AssertionError:
            FAILURES.append("une connexion SMTP a été ouverte en mode démonstration")
        except sending.SendingProblem as problem:
            PASSED_COUNT("l’envoi est refusé en mode démonstration")
            check_true("… avec un motif explicite",
                       "démonstration" in problem.reason)
        check("aucune connexion n’a été tentée", attempts, [])

        # The preview path shows the message without touching the network.
        shown = sending.preview(settings, "client@example.com", "Objet", "Corps")
        check("l’aperçu montre le destinataire", shown["to"], "client@example.com")
        check("l’aperçu montre l’objet", shown["subject"], "Objet")
        check("l’aperçu montre le corps", shown["body"], "Corps")
        check_true("l’aperçu montre l’expéditeur", bool(shown["from"]))
        check("l’aperçu n’ouvre aucune connexion", attempts, [])

        # The connection test is the one deliberate exemption: it writes to the
        # owner's own address, and it must work without leaving demo mode.
        try:
            sending.send_test(settings)
        except AssertionError:
            PASSED_COUNT("le test de connexion tente bien une connexion")
        except sending.SendingProblem:
            PASSED_COUNT("le test de connexion tente bien une connexion")
        check_true("… c’est la seule chose qui ouvre une socket en mode démo",
                   len(attempts) == 1)
        check("… et elle vise le serveur configuré", attempts[0][0], settings.host)
        attempts.clear()

        # Out of demo mode, an unconfigured setup still refuses before the socket.
        sending.DEMO_MODE = False
        try:
            sending.send(sending.SmtpSettings(), "client@example.com", "O", "C")
            FAILURES.append("un envoi sans configuration a été accepté")
        except AssertionError:
            FAILURES.append("un envoi sans configuration a ouvert une connexion")
        except sending.SendingProblem as problem:
            PASSED_COUNT("sans configuration, rien n’est tenté")
            check_true("… et le motif dit quoi faire",
                       "configuré" in problem.reason or "configur" in problem.reason)
        check("encore aucune connexion tentée", attempts, [])
    finally:
        smtplib.SMTP = original_smtp
        smtplib.SMTP_SSL = original_ssl
        sending.DEMO_MODE = original_demo


def test_sending_failures_are_actionable() -> None:
    section("Un envoi qui échoue le dit en français, et ne ment jamais")

    from app import sending

    cases = [
        (smtplib.SMTPAuthenticationError(
            535, b"5.7.8 Username and Password not accepted"),
         ("mot de passe", "identifiants")),
        (smtplib.SMTPAuthenticationError(
            534, b"5.7.9 Application-specific password required"),
         ("application",)),
        (smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded"),
         ("quota", "500")),
        (socket.gaierror("Name or service not known"), ("injoignable",)),
        (ConnectionRefusedError("refused"), ("injoignable",)),
        (TimeoutError("timed out"), ("injoignable",)),
        (smtplib.SMTPRecipientsRefused({"a@b.fr": (550, b"no such user")}),
         ("destinataire",)),
        (smtplib.SMTPSenderRefused(553, b"bad sender", "x@y.fr"), ("expéditeur",)),
    ]
    for error, expected in cases:
        reason = sending.explain(error)
        check_true(f"{type(error).__name__} : message actionnable",
                   any(word in reason.lower() for word in expected))
        check_true(f"{type(error).__name__} : rédigé en français",
                   len(reason) > 30 and " " in reason)

    # A failure must never look like a success.
    settings = sending.SmtpSettings(
        sender="garage@example.com", host="127.0.0.1", port=1,
        password="abcdefghijklmnop", security="starttls",
    )
    original_demo = sending.DEMO_MODE
    try:
        sending.DEMO_MODE = False
        try:
            sending.send(settings, "client@example.com", "Objet", "Corps")
            FAILURES.append("un envoi vers un port fermé a été signalé réussi")
        except sending.SendingProblem as problem:
            PASSED_COUNT("un serveur injoignable lève une erreur, pas un succès")
            check_true("… avec un motif utile", bool(problem.reason))
    finally:
        sending.DEMO_MODE = original_demo


def test_sending_settings_stay_out_of_the_data() -> None:
    section("Les identifiants ne touchent ni la base ni le jeu de démonstration")

    from app import sending
    from app.config import DB_PATH

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "envoi-smtp.json"
        secret = "zzsecretzzpasswd"
        settings = sending.SmtpSettings(
            sender="garage@example.com", host="smtp.gmail.com", port=587,
            password=secret, sender_name="Garage Martel",
        )
        check_true("une configuration incomplète n’est pas « prête »",
                   not sending.SmtpSettings().configured)
        check_true("une configuration complète l’est", settings.configured)

        sending.save_settings(settings, path)
        check_true("les identifiants vivent dans un fichier JSON", path.exists())

        reloaded = sending.load_settings(path)
        check("l’adresse est relue", reloaded.sender, "garage@example.com")
        check("le mot de passe est relu", reloaded.password, secret)
        check_true("l’écran n’affiche jamais le mot de passe",
                   secret not in reloaded.masked())

        if DB_PATH.exists():
            check_true("le mot de passe n’apparaît pas dans la base de démo",
                       secret.encode() not in DB_PATH.read_bytes())

        check_true("les identifiants se suppriment", sending.clear_settings(path))
        check_true("… et le fichier disparaît", not path.exists())

    # Presets exist for the providers a French garage actually uses.
    for key in ("gmail", "microsoft", "ovh", "gandi"):
        check_true(f"préréglage {key} disponible", key in sending.PROVIDERS)
    check("Gmail passe par le port 587", sending.PROVIDERS["gmail"]["port"], 587)
    check("… en STARTTLS", sending.PROVIDERS["gmail"]["security"], "starttls")
    check_true("… avec la marche à suivre pour le mot de passe d’application",
               "application" in sending.PROVIDERS["gmail"]["help"])



def test_demo_previews_instead_of_hiding() -> None:
    section("Mode démonstration : le bouton Envoyer montre un aperçu")

    from fastapi.testclient import TestClient

    from app import db as app_db
    from app import sending
    import app.server as srv

    folder = tempfile.mkdtemp()
    demo_db = Path(folder) / "demo.db"

    original_path = app_db.DB_PATH
    original_settings = sending.SMTP_SETTINGS_PATH
    original_load = sending.load_settings
    attempts = []

    class Tripwire:
        def __init__(self, *args, **kwargs):
            attempts.append(args)
            raise AssertionError("une connexion SMTP a été tentée")

    original_smtp, original_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = Tripwire
    smtplib.SMTP_SSL = Tripwire

    try:
        app_db.DB_PATH = demo_db
        # No credentials at all on this machine, as on a prospect's laptop.
        sending.load_settings = lambda path=None: sending.SmtpSettings()

        conn = app_db.connect(demo_db)
        # Today's real date, not a fixed one: the send below is stamped with
        # the real clock, and a hard-coded month makes this check fail every
        # time the calendar rolls over.
        reference = date.today()
        seed.generate(conn, reference)
        followup_module.create_drafts(conn, reference)
        row = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "email"
        ][0]
        followup_id = row["id"]
        courrier_id = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "courrier"
        ][0]["id"]
        before = kpi.last_30_days(conn, reference).followups_sent
        conn.close()

        client = TestClient(srv.app)

        # An unvalidated message cannot be sent, demo or not.
        refused = client.post(f"/api/followups/{followup_id}/envoyer?contexte=demo")
        check("une relance non validée est refusée", refused.status_code, 409)
        check("aucune connexion tentée pour autant", attempts, [])

        client.post(f"/api/followups/{followup_id}/validate?contexte=demo")
        answer = client.post(f"/api/followups/{followup_id}/envoyer?contexte=demo")
        check("le bouton Envoyer répond en mode démo", answer.status_code, 200)

        payload = answer.json()
        check_true("… et il réussit", payload["ok"])
        check_true("… en signalant que c’est une démonstration", payload["demo"])
        check("… un aperçu reste validé, pas envoyé",
              payload["status"], "validated")
        check_true("… un aperçu complet est renvoyé",
                   all(key in payload["preview"]
                       for key in ("from", "to", "subject", "body")))
        check_true("… avec un expéditeur crédible même sans configuration",
                   "@" in payload["preview"]["from"])
        check("AUCUNE connexion SMTP n’a été ouverte", attempts, [])

        conn = app_db.connect(demo_db)
        try:
            stored = conn.execute(
                "SELECT status, sent_at FROM followups WHERE id = ?", (followup_id,)
            ).fetchone()
            check("l’aperçu conserve la relance validée", stored["status"], "validated")
            check("… sans fausse date d’envoi", stored["sent_at"], None)
            check("… et sans gonfler le compteur du mois",
                  kpi.last_30_days(conn, reference).followups_sent, before)
        finally:
            conn.close()

        # A formal demand is still never an e-mail, demo or not.
        client.post(f"/api/followups/{courrier_id}/validate?contexte=demo")
        letter = client.post(f"/api/followups/{courrier_id}/envoyer?contexte=demo")
        check("une mise en demeure reste refusée à l’envoi",
              letter.status_code, 400)
        check("toujours aucune connexion SMTP", attempts, [])
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = original_smtp, original_ssl
        sending.load_settings = original_load
        sending.SMTP_SETTINGS_PATH = original_settings
        app_db.DB_PATH = original_path
        shutil.rmtree(folder, ignore_errors=True)



def test_only_the_send_scope_exists() -> None:
    section("Google — une seule autorisation, et jamais une de lecture")

    from app import google_mail

    # Exactly three, and nothing else: the Gmail send scope, plus the two basic
    # scopes that let the interface name the connected account.
    check("l’application demande exactement trois autorisations",
          list(google_mail.scopes_in_use()),
          ["https://www.googleapis.com/auth/gmail.send", "openid", "email"])
    check("une seule est une autorisation Gmail",
          [scope for scope in google_mail.scopes_in_use() if "gmail" in scope],
          ["https://www.googleapis.com/auth/gmail.send"])
    check("c’est bien celle d’envoi",
          google_mail.GMAIL_SEND_SCOPE,
          "https://www.googleapis.com/auth/gmail.send")
    check_true("openid et email sont des autorisations de base, sans audit",
               set(google_mail.scopes_in_use())
               - {"https://www.googleapis.com/auth/gmail.send"}
               == {"openid", "email"})

    # The URL the owner is actually sent to must carry that scope and no other.
    original_client = google_mail.load_client
    google_mail.load_client = lambda path=None: google_mail.ClientApp(
        client_id="test.apps.googleusercontent.com", client_secret="secret"
    )
    try:
        url = google_mail.start_flow("http://127.0.0.1:8000/parametres/envoi")
    finally:
        google_mail.load_client = original_client

    query = parse_qs(urlparse(url).query)
    check("l’URL d’autorisation porte exactement ces trois scopes",
          query["scope"][0].split(),
          ["https://www.googleapis.com/auth/gmail.send", "openid", "email"])
    check("… dont une seule touche Gmail",
          [scope for scope in query["scope"][0].split() if "gmail" in scope],
          ["https://www.googleapis.com/auth/gmail.send"])
    check("… en accès hors ligne, pour obtenir un jeton durable",
          query["access_type"], ["offline"])
    check("… avec un état anti-rejeu", len(query["state"][0]) >= 20, True)
    check_true("… et une redirection en boucle locale",
               query["redirect_uri"][0].startswith("http://127.0.0.1:"))

    # The check that protects against a months-long paid audit: no scope that
    # can read a mailbox may appear anywhere in the source. The forbidden names
    # are assembled at runtime so this file never contains them literally.
    base = "https://www.googleapis.com/auth/"
    forbidden = [base + name for name in (
        "gmail." + "readonly", "gmail." + "modify", "gmail." + "metadata",
        "gmail." + "settings.basic", "gmail." + "compose", "gmail." + "insert",
        "gmail." + "labels", "contacts", "userinfo.profile",
    )]
    forbidden.append("https://" + "mail.google.com/")

    root = Path(__file__).resolve().parent.parent
    scanned, offenders = 0, []
    for path in sorted(root.rglob("*.py")) + sorted(root.rglob("*.html")):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        scanned += 1
        for scope in forbidden:
            if scope in content:
                offenders.append(f"{path.name} contient {scope}")

    check("aucun scope de lecture Gmail dans tout le projet", offenders, [])
    check_true("… et le contrôle a bien parcouru le code", scanned > 20)

    # gmail.send itself must appear, or the scan above would pass vacuously.
    module = (root / "app" / "google_mail.py").read_text(encoding="utf-8")
    check_true("le scope d’envoi est bien celui déclaré",
               "https://www.googleapis.com/auth/gmail.send" in module)


def test_expired_connection_leaves_the_followup_alone() -> None:
    section("Google — une connexion expirée ne perd jamais la relance")

    from fastapi.testclient import TestClient

    from app import db as app_db
    from app import google_mail
    from app import sending
    import app.data_context as contexts
    import app.server as srv

    folder = tempfile.mkdtemp()
    demo_db = Path(folder) / "demo.db"
    original_path = app_db.DB_PATH
    original_demo = srv.DEMO_MODE
    original_context_demo = contexts.DEMO_MODE
    original_real = contexts.REAL
    original_token = google_mail.load_token
    original_send = google_mail.send

    try:
        app_db.DB_PATH = demo_db
        conn = app_db.connect(demo_db)
        reference = date(2026, 8, 31)
        seed.generate(conn, reference)
        followup_module.create_drafts(conn, reference)
        row = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "email"
        ][0]
        followup_id = row["id"]
        before = kpi.last_30_days(conn, reference).followups_sent
        conn.close()

        # A connected account whose refresh token has lapsed — the normal case
        # after seven days while the app is in testing mode.
        google_mail.load_token = lambda path=None: google_mail.GoogleToken(
            refresh_token="stale", connected_at="2026-08-01T09:00:00",
        )

        def expired(recipient, subject, body):
            raise google_mail.GoogleProblem(
                "La connexion à votre messagerie a expiré — reconnectez-vous.",
                expired=True,
            )

        google_mail.send = expired
        srv.DEMO_MODE = False                 # exercise the real sending path
        contexts.DEMO_MODE = False
        contexts.REAL = contexts.DataContext(
            key="real", label="Données réelles", description="Test",
            path=demo_db, is_demo=False,
        )

        client = TestClient(srv.app)
        client.cookies.set(contexts.COOKIE_NAME, contexts.cookie_value("real"))
        client.post(f"/api/followups/{followup_id}/validate?contexte=real")
        answer = client.post(f"/api/followups/{followup_id}/envoyer?contexte=real")

        check("l’envoi échoue proprement", answer.status_code, 400)
        payload = answer.json()
        check_true("… sans prétendre avoir réussi", not payload["ok"])
        check("… avec le message attendu à l’écran",
              payload["detail"],
              "La connexion à votre messagerie a expiré — reconnectez-vous.")
        check_true("… en signalant qu’il faut se reconnecter", payload["reconnect"])
        check("… et en donnant le lien de reconnexion",
              payload["reconnect_url"], "/parametres/envoi/google/connecter")

        conn = app_db.connect(demo_db)
        try:
            stored = conn.execute(
                "SELECT status, sent_at FROM followups WHERE id = ?", (followup_id,)
            ).fetchone()
            check("la relance reste validée", stored["status"], "validated")
            check("… et non envoyée", stored["sent_at"], None)
            check("… elle attend toujours dans la file",
                  len([item for item in followup_module.queue(conn, "validated")
                       if item["id"] == followup_id]), 1)
            check("… et le compteur du mois n’a pas bougé",
                  kpi.last_30_days(conn, reference).followups_sent, before)
        finally:
            conn.close()
    finally:
        google_mail.send = original_send
        google_mail.load_token = original_token
        srv.DEMO_MODE = original_demo
        contexts.DEMO_MODE = original_context_demo
        contexts.REAL = original_real
        app_db.DB_PATH = original_path
        shutil.rmtree(folder, ignore_errors=True)


def test_google_demo_mode_opens_nothing() -> None:
    section("Google — en mode démonstration, aucune connexion n’est ouverte")

    from fastapi.testclient import TestClient
    import urllib.request

    from app import db as app_db
    from app import google_mail
    import app.data_context as contexts
    import app.server as srv

    folder = tempfile.mkdtemp()
    demo_db = Path(folder) / "demo.db"
    original_path = app_db.DB_PATH
    original_token = google_mail.load_token
    original_open = urllib.request.urlopen
    calls = []

    def tripwire(*args, **kwargs):
        calls.append(args)
        raise AssertionError("un appel réseau a été tenté")

    try:
        app_db.DB_PATH = demo_db
        conn = app_db.connect(demo_db)
        reference = date(2026, 8, 31)
        seed.generate(conn, reference)
        followup_module.create_drafts(conn, reference)
        followup_id = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "email"
        ][0]["id"]
        conn.close()

        # A fully connected Google account, and demo mode on.
        google_mail.load_token = lambda path=None: google_mail.GoogleToken(
            refresh_token="whatever", access_token="whatever",
            connected_at="2026-08-30T09:00:00",
            access_expires_at="2030-01-01T00:00:00",
        )
        urllib.request.urlopen = tripwire

        client = TestClient(srv.app)
        client.post(f"/api/followups/{followup_id}/validate?contexte=demo")
        answer = client.post(f"/api/followups/{followup_id}/envoyer?contexte=demo")

        check("le bouton répond quand même", answer.status_code, 200)
        payload = answer.json()
        check_true("… en montrant un aperçu", payload["demo"])
        check("… sans transformer un aperçu en envoi", payload["status"], "validated")
        check("AUCUN appel réseau vers Google", calls, [])
    finally:
        urllib.request.urlopen = original_open
        google_mail.load_token = original_token
        app_db.DB_PATH = original_path
        shutil.rmtree(folder, ignore_errors=True)


def test_google_token_stays_out_of_the_data() -> None:
    section("Google — le jeton ne touche ni la base ni la démonstration")

    from app import google_mail
    from app.config import DB_PATH, GOOGLE_CLIENT_PATH, GOOGLE_TOKEN_PATH

    check_true("le jeton vit dans un fichier JSON",
               GOOGLE_TOKEN_PATH.suffix == ".json")
    check_true("… hors de la base de démonstration",
               GOOGLE_TOKEN_PATH != DB_PATH)

    gitignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(
        encoding="utf-8"
    )
    check_true("l’identifiant client est ignoré par git",
               GOOGLE_CLIENT_PATH.name in gitignore)
    check_true("le jeton aussi", "google-token.json" in gitignore)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "google-token.json"
        secret = "zzrefreshtokenzz"
        google_mail.save_token(
            google_mail.GoogleToken(
                refresh_token=secret, access_token="short-lived",
                connected_at="2026-08-25T10:00:00",
            ),
            path,
        )
        reloaded = google_mail.load_token(path)
        check("le jeton est relu", reloaded.refresh_token, secret)
        check_true("la connexion est vue comme active", reloaded.connected)

        # Seven days from the connection, in testing mode.
        check("l’expiration est calculée à sept jours",
              reloaded.expires_on, date(2026, 9, 1))

        if DB_PATH.exists():
            check_true("le jeton n’apparaît pas dans la base de démo",
                       secret.encode() not in DB_PATH.read_bytes())

        check_true("le jeton s’efface", google_mail.forget_token(path))
        check_true("… et le fichier disparaît", not path.exists())
        check_true("une absence de jeton n’est pas une connexion",
                   not google_mail.load_token(path).connected)

    # Refusals from Google are translated, never shown raw.
    refusal = google_mail._explain_authorisation_error("access_denied")
    check_true("un compte non autorisé est expliqué en français",
               "utilisateurs de test" in refusal)
    check_true("… sans jargon brut", "access_denied" not in refusal)



def test_account_mismatch_warns_without_blocking() -> None:
    section("Google — un compte qui n’est pas celui du garage est signalé")

    from app import google_mail
    from app.config import GARAGE

    garage = GARAGE["email"]
    check_true("l’adresse du garage se reconnaît elle-même",
               google_mail.address_matches_garage(garage, garage))
    check_true("… quelles que soient la casse et les espaces",
               google_mail.address_matches_garage(f"  {garage.upper()} ", garage))
    check_true("un compte personnel ne correspond pas",
               not google_mail.address_matches_garage(
                   "julien.perso@example.com", garage))
    check_true("une adresse vide ne correspond pas non plus",
               not google_mail.address_matches_garage("", garage))

    # The address is read from Google's identity token, not invented.
    claims = base64.urlsafe_b64encode(
        json.dumps({"email": "julien.perso@example.com",
                    "email_verified": True}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    fake_id_token = f"header.{claims}.signature"
    check("l’adresse est lue dans le jeton d’identité",
          google_mail._address_from_id_token(fake_id_token),
          "julien.perso@example.com")
    check("un jeton illisible ne renvoie rien plutôt qu’une bêtise",
          google_mail._address_from_id_token("pas-un-jeton"), "")

    # And the dashboard says so, without stopping anything.
    import app.server as srv

    original = google_mail.load_token
    try:
        google_mail.load_token = lambda path=None: google_mail.GoogleToken(
            refresh_token="x", email="julien.perso@example.com",
            connected_at="2026-08-30T09:00:00",
        )
        banner = srv._google_banner()
        check_true("le tableau de bord signale le décalage", banner["mismatch"])
        check("… en nommant le compte connecté",
              banner["address"], "julien.perso@example.com")
        check("… et celui du garage", banner["garage_address"], garage)

        google_mail.load_token = lambda path=None: google_mail.GoogleToken(
            refresh_token="x", email=garage, connected_at="2026-08-30T09:00:00",
        )
        check_true("le bon compte ne déclenche aucun avertissement",
                   not srv._google_banner()["mismatch"])

        google_mail.load_token = lambda path=None: google_mail.GoogleToken()
        check_true("sans connexion, rien n’est affiché",
                   not srv._google_banner()["connected"])
    finally:
        google_mail.load_token = original


def test_mismatch_never_blocks_sending() -> None:
    section("Google — l’avertissement prévient, il n’interdit pas")

    from fastapi.testclient import TestClient

    from app import db as app_db
    from app import google_mail
    import app.data_context as contexts
    import app.server as srv

    folder = tempfile.mkdtemp()
    demo_db = Path(folder) / "demo.db"
    original_path = app_db.DB_PATH
    original_token = google_mail.load_token
    original_send = google_mail.send
    original_demo = srv.DEMO_MODE
    original_context_demo = contexts.DEMO_MODE
    original_real = contexts.REAL
    sent = []

    try:
        app_db.DB_PATH = demo_db
        conn = app_db.connect(demo_db)
        reference = date(2026, 8, 31)
        seed.generate(conn, reference)
        followup_module.create_drafts(conn, reference)
        followup_id = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "email"
        ][0]["id"]
        conn.close()

        # Connected with the wrong account — and it still sends.
        google_mail.load_token = lambda path=None: google_mail.GoogleToken(
            refresh_token="x", email="julien.perso@example.com",
            connected_at="2026-08-30T09:00:00",
        )
        google_mail.send = lambda recipient, subject, body: sent.append(recipient)
        srv.DEMO_MODE = False
        contexts.DEMO_MODE = False
        contexts.REAL = contexts.DataContext(
            key="real", label="Données réelles", description="Test",
            path=demo_db, is_demo=False,
        )

        client = TestClient(srv.app)
        client.cookies.set(contexts.COOKIE_NAME, contexts.cookie_value("real"))
        client.post(f"/api/followups/{followup_id}/validate?contexte=real")
        answer = client.post(f"/api/followups/{followup_id}/envoyer?contexte=real")

        check("l’envoi aboutit malgré le décalage", answer.status_code, 200)
        check_true("… le message est bien parti", len(sent) == 1)

        conn = app_db.connect(demo_db)
        try:
            check("… et la relance est marquée envoyée",
                  conn.execute(
                      "SELECT status FROM followups WHERE id = ?", (followup_id,)
                  ).fetchone()["status"], "sent")
        finally:
            conn.close()
    finally:
        srv.DEMO_MODE = original_demo
        contexts.DEMO_MODE = original_context_demo
        contexts.REAL = original_real
        google_mail.send = original_send
        google_mail.load_token = original_token
        app_db.DB_PATH = original_path
        shutil.rmtree(folder, ignore_errors=True)



def test_setup_hint_appears_only_when_nothing_is_connected() -> None:
    section("Tableau de bord — la démonstration reste simple et privée")

    from fastapi.testclient import TestClient

    from app import db as app_db
    from app import google_mail
    from app import sending
    import app.server as srv

    folder = tempfile.mkdtemp()
    demo_db = Path(folder) / "demo.db"
    original_path = app_db.DB_PATH
    original_token = google_mail.load_token
    original_settings = sending.load_settings

    try:
        app_db.DB_PATH = demo_db
        conn = app_db.connect(demo_db)
        seed.generate(conn, date(2026, 8, 31))
        followup_module.create_drafts(conn, date(2026, 8, 31))
        conn.close()

        client = TestClient(srv.app)
        link = 'href="/parametres/envoi"'

        # Nothing configured at all.
        google_mail.load_token = lambda path=None: google_mail.GoogleToken()
        sending.load_settings = lambda path=None: sending.SmtpSettings()
        page = client.get("/").text
        check_true("la démonstration ne réclame pas de messagerie réelle",
                   "Aucune messagerie connectée" not in page)
        check_true("les réglages restent accessibles", link in page)
        check_true("le lien d’en-tête est explicite",
                   ">Paramètres</a>" in page)
        check_true("le mode fictif est expliqué",
                   "Données fictives · aucun envoi réel" in page)

        # A Google account connected.
        google_mail.load_token = lambda path=None: google_mail.GoogleToken(
            refresh_token="x", email="contact@garage.fr",
            connected_at="2026-08-30T09:00:00",
        )
        check_true("un compte Google réel reste masqué en démonstration",
                   "julien.perso" not in client.get("/").text)

        # SMTP alone is enough too.
        google_mail.load_token = lambda path=None: google_mail.GoogleToken()
        sending.load_settings = lambda path=None: sending.SmtpSettings(
            sender="g@example.com", host="smtp.example.com", password="abcdefghijkl"
        )
        check_true("une configuration SMTP réelle reste masquée aussi",
                   "smtp.example.com" not in client.get("/").text)

        # And the settings page is reachable from where the hint points.
        check("la page de réglages répond",
              client.get("/parametres/envoi").status_code, 200)
    finally:
        sending.load_settings = original_settings
        google_mail.load_token = original_token
        app_db.DB_PATH = original_path
        shutil.rmtree(folder, ignore_errors=True)



def test_missing_route_always_says_why() -> None:
    section("Une voie d’envoi absente est toujours expliquée")

    import app.server as srv

    class Row(dict):
        def __getitem__(self, key):
            return dict.get(self, key)

    postal = srv._routes_for(Row(channel="courrier", email="client@example.com"))
    check_true("une mise en demeure ne part pas par e-mail", not postal["by_email"])
    check_true("… et la raison est donnée", bool(postal["reason"]))
    check_true("… en nommant la lettre recommandée",
               "recommandée" in postal["reason"])
    check_true("… et en disant quoi faire", "Imprimez" in postal["reason"])
    check_true("… elle reste imprimable", postal["can_print"])

    for empty in ("", "   ", None):
        no_mail = srv._routes_for(Row(channel="email", email=empty))
        check_true("sans adresse e-mail, aucune voie électronique",
                   not no_mail["by_email"])
        check_true("… et la raison est donnée", bool(no_mail["reason"]))
    check_true("… qui parle bien de l’adresse manquante",
               "adresse e-mail" in srv._routes_for(
                   Row(channel="email", email="")
               )["reason"])
    check_true("… et propose le courrier",
               "courrier" in srv._routes_for(
                   Row(channel="email", email="")
               )["reason"])

    usual = srv._routes_for(Row(channel="email", email="client@example.com"))
    check_true("un cas normal garde ses voies", usual["by_email"])
    check("… et n’affiche aucune explication inutile", usual["reason"], None)


def test_no_recipient_is_never_sent_even_in_demo() -> None:
    section("Sans destinataire, rien n’est envoyé — pas même un aperçu")

    from fastapi.testclient import TestClient

    from app import db as app_db
    import app.server as srv

    folder = tempfile.mkdtemp()
    demo_db = Path(folder) / "demo.db"
    original_path = app_db.DB_PATH

    try:
        app_db.DB_PATH = demo_db
        conn = app_db.connect(demo_db)
        reference = date(2026, 8, 31)
        seed.generate(conn, reference)
        followup_module.create_drafts(conn, reference)

        row = [
            item for item in followup_module.queue(conn, "pending")
            if item["channel"] == "email"
        ][0]
        followup_id = row["id"]
        customer = conn.execute(
            "SELECT customer_id FROM invoices WHERE id = ?", (row["invoice_id"],)
        ).fetchone()["customer_id"]
        conn.execute("UPDATE customers SET email = '' WHERE id = ?", (customer,))
        conn.commit()
        before = kpi.last_30_days(conn, reference).followups_sent
        conn.close()

        client = TestClient(srv.app)
        client.post(f"/api/followups/{followup_id}/validate?contexte=demo")
        answer = client.post(f"/api/followups/{followup_id}/envoyer?contexte=demo")

        check("l’envoi est refusé", answer.status_code, 400)
        check_true("… avec la raison, pas un bouton manquant",
                   "adresse e-mail" in answer.json()["detail"])

        conn = app_db.connect(demo_db)
        try:
            stored = conn.execute(
                "SELECT status, sent_at FROM followups WHERE id = ?", (followup_id,)
            ).fetchone()
            check("la relance reste validée", stored["status"], "validated")
            check("… et non envoyée", stored["sent_at"], None)
            check("… et le compteur ne bouge pas",
                  kpi.last_30_days(conn, reference).followups_sent, before)
        finally:
            conn.close()

        # The .eml route refuses for the same reason, not with a blank file.
        download = client.get(f"/relances/{followup_id}/message.eml?contexte=demo")
        check("le téléchargement .eml est refusé aussi", download.status_code, 400)
        check_true("… en expliquant pourquoi",
                   "adresse e-mail" in download.text)
    finally:
        app_db.DB_PATH = original_path
        shutil.rmtree(folder, ignore_errors=True)


def test_window_survives_the_first_of_the_month() -> None:
    section("Le 1er du mois, les compteurs ne se vident pas")

    # The case that broke: on 1 September a calendar month showed
    # "0 relance · 0,00 €" — days after the tool had recovered real money.
    # Nothing about the business changed at midnight; the counter did.
    folder = tempfile.mkdtemp()
    try:
        conn = db_module.connect(Path(folder) / "demo.db")
        first = date(2026, 9, 1)
        check("le cas testé est bien un 1er du mois", first.day, 1)

        seed.generate(conn, first)
        followup_module.create_drafts(conn, first)

        window = kpi.last_30_days(conn, first)

        check_true("des relances sont comptées", window.followups_sent > 0)
        check_true("de l’argent encaissé est compté", window.recovered_cents > 0)
        check_true("… sur des factures réellement réglées",
                   window.recovered_count > 0)
        check_true("des brouillons préparés sont comptés",
                   window.drafts_prepared > 0)
        check_true("… donc du temps gagné", window.minutes_saved > 0)

        # The window itself: thirty days ending on the reference, inclusive.
        start, end = kpi.window_bounds(first)
        check("le premier jour compté est le 3 août", start, "2026-08-03")
        check("la borne haute exclut le lendemain", end, "2026-09-02")
        check("l’intitulé est daté pour l’écran",
              window.window_range, "du 03/08/2026 au 01/09/2026")
        check("l’intitulé ne parle pas de mois",
              window.window_label, "sur les 30 derniers jours")

        # A calendar month would have counted the 1st alone. That is the bug.
        month_only = conn.execute(
            "SELECT COUNT(*) FROM followups "
            "WHERE status = 'sent' AND sent_at >= '2026-09-01'"
        ).fetchone()[0]
        check("un mois calendaire aurait affiché zéro", month_only, 0)
        check_true("la fenêtre glissante, elle, montre le travail fait",
                   window.followups_sent > month_only)

        # Same data, one day later: the figures must not fall off a cliff.
        tomorrow = kpi.last_30_days(conn, date(2026, 9, 2))
        check_true("le lendemain ne remet rien à zéro",
                   tomorrow.followups_sent > 0 and tomorrow.recovered_cents > 0)
        conn.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _source_from(text: str):
    """A SourceFile straight from CSV text, no file on disk."""
    return file_reader.read_bytes(text.encode('utf-8'), 'essai.csv')


@contextlib.contextmanager
def _throwaway_ledger(real_db):
    """Point the import at a scratch ledger for the whole block.

    The redirection has to outlive the page render: confirming an import is a
    second request, and a helper that restored the path on the way out would
    write the test's rows straight into the owner's real ledger.
    """
    import app.web_import as web
    import app.data_context as contexts

    original = web.REAL_DB_PATH
    original_real = contexts.REAL
    web.REAL_DB_PATH = real_db
    contexts.REAL = contexts.DataContext(
        key="real", label="Données réelles", description="Test",
        path=real_db, is_demo=False,
    )
    try:
        yield
    finally:
        web.REAL_DB_PATH = original
        contexts.REAL = original_real


def _summary_page(csv_text: str, term: str = "30"):
    """Drive the import flow inside a `_throwaway_ledger` block."""
    from fastapi.testclient import TestClient

    from app.server import app as fastapi_app
    import app.data_context as contexts

    client = TestClient(fastapi_app)
    client.cookies.set(contexts.COOKIE_NAME, contexts.cookie_value("real"))
    answer = client.post(
        "/import/analyser",
        files={"fichier": ("essai.csv", csv_text.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    token = answer.headers["location"].rsplit("/", 1)[-1]
    client.post(
        f"/import/{token}/mapping",
        data={
            "champ_number": "Numero", "champ_customer_name": "Client",
            "champ_email": "Email", "champ_issue_date": "Date",
            "champ_due_date": "Echeance", "champ_total_ttc": "Total TTC",
            "delai": term,
        },
        follow_redirects=False,
    )
    page = client.get(f"/import/{token}/recapitulatif").text
    return page, client, token


# Three good rows, one duplicate number, one unreadable total, one missing
# due date, one customer with no e-mail: the exact shape of the screen.
_TRIAL_CSV = """Numero;Client;Email;Date;Echeance;Total TTC
F-201;Nathalie Dupre;nathalie@exemple.fr;01/07/2026;31/07/2026;1234,56
F-202;SARL Beton Leger;beton@exemple.fr;05/07/2026;;890,00
F-203;Garage Freres Munoz;;09/07/2026;08/08/2026;2145,80
F-201;Nathalie Dupre;nathalie@exemple.fr;12/07/2026;11/08/2026;300,00
F-204;Client Sans Total;x@exemple.fr;15/07/2026;14/08/2026;
"""


def test_summary_has_one_thing_to_read() -> None:
    section("Le récapitulatif d’import n’a qu’une chose à lire")

    import re

    from app.config import REAL_DB_PATH

    folder = tempfile.mkdtemp()
    untouched = REAL_DB_PATH.read_bytes() if REAL_DB_PATH.exists() else None
    try:
        with _throwaway_ledger(Path(folder) / "essai.db"):
            page, client, token = _summary_page(_TRIAL_CSV)

            # --- what is on screen before any click --------------------------
            verdict = page.split('<div class="verdict">', 1)[1].split("</div>", 1)[0]
            lines = [
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", block)).strip()
                for block in re.findall(r"<p[^>]*>(.*?)</p>", verdict, re.S)
            ]
            check("trois lignes, pas une de plus", len(lines), 3)
            check("ce qui sera ajouté", lines[0], "3 factures vont être ajoutées.")
            check("ce qui est refusé", lines[1],
                  "2 lignes ont un problème et ne seront pas importées.")
            check("ce qui a été deviné", lines[2],
                  "1 échéance a été calculée (absente du fichier).")

            # --- the two exceptions, one line each, outside every fold -------
            before_folds = page.split("<details", 1)[0]
            acts = re.findall(r'<p class="act">(.*?)</p>', before_folds, re.S)
            check("deux exceptions restent visibles", len(acts), 2)
            check_true("… le délai deviné est nommé", "30 jours" in acts[0])
            check_true("… et le client sans e-mail aussi",
                       "Garage Freres Munoz" in acts[1])
            check_true("… en disant que ce sera du courrier", "courrier" in acts[1])
            for act in acts:
                plain = re.sub(r"<[^>]+>", "", act)
                check_true("… une phrase, pas un paragraphe", plain.count(".") <= 1)

            # --- everything else is behind the fold --------------------------
            check_true("le bouton de confirmation ne compte plus les factures",
                       "Confirmer l’import</button>" in before_folds)
            for hidden, label in (
                ("Lignes écartées", "la liste des lignes écartées"),
                ("Nouveaux clients", "la liste des nouveaux clients"),
                ("telles qu’elles seront comprises", "l’aperçu des cinq lignes"),
            ):
                check_true(f"{label} n’est pas dans la vue par défaut",
                           hidden not in before_folds)
                check_true(f"… mais {label} est bien sur la page", hidden in page)

            # --- the preview promises only what will be written --------------
            preview = page.split("telles qu’elles seront comprises", 1)[1]
            shown = re.findall(r"<td><b>(F-\d+)</b></td>", preview)
            check("l’aperçu ne montre que des lignes écrites", shown,
                  ["F-201", "F-202", "F-203"])
            check("… la ligne en double n’y figure pas deux fois",
                  shown.count("F-201"), 1)
            check_true("… ni la ligne illisible", "F-204" not in preview)

            # --- read once, not every week -----------------------------------
            check_true("au premier import, la garantie est dépliée",
                       re.search(r'<details class="fold" open>\s*<summary>Ce que l’import',
                                 page) is not None)

            client.post(f"/import/{token}/confirmer")

            page2, _, _ = _summary_page(_TRIAL_CSV)
            check_true("au second import, elle est repliée",
                       re.search(r'<details class="fold">\s*<summary>Ce que l’import',
                                 page2) is not None)
            check_true("… mais toujours présente",
                       "Ce que l’import ne fait jamais" in page2)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    # An import test that writes into the owner's own ledger is a bug in the
    # test and a lost file for him.
    if untouched is not None:
        check("le vrai grand livre n’a pas été touché",
              REAL_DB_PATH.read_bytes(), untouched)


def test_a_name_is_the_customer_not_the_address() -> None:
    section("Le nom fait le client, pas l’adresse e-mail")

    from app.importer import reconcile as rc

    # Two different names on one address, both new in the same file. The name
    # is the identity: they stay two customers. Merging them would send a
    # follow-up under the wrong name for someone else's debt — and that
    # customer reads it. A duplicate record, if it is one, nobody ever sees.
    rows = runner.parse_rows(
        _source_from(
            "Numero;Client;Email;Date;Echeance;Total TTC\n"
            "F-301;Nathalie Dupre;partage@exemple.fr;01/07/2026;31/07/2026;100,00\n"
            "F-302;SARL Beton Leger;partage@exemple.fr;02/07/2026;01/08/2026;200,00\n"
            "F-303;Garage Munoz;autre@exemple.fr;03/07/2026;02/08/2026;300,00\n"
        ),
        {"number": "Numero", "customer_name": "Client", "email": "Email",
         "issue_date": "Date", "due_date": "Echeance", "total_ttc": "Total TTC"},
        date(2026, 9, 2), 30,
    )
    plan = rc.reconcile(rows, rc.ExistingState({}, {}, {}, {}, {}))

    check("les trois factures sont écrites", len(plan.inserts), 3)
    check("les trois noms donnent trois clients", len(plan.new_customers), 3)
    check("aucun nom n’est absorbé par un autre",
          len(plan.new_customers), len({
              rc.normalise_name(row.customer_name) for row in plan.inserts
          }))
    check("aucun client existant n’a été retrouvé", plan.matched_customers, 0)
    check_true("le nom qui disparaissait est bien créé",
               "SARL Beton Leger" in plan.new_customers)

    # The shared address is reported, without changing who is who.
    check("l’adresse partagée est signalée", len(plan.shared_emails), 1)
    shared = plan.shared_emails[0]
    check("… sur la facture concernée", shared["number"], "F-302")
    check("… en nommant les deux clients",
          {shared["name"], shared["other_name"]},
          {"SARL Beton Leger", "Nathalie Dupre"})
    check("… et l’adresse en cause", shared["email"], "partage@exemple.fr")

    flagged = [row for row in plan.inserts if row.number == "F-302"][0]
    warning = " ".join(flagged.warnings)
    check_true("la ligne porte l’avertissement", "partagent" in warning)
    check_true("… qui dit ce qui a été fait",
               "traités comme deux clients distincts" in warning)
    check_true("… et comment corriger", "corrigez l’adresse" in warning)
    check_true("… sans jamais prétendre que l’e-mail fait foi",
               "fait foi" not in warning)

    # And the same rule against the database: a known address does not make a
    # new name into that customer.
    existing = rc.ExistingState(
        invoices={},
        by_match_key={rc.normalise_name("Nathalie Dupre"): 1},
        by_email={"partage@exemple.fr": 1},
        customer_names={1: "Nathalie Dupre"},
        open_followups={},
        customer_emails={1: "partage@exemple.fr"},
    )
    rows = runner.parse_rows(
        _source_from(
            "Numero;Client;Email;Date;Echeance;Total TTC\n"
            "F-501;Auto-Ecole Leger;partage@exemple.fr;01/07/2026;31/07/2026;100,00\n"
        ),
        {"number": "Numero", "customer_name": "Client", "email": "Email",
         "issue_date": "Date", "due_date": "Echeance", "total_ttc": "Total TTC"},
        date(2026, 9, 2), 30,
    )
    plan = rc.reconcile(rows, existing)
    check("face à la base, un nom nouveau reste un client nouveau",
          plan.new_customers, ["Auto-Ecole Leger"])
    check("… il n’est pas rattaché au client connu", plan.matched_customers, 0)
    check("… mais l’adresse commune est signalée", len(plan.shared_emails), 1)
    check("… en nommant le client déjà en base",
          plan.shared_emails[0]["other_name"], "Nathalie Dupre")

    # A name that only differs in spelling is still the same customer: the
    # normalisation absorbs case, accents, punctuation, legal form and order.
    rows = runner.parse_rows(
        _source_from(
            "Numero;Client;Email;Date;Echeance;Total TTC\n"
            "F-601;NATHALIE DUPRÉ;autre@exemple.fr;01/07/2026;31/07/2026;100,00\n"
        ),
        {"number": "Numero", "customer_name": "Client", "email": "Email",
         "issue_date": "Date", "due_date": "Echeance", "total_ttc": "Total TTC"},
        date(2026, 9, 2), 30,
    )
    plan = rc.reconcile(rows, existing)
    check("une simple variante d’écriture reste le même client",
          plan.matched_customers, 1)
    check("… et ne crée personne", plan.new_customers, [])

    # A customer with no address is named, and only when actually written.
    rows = runner.parse_rows(
        _source_from(
            "Numero;Client;Email;Date;Echeance;Total TTC\n"
            "F-401;Sans Adresse;;01/07/2026;31/07/2026;100,00\n"
            "F-402;Ecarte Sans Total;;02/07/2026;01/08/2026;\n"
        ),
        {"number": "Numero", "customer_name": "Client", "email": "Email",
         "issue_date": "Date", "due_date": "Echeance", "total_ttc": "Total TTC"},
        date(2026, 9, 2), 30,
    )
    plan = rc.reconcile(rows, rc.ExistingState({}, {}, {}, {}, {}))
    check("le client sans e-mail est nommé", plan.customers_without_email,
          ["Sans Adresse"])
    check_true("… et celui d’une ligne écartée ne l’est pas",
               "Ecarte Sans Total" not in plan.customers_without_email)


def test_recognition_is_a_result() -> None:
    section("Réimporter le même fichier dit ce qui a été reconnu")

    import re

    from app.config import REAL_DB_PATH

    folder = tempfile.mkdtemp()
    untouched = REAL_DB_PATH.read_bytes() if REAL_DB_PATH.exists() else None
    try:
        with _throwaway_ledger(Path(folder) / "essai.db"):
            page, client, token = _summary_page(_TRIAL_CSV)
            client.post(f"/import/{token}/confirmer")

            # The same file, a second time: the weekly case for a garage.
            page2, client2, token2 = _summary_page(_TRIAL_CSV)

            verdict = page2.split('<div class="verdict">', 1)[1].split("</div>", 1)[0]
            lines = [
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", block)).strip()
                for block in re.findall(r"<p[^>]*>(.*?)</p>", verdict, re.S)
            ]
            check("ce qui a été reconnu est dit en premier", lines[0],
                  "3 factures déjà connues, inchangées.")
            check("… avant l’absence d’ajout", lines[1],
                  "Aucune facture ne sera ajoutée.")
            check_true("… et le reste suit", lines[2].startswith("2 lignes"))

            # The failure this fixes: without the first line, a file that was
            # fully recognised and one that was not read at all print the same
            # three lines.
            check_true("« vu, rien à changer » ne se lit plus comme « rien vu »",
                       "déjà connue" in verdict)

            # The rule itself is on the page, not left to be inferred.
            check_true("la règle des mises à jour est écrite",
                       "n’est comptée comme <b>mise à jour</b> que si son" in page2)
            check_true("… et dit que réimporter le même fichier ne change rien",
                       "c’est le résultat normal, pas" in page2)
            check_true("les numéros reconnus sont listés dans le détail",
                       all(number in page2.split("Factures déjà connues", 1)[1]
                           for number in ("F-201", "F-202", "F-203")))

            # Confirming says the same thing: "0 mise(s) à jour" is gone.
            done = client2.post(f"/import/{token2}/confirmer").text
            banner = re.sub(
                r"\s+", " ",
                re.sub(r"<[^>]+>", "",
                       done.split('<div class="note ok">', 1)[1]
                           .split("</div>", 1)[0])).strip()
            check("la confirmation nomme les factures reconnues", banner,
                  "Import vérifié. 3 factures déjà connues, aucune "
                  "modification. 2 lignes n’ont pas été importées. Aucune "
                  "écriture n’était nécessaire : les factures reconnues sont "
                  "restées inchangées.")
            check_true("… et ne dit plus « 0 mise(s) à jour »",
                       "0 mise(s) à jour" not in banner)
            check_true("… ni ne prétend avoir ajouté quoi que ce soit",
                       "facture ajoutée" not in banner)
            check_true("… et n’annonce pas une écriture qui n’a pas eu lieu",
                       "Aucune écriture n’était nécessaire" in banner)
            check_true("… en disant pourquoi",
                       "restées inchangées" in banner)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    if untouched is not None:
        check("le vrai grand livre n’a pas été touché",
              REAL_DB_PATH.read_bytes(), untouched)


def test_an_update_needs_something_to_update() -> None:
    section("Une mise à jour sans changement n’est pas une mise à jour")

    from app.importer import reconcile as rc

    header = "Numero;Client;Email;Date;Echeance;Total TTC;Regle\n"
    mapping = {"number": "Numero", "customer_name": "Client", "email": "Email",
               "issue_date": "Date", "due_date": "Echeance",
               "total_ttc": "Total TTC", "amount_paid": "Regle"}

    folder = tempfile.mkdtemp()
    try:
        conn = db_module.connect(Path(folder) / "essai.db")
        db_module.init_schema(conn)

        first = (header +
                 "F-701;Client Test;c@exemple.fr;01/07/2026;31/07/2026;100,00;0,00\n")
        rows = runner.parse_rows(
            _source_from(first), mapping, date(2026, 9, 2), 30)
        plan = rc.reconcile(rows, rc.load_existing(conn))
        check("le premier import ajoute", len(plan.inserts), 1)
        check("… et ne reconnaît rien", len(plan.unchanged), 0)
        runner.apply(conn, plan)

        # Byte-identical file: nothing has moved, so nothing is an update.
        rows = runner.parse_rows(
            _source_from(first), mapping, date(2026, 9, 2), 30)
        same = rc.reconcile(rows, rc.load_existing(conn))
        check("le même fichier n’ajoute rien", len(same.inserts), 0)
        check("… ne met rien à jour", len(same.updates), 0)
        check("… et reconnaît la facture", len(same.unchanged), 1)
        check("… en la nommant", same.unchanged[0].number, "F-701")
        check_true("le résumé le dit aussi",
                   "1 déjà connue(s), inchangée(s)" in same.headline)
        result = runner.apply(conn, same)
        check("rien n’est écrit", (result.inserted, result.updated), (0, 0))
        check("… et la reconnaissance est comptée", result.unchanged, 1)

        # A payment arrives: that, and only that, is an update.
        paid = (header +
                "F-701;Client Test;c@exemple.fr;01/07/2026;31/07/2026;100,00;100,00\n")
        rows = runner.parse_rows(
            _source_from(paid), mapping, date(2026, 9, 2), 30)
        moved = rc.reconcile(rows, rc.load_existing(conn))
        check("un règlement, lui, est une mise à jour", len(moved.updates), 1)
        check("… et n’est pas compté comme inchangé", len(moved.unchanged), 0)
        check_true("… et il est signalé comme réglé", moved.settled_count == 1)
        runner.apply(conn, moved)

        # A changed total is not an update either: it is flagged, untouched.
        other = (header +
                 "F-701;Client Test;c@exemple.fr;01/07/2026;31/07/2026;150,00;100,00\n")
        rows = runner.parse_rows(
            _source_from(other), mapping, date(2026, 9, 2), 30)
        changed = rc.reconcile(rows, rc.load_existing(conn))
        check("un total différent n’est pas une mise à jour",
              len(changed.updates), 0)
        check("… c’est un signalement", len(changed.flagged), 1)
        check("… et pas davantage un « inchangé »", len(changed.unchanged), 0)
        conn.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main() -> int:
    print("═" * 78)
    print("  CONTRÔLES DE CALCUL — tout est calculé en Python, jamais par le modèle")
    print("═" * 78)

    test_levels()
    test_money()
    test_penalties()
    test_workflow()
    test_ledger()
    test_determinism()
    test_guardrail()
    test_rejected_never_queued()
    test_import_amounts()
    test_import_convention()
    test_import_dates()
    test_import_files()
    test_import_touches_nothing()
    test_import_mapping()
    test_import_profiles()
    test_import_never_touches_demo()
    test_reconcile_names()
    test_reconcile_second_import()
    test_reconcile_total_change()
    test_reconcile_leaves_demo_alone()
    test_guessed_kind_claims_nothing()
    test_imported_customer_is_a_guess()
    test_payment_term_is_asked()
    test_eml_handover()
    test_gmail_threshold()
    test_stale_drafts()
    test_second_handover_is_flagged()
    test_both_handover_routes_agree()
    test_demo_mode_blocks_every_send()
    test_demo_previews_instead_of_hiding()
    test_only_the_send_scope_exists()
    test_expired_connection_leaves_the_followup_alone()
    test_google_demo_mode_opens_nothing()
    test_google_token_stays_out_of_the_data()
    test_account_mismatch_warns_without_blocking()
    test_mismatch_never_blocks_sending()
    test_setup_hint_appears_only_when_nothing_is_connected()
    test_window_survives_the_first_of_the_month()
    test_summary_has_one_thing_to_read()
    test_a_name_is_the_customer_not_the_address()
    test_recognition_is_a_result()
    test_an_update_needs_something_to_update()
    test_missing_route_always_says_why()
    test_no_recipient_is_never_sent_even_in_demo()
    test_sending_failures_are_actionable()
    test_sending_settings_stay_out_of_the_data()

    print()
    print("═" * 78)
    if FAILURES:
        print(f"  {len(FAILURES)} ÉCHEC(S) sur {PASSED + len(FAILURES)} contrôles")
        print("═" * 78)
        for failure in FAILURES:
            print(f"\n  ✗ {failure}")
        print()
        return 1

    print(f"  {PASSED} contrôles passés — aucun écart d’un centime")
    print("═" * 78)
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main())
