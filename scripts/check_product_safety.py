"""Focused integration checks for the demo/real and sending safety barriers.

This complements ``check_logic.py``.  It only uses temporary ledgers and mocks
every external send; running it can never contact a customer.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app import db, followups, google_mail, overdue, seed, sending
from app import data_context as contexts
from app import demo_reset as demo_reset_module
from app import server
from app import web_import
from app.email_address import is_valid as valid_email
from app.importer import reader, reconcile, runner


PASSED = 0
FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILURES.append(label)


def equal(label: str, actual, expected) -> None:
    check(f"{label} (attendu {expected!r}, obtenu {actual!r})", actual == expected)


def section(title: str) -> None:
    print(f"\n  {title}\n  " + "-" * 72)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def isolated_application(demo_path: Path, real_path: Path, *, preview: bool = True):
    """Point every request-scoped selector at temporary, distinct ledgers."""
    originals = {
        "context_demo": contexts.DEMO,
        "context_real": contexts.REAL,
        "context_preview": contexts.DEMO_MODE,
        "server_demo": server.DEMO,
        "server_real": server.REAL,
        "server_preview": server.DEMO_MODE,
        "db_path": db.DB_PATH,
        "web_real": web_import.REAL_DB_PATH,
        "reset_demo": demo_reset_module.DB_PATH,
        "reset_real": demo_reset_module.REAL_DB_PATH,
    }
    demo = contexts.DataContext(
        key="demo", label="Mode démonstration",
        description="Données fictives · aucun envoi réel",
        path=demo_path, is_demo=True,
    )
    real = contexts.DataContext(
        key="real", label="Données réelles",
        description=("Envoi désactivé pendant la pré-production"
                     if preview else "Envoi possible après validation"),
        path=real_path, is_demo=False,
    )
    contexts.DEMO, contexts.REAL, contexts.DEMO_MODE = demo, real, preview
    server.DEMO, server.REAL, server.DEMO_MODE = demo, real, preview
    db.DB_PATH = demo_path
    web_import.REAL_DB_PATH = real_path
    demo_reset_module.DB_PATH = demo_path
    demo_reset_module.REAL_DB_PATH = real_path
    web_import._HELD.clear()
    web_import._COMPLETED.clear()
    try:
        yield demo, real
    finally:
        contexts.DEMO = originals["context_demo"]
        contexts.REAL = originals["context_real"]
        contexts.DEMO_MODE = originals["context_preview"]
        server.DEMO = originals["server_demo"]
        server.REAL = originals["server_real"]
        server.DEMO_MODE = originals["server_preview"]
        db.DB_PATH = originals["db_path"]
        web_import.REAL_DB_PATH = originals["web_real"]
        demo_reset_module.DB_PATH = originals["reset_demo"]
        demo_reset_module.REAL_DB_PATH = originals["reset_real"]
        web_import._HELD.clear()
        web_import._COMPLETED.clear()


def real_client() -> TestClient:
    client = TestClient(server.app)
    client.cookies.set(contexts.COOKIE_NAME, contexts.cookie_value("real"))
    return client


def pending_email_followup(path: Path, reference: date | None = None) -> int:
    reference = reference or date.today()
    conn = db.connect(path)
    try:
        seed.generate(conn, reference)
        followups.create_drafts(conn, reference, mode="mock")
        return next(
            row["id"] for row in followups.queue(conn, "pending")
            if row["channel"] == "email"
        )
    finally:
        conn.close()


def test_context_and_headers(folder: Path) -> None:
    section("Contexte visible, démo par défaut et requêtes locales")
    demo_path, real_path = folder / "demo.db", folder / "real.db"
    with isolated_application(demo_path, real_path):
        pending_email_followup(demo_path)
        client = TestClient(server.app)

        page = client.get("/")
        equal("la page d’accueil répond", page.status_code, 200)
        check("le démarrage est en démonstration", "Mode démonstration" in page.text)
        check("les données sont explicitement fictives", "Données fictives" in page.text)
        check("les réponses ne sont pas mises en cache",
              page.headers.get("cache-control") == "no-store")
        check("l’application ne peut pas être intégrée dans un cadre",
              page.headers.get("x-frame-options") == "DENY")

        stale = TestClient(server.app)
        stale.cookies.set(contexts.COOKIE_NAME, "real.ancienne-session")
        check("un ancien choix Réel retombe en Démo au redémarrage",
              "Mode démonstration" in stale.get("/").text)

        switched = client.post(
            "/mode", data={"mode": "real", "next": "/"},
            follow_redirects=False,
        )
        equal("le changement de contexte utilise un PRG", switched.status_code, 303)
        real_page = client.get("/")
        check("le mode réel est écrit en toutes lettres", "Données réelles" in real_page.text)
        check("la base réelle vide ne montre pas les factures de démo",
              "Aucune facture pour le moment" in real_page.text)

        client.post("/mode", data={"mode": "demo", "next": "/"})
        check("le retour en démo retrouve le jeu fictif",
              "Reste à encaisser" in client.get("/").text)

        # Exact stale-tab regression: a Demo page still holds a numeric id,
        # while another tab changes the shared cookie to Real.
        real_id = pending_email_followup(real_path)
        client.post("/mode", data={"mode": "real", "next": "/"})
        stale_action = client.post(
            f"/api/followups/{real_id}/validate?contexte=demo"
        )
        equal("un bouton d’un ancien onglet Démo est refusé en mode Réel",
              stale_action.status_code, 409)
        conn = db.connect(real_path)
        try:
            equal("la relance réelle de même identifiant reste intacte",
                  conn.execute(
                      "SELECT status FROM followups WHERE id=?", (real_id,)
                  ).fetchone()[0], "pending")
        finally:
            conn.close()
        client.post("/mode", data={"mode": "demo", "next": "/"})

        refused = client.post(
            "/mode", data={"mode": "real", "next": "/"},
            headers={"Origin": "https://site-inconnu.example"},
        )
        equal("une mutation venant d’un autre site est refusée", refused.status_code, 403)
        equal("une redirection externe est neutralisée",
              contexts.safe_return_path("//site-inconnu.example"), "/")


def test_demo_reset_isolation(folder: Path) -> None:
    section("Remise à zéro atomique de la démonstration")
    demo_path, real_path = folder / "reset-demo.db", folder / "reset-real.db"
    real_path.write_bytes(b"DONNEES-REELLES-A-NE-PAS-MODIFIER")
    before = sha256(real_path)
    with isolated_application(demo_path, real_path):
        result = demo_reset_module.reset_demo(date(2026, 9, 3))
        check("la base de démonstration est créée", demo_path.exists())
        check("son jeu est cohérent", not result.problems)
        equal("la base réelle reste bit à bit identique", sha256(real_path), before)
        conn = db.connect(demo_path)
        try:
            check("la démonstration contient des factures",
                  conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] > 0)
        finally:
            conn.close()

        old_demo, old_real = demo_reset_module.DB_PATH, demo_reset_module.REAL_DB_PATH
        try:
            demo_reset_module.DB_PATH = real_path
            demo_reset_module.REAL_DB_PATH = real_path
            try:
                demo_reset_module.reset_demo(date(2026, 9, 3))
            except RuntimeError:
                refused = True
            else:
                refused = False
            check("des chemins identiques font échouer la remise à zéro", refused)
            equal("le refus ne touche pas au fichier réel", sha256(real_path), before)
        finally:
            demo_reset_module.DB_PATH, demo_reset_module.REAL_DB_PATH = old_demo, old_real


def mapped_rows(csv_text: str, mapping: dict[str, str]):
    source = reader.read_bytes(csv_text.encode("utf-8"), "controle.csv")
    return runner.parse_rows(source, mapping, date(2026, 9, 3), 30)


BASE_MAPPING = {
    "number": "Numero", "customer_name": "Client", "email": "Email",
    "issue_date": "Date", "due_date": "Echeance", "total_ttc": "Total TTC",
}


def test_identity_email_and_vehicle(folder: Path) -> None:
    section("Identité client, destinataire unique et facture sans véhicule")
    check("une adresse simple est acceptée", valid_email("client+compta@example.fr"))
    for address in (
        "a@example.fr,b@example.fr", "a@example.fr; b@example.fr",
        "a@example.fr\r\nBcc: tiers@example.fr", "sans-arobase",
    ):
        check(f"l’adresse dangereuse est refusée : {address!r}", not valid_email(address))

    invalid = mapped_rows(
        "Numero;Client;Email;Date;Echeance;Total TTC\n"
        "MAIL-1;Client Test;a@example.fr,b@example.fr;01/07/2026;30/07/2026;100,00\n",
        BASE_MAPPING,
    )[0]
    equal("une adresse multiple n’est pas enregistrée", invalid.email, None)
    check("l’utilisateur reçoit une explication",
          any("plusieurs destinataires" in warning for warning in invalid.warnings))

    ledger = folder / "identity.db"
    conn = db.connect(ledger)
    try:
        db.init_schema(conn)
        first = mapped_rows(
            "Numero;Client;Email;Date;Echeance;Total TTC\n"
            "SANS-VEH;Nathalie Dupré;partage@example.fr;01/07/2026;30/07/2026;100,00\n"
            "AUTRE-1;SARL Béton Léger;partage@example.fr;02/07/2026;31/07/2026;200,00\n",
            BASE_MAPPING,
        )
        plan = reconcile.reconcile(first, reconcile.load_existing(conn))
        equal("deux noms distincts restent deux clients", len(plan.new_customers), 2)
        equal("l’adresse partagée est signalée", len(plan.shared_emails), 1)
        result = runner.apply(conn, plan)
        equal("les deux factures sont ajoutées", result.inserted, 2)
        equal("les deux clients sont réellement créés", result.customers_created, 2)
        equal("l’absence de véhicule est conservée",
              conn.execute("SELECT vehicle_id FROM invoices WHERE number='SANS-VEH'").fetchone()[0],
              None)

        followups.create_drafts(conn, date(2026, 9, 3), mode="mock")
        check("une facture sans véhicule entre bien dans les relances",
              any(row["number"] == "SANS-VEH" for row in followups.queue(conn, "pending")))

        changed_customer = mapped_rows(
            "Numero;Client;Email;Date;Echeance;Total TTC;Regle\n"
            "SANS-VEH;Société Sans Rapport;autre@example.fr;01/07/2026;30/07/2026;100,00;100,00\n",
            {**BASE_MAPPING, "amount_paid": "Regle"},
        )
        mismatch = reconcile.reconcile(changed_customer, reconcile.load_existing(conn))
        equal("un numéro connu avec un autre client est bloqué", len(mismatch.flagged), 1)
        equal("aucune fausse mise à jour n’est prévue", len(mismatch.updates), 0)
        runner.apply(conn, mismatch)
        equal("le paiement n’est pas appliqué au mauvais client",
              conn.execute("SELECT amount_paid_cents FROM invoices WHERE number='SANS-VEH'").fetchone()[0],
              0)
    finally:
        conn.close()


def test_transitions_and_real_preview(folder: Path) -> None:
    section("Transitions contrôlées et pré-production honnête")
    demo_path, real_path = folder / "flow-demo.db", folder / "flow-real.db"
    with isolated_application(demo_path, real_path, preview=True):
        demo_id = pending_email_followup(demo_path)
        demo = TestClient(server.app)
        equal("une relance en attente ne peut pas être déclarée envoyée",
              demo.post(f"/api/followups/{demo_id}/envoye?contexte=demo").status_code, 409)
        equal("une relance en attente ne peut pas être imprimée",
              demo.get(f"/relances/{demo_id}/imprimer?contexte=demo").status_code, 409)
        equal("la validation réussit", demo.post(
            f"/api/followups/{demo_id}/validate?contexte=demo").status_code, 200)
        equal("validée mais non remise, elle n’est toujours pas envoyée",
              demo.post(f"/api/followups/{demo_id}/envoye?contexte=demo").status_code, 409)
        equal("la remise est enregistrée",
              demo.post(f"/api/followups/{demo_id}/remis?contexte=demo").status_code, 200)
        equal("la confirmation finale est alors permise",
              demo.post(f"/api/followups/{demo_id}/envoye?contexte=demo").status_code, 200)

        real_id = pending_email_followup(real_path)
        client = real_client()
        client.post(f"/api/followups/{real_id}/validate?contexte=real")
        original_token = google_mail.load_token
        original_settings = sending.load_settings
        original_send = sending.send
        calls: list[str] = []
        try:
            google_mail.load_token = lambda path=None: google_mail.GoogleToken()
            sending.load_settings = lambda path=None: sending.SmtpSettings()
            sending.send = lambda *args, **kwargs: calls.append("network")
            answer = client.post(f"/api/followups/{real_id}/envoyer?contexte=real")
        finally:
            google_mail.load_token = original_token
            sending.load_settings = original_settings
            sending.send = original_send
        equal("l’aperçu réel de pré-production répond", answer.status_code, 200)
        payload = answer.json()
        check("il est explicitement présenté comme aperçu", payload["preview_only"])
        equal("il ne prétend pas être une démonstration", payload["demo"], False)
        equal("il ne marque pas le message comme envoyé", payload["status"], "validated")
        equal("aucun transport n’est appelé", calls, [])
        conn = db.connect(real_path)
        try:
            row = conn.execute(
                "SELECT status, sent_at FROM followups WHERE id=?", (real_id,)
            ).fetchone()
            equal("la base reste validée", row["status"], "validated")
            equal("aucune date d’envoi n’est inventée", row["sent_at"], None)
        finally:
            conn.close()


def test_concurrent_send(folder: Path) -> None:
    section("Double clic : une seule remise au transport")
    demo_path, real_path = folder / "race-demo.db", folder / "race-real.db"
    with isolated_application(demo_path, real_path, preview=False):
        followup_id = pending_email_followup(real_path)
        client = real_client()
        client.post(f"/api/followups/{followup_id}/validate?contexte=real")

        original_token = google_mail.load_token
        original_settings = sending.load_settings
        original_send = sending.send
        sent: list[str] = []
        lock = threading.Lock()

        def fake_send(_settings, recipient, _subject, _body):
            with lock:
                sent.append(recipient)
            time.sleep(0.2)

        def click() -> int:
            local = real_client()
            return local.post(
                f"/api/followups/{followup_id}/envoyer?contexte=real"
            ).status_code

        try:
            google_mail.load_token = lambda path=None: google_mail.GoogleToken()
            sending.load_settings = lambda path=None: sending.SmtpSettings(
                sender="garage@example.fr", host="smtp.example.fr", password="test"
            )
            sending.send = fake_send
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = sorted(pool.map(lambda _value: click(), (1, 2)))
        finally:
            google_mail.load_token = original_token
            sending.load_settings = original_settings
            sending.send = original_send

        equal("un clic réussit et le doublon est refusé", statuses, [200, 409])
        equal("le transport n’est appelé qu’une fois", len(sent), 1)
        conn = db.connect(real_path)
        try:
            equal("un seul état envoyé est persisté", conn.execute(
                "SELECT status FROM followups WHERE id=?", (followup_id,)
            ).fetchone()[0], "sent")
        finally:
            conn.close()


def test_concurrent_drafting(folder: Path) -> None:
    section("Double préparation : aucun brouillon en double")
    ledger = folder / "draft-race.db"
    conn = db.connect(ledger)
    try:
        seed.generate(conn, date(2026, 9, 3))
    finally:
        conn.close()

    def prepare(_value: int) -> int:
        local = db.connect(ledger)
        try:
            return followups.create_drafts(
                local, date(2026, 9, 3), mode="mock"
            ).total
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(prepare, (1, 2)))
    conn = db.connect(ledger)
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM followups WHERE status IN ('pending','rejected')"
        ).fetchone()[0]
        duplicates = conn.execute(
            "SELECT invoice_id, level, COUNT(*) n FROM followups "
            "WHERE status != 'obsolete' GROUP BY invoice_id, level HAVING n > 1"
        ).fetchall()
    finally:
        conn.close()
    equal("les deux appels ne créent qu’un seul lot", sum(created), active)
    equal("aucune facture/niveau n’est dupliquée", duplicates, [])
    check("le second appel constate le travail déjà fait", 0 in created)


def test_stale_draft_refresh(folder: Path) -> None:
    section("Brouillons datés : une nouvelle journée force un recalcul")
    ledger = folder / "stale-drafts.db"
    first_day, next_day = date(2026, 9, 3), date(2026, 9, 4)
    conn = db.connect(ledger)
    try:
        seed.generate(conn, first_day)
        first_run = followups.create_drafts(conn, first_day, mode="mock")
        check("le premier jour prépare bien un lot", first_run.total > 0)
        old_pending = conn.execute(
            "SELECT COUNT(*) FROM followups WHERE status='pending'"
        ).fetchone()[0]
        equal("les dates stockées suivent la date métier", conn.execute(
            "SELECT COUNT(*) FROM followups WHERE status='pending' "
            "AND substr(created_at,1,10)=?", (first_day.isoformat(),)
        ).fetchone()[0], old_pending)

        equal("les anciens textes ne sont plus proposés à la validation",
              server._queue_rows(conn, next_day), [])
        check("les factures concernées sont proposées au recalcul",
              len(overdue.followups_due(conn, next_day)) > 0)

        second_run = followups.create_drafts(conn, next_day, mode="mock")
        equal("tous les brouillons de la veille sont rendus sans objet",
              conn.execute(
                  "SELECT COUNT(*) FROM followups WHERE status='obsolete' "
                  "AND guardrail_note LIKE 'Brouillon arrivé à expiration%'"
              ).fetchone()[0], old_pending)
        current_pending = conn.execute(
            "SELECT COUNT(*) FROM followups WHERE status='pending' "
            "AND substr(created_at,1,10)=?", (next_day.isoformat(),)
        ).fetchone()[0]
        equal("le nouveau lot est immédiatement visible",
              len(server._queue_rows(conn, next_day)), current_pending)
        equal("le nombre annoncé correspond au lot recalculé",
              second_run.total, current_pending)
    finally:
        conn.close()


def test_changed_due_date_and_paid_draft(folder: Path) -> None:
    section("Échéance corrigée et paiement : brouillons recalculés")
    demo_path, real_path = folder / "date-demo.db", folder / "date-real.db"
    mapping = {**BASE_MAPPING, "amount_paid": "Regle"}
    with isolated_application(demo_path, real_path, preview=True):
        conn = db.connect(real_path)
        try:
            db.init_schema(conn)
            original = mapped_rows(
                "Numero;Client;Email;Date;Echeance;Total TTC;Regle\n"
                "DATE-1;Client Date;date@example.fr;01/07/2026;20/08/2026;100,00;0,00\n",
                mapping,
            )
            runner.apply(conn, reconcile.reconcile(original, reconcile.load_existing(conn)))
            followups.create_drafts(conn, date(2026, 9, 3), mode="mock")
            first = conn.execute(
                "SELECT f.id, f.level FROM followups f JOIN invoices i ON i.id=f.invoice_id "
                "WHERE i.number='DATE-1' AND f.status='pending'"
            ).fetchone()
            equal("l’échéance initiale produit le niveau 2", first["level"], 2)
            conn.execute("UPDATE followups SET status='validated' WHERE id=?", (first["id"],))
            conn.commit()

            corrected = mapped_rows(
                "Numero;Client;Email;Date;Echeance;Total TTC;Regle\n"
                "DATE-1;Client Date;date@example.fr;01/07/2026;30/07/2026;100,00;0,00\n",
                mapping,
            )
            plan = reconcile.reconcile(corrected, reconcile.load_existing(conn))
            equal("une échéance corrigée est une vraie mise à jour", len(plan.updates), 1)
            check("le champ modifié est nommé", "échéance" in plan.updates[0].changed_fields)
            equal("le brouillon déjà validé est prévu à la fermeture",
                  plan.closed_followups[0].was_status, "validated")
            runner.apply(conn, plan)
            followups.create_drafts(conn, date(2026, 9, 3), mode="mock")
            levels = conn.execute(
                "SELECT f.id, f.status, f.level FROM followups f JOIN invoices i ON i.id=f.invoice_id "
                "WHERE i.number='DATE-1' ORDER BY f.id"
            ).fetchall()
            equal("l’ancien niveau 2 est rendu obsolète", levels[0]["status"], "obsolete")
            equal("la nouvelle échéance produit immédiatement le niveau 4",
                  (levels[-1]["status"], levels[-1]["level"]), ("pending", 4))

            conn.execute("UPDATE followups SET status='validated' WHERE id=(SELECT MAX(f.id) FROM followups f JOIN invoices i ON i.id=f.invoice_id WHERE i.number='DATE-1')")
            conn.commit()
            paid = mapped_rows(
                "Numero;Client;Email;Date;Echeance;Total TTC;Regle\n"
                "DATE-1;Client Date;date@example.fr;01/07/2026;30/07/2026;100,00;100,00\n",
                mapping,
            )
            paid_plan = reconcile.reconcile(paid, reconcile.load_existing(conn))
            equal("le paiement ferme aussi une relance validée",
                  paid_plan.closed_followups[0].was_status, "validated")
            runner.apply(conn, paid_plan)
            invoice = conn.execute(
                "SELECT status, paid_date FROM invoices WHERE number='DATE-1'"
            ).fetchone()
            equal("la facture devient réglée", invoice["status"], "paid")
            equal("aucune date de paiement n’est inventée", invoice["paid_date"], None)

            # Simulate a stale status written by another integration: the final
            # dispatch guard must still look at the live invoice balance.
            stale_id = levels[-1]["id"]
            conn.execute("UPDATE followups SET status='validated' WHERE id=?", (stale_id,))
            conn.commit()
        finally:
            conn.close()

        blocked = real_client().post(
            f"/api/followups/{stale_id}/envoyer?contexte=real"
        )
        equal("même un statut périmé ne peut relancer une facture payée",
              blocked.status_code, 409)
        check("le message explique que la facture est réglée",
              "réglée" in blocked.json()["detail"])


def import_once(client: TestClient, csv_text: str) -> tuple[str, str]:
    uploaded = client.post(
        "/import/analyser",
        files={"fichier": ("controle.csv", csv_text.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    equal("le fichier accepté mène à la correspondance", uploaded.status_code, 303)
    token = uploaded.headers["location"].rsplit("/", 1)[-1]
    mapped = client.post(
        f"/import/{token}/mapping",
        data={
            "champ_number": "Numero", "champ_customer_name": "Client",
            "champ_email": "Email", "champ_issue_date": "Date",
            "champ_due_date": "Echeance", "champ_total_ttc": "Total TTC",
            "delai_paiement": "30",
        },
        follow_redirects=False,
    )
    equal("la correspondance mène au récapitulatif", mapped.status_code, 303)
    summary = client.get(f"/import/{token}/recapitulatif").text
    confirmed = client.post(
        f"/import/{token}/confirmer", follow_redirects=False
    )
    equal("la confirmation utilise un PRG stable", confirmed.status_code, 303)
    result = client.get(confirmed.headers["location"]).text
    return summary, result


def test_import_visibility_and_noop(folder: Path) -> None:
    section("Import réel visible, exploitable et idempotent")
    demo_path, real_path = folder / "import-demo.db", folder / "import-real.db"
    csv_text = (
        "Numero;Client;Email;Date;Echeance;Total TTC\n"
        "IMP-VISIBLE-1;Client Import;client.import@example.fr;01/07/2026;30/07/2026;345,67\n"
    )
    with isolated_application(demo_path, real_path, preview=True):
        pending_email_followup(demo_path)
        client = real_client()
        first_summary, first_result = import_once(client, csv_text)
        compact_summary = " ".join(first_summary.split())
        check("le premier récapitulatif annonce une nouvelle facture",
              "facture va être ajoutée" in compact_summary)
        check("le résultat confirme l’ajout", "1 facture ajoutée" in first_result)
        check("la facture apparaît immédiatement dans la liste",
              "IMP-VISIBLE-1" in client.get("/factures").text)
        check("elle apparaît aussi dans le travail de relance",
              "IMP-VISIBLE-1" in client.get("/relances").text)

        conn = db.connect(real_path)
        try:
            equal("une seule facture est persistée",
                  conn.execute("SELECT COUNT(*) FROM invoices WHERE number='IMP-VISIBLE-1'").fetchone()[0], 1)
            equal("elle reste valable sans véhicule",
                  conn.execute("SELECT vehicle_id FROM invoices WHERE number='IMP-VISIBLE-1'").fetchone()[0], None)
        finally:
            conn.close()

        second_summary, second_result = import_once(client, csv_text)
        check("le réimport dit explicitement ce qu’il a reconnu",
              "déjà connue" in second_summary and "inchangée" in second_summary)
        check("le résultat explique qu’aucune écriture n’était nécessaire",
              "Aucune écriture n’était nécessaire" in second_result)
        check("le résultat ne présente pas un faux zéro de mise à jour",
              "0 mise" not in second_result)
        conn = db.connect(real_path)
        try:
            equal("le réimport ne crée aucun doublon",
                  conn.execute("SELECT COUNT(*) FROM invoices WHERE number='IMP-VISIBLE-1'").fetchone()[0], 1)
        finally:
            conn.close()

        demo_page = TestClient(server.app).get("/factures").text
        check("la facture réelle ne fuit jamais dans la démonstration",
              "IMP-VISIBLE-1" not in demo_page)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="relance-product-check-"))
    try:
        test_context_and_headers(root)
        test_demo_reset_isolation(root)
        test_identity_email_and_vehicle(root)
        test_transitions_and_real_preview(root)
        test_concurrent_send(root)
        test_concurrent_drafting(root)
        test_stale_draft_refresh(root)
        test_changed_due_date_and_paid_draft(root)
        test_import_visibility_and_noop(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"  {len(FAILURES)} ÉCHEC(S) sur {PASSED + len(FAILURES)} contrôles")
        for failure in FAILURES:
            print(f"\n  ECHEC: {failure}")
        return 1
    print(f"  {PASSED} contrôles de sécurité produit passés")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
