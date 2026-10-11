"""Owner workflows against temporary ledgers only. Every sender is mocked."""

import re
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app import db, followups, google_mail, manual, overdue, server
from app.config import today
from app.importer import reconcile, runner
from app.mail_demo import reminder_test_message
from check_product_safety import isolated_application, real_client


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="relance-owner-")))
        self.demo, self.real = root / "demo.db", root / "real.db"
        self.stack.enter_context(isolated_application(self.demo, self.real))
        for path in (self.demo, self.real):
            c = db.connect(path)
            db.init_schema(c)
            c.close()
        for target in (google_mail, server.sending):
            self.stack.enter_context(patch.object(target, "send", side_effect=AssertionError("NEVER SEND")))
        self.client = TestClient(server.app)
        self.owner = {"name": "Client Démo Personnel", "email": "owner@example.com", "kind": "particulier", "contexte": "demo"}

    def rows(self, sql, path=None):
        c = db.connect(path or self.demo)
        try:
            return [dict(r) for r in c.execute(sql)]
        finally:
            c.close()

    def add_client(self, client=None, **kwargs):
        response = (client or self.client).post("/clients/nouveau", data={**self.owner, **kwargs}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        return int(response.headers["location"].split("/")[-1].split("?")[0])

    def invoice_form(self, customer_id, **kwargs):
        return {"customer_id": str(customer_id), "number": "DEMO-001", "total": "120,50", "issue_date": (today()-timedelta(days=40)).isoformat(), "due_date": (today()-timedelta(days=34)).isoformat(), "payment_state": "unpaid", "contexte": "demo", **kwargs}

    def add_invoice(self, customer_id=None, client=None, **kwargs):
        customer_id = customer_id or self.add_client()
        r = (client or self.client).post("/factures/nouvelle", data=self.invoice_form(customer_id, **kwargs), follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return int(r.headers["location"].split("/")[-1].split("?")[0])

    def version(self, url, client=None):
        page = (client or self.client).get(url)
        self.assertEqual(page.status_code, 200)
        return re.search(r'name="version" value="([^"]+)"', page.text)[1]

    def pay(self, invoice_id, state, client=None, **kwargs):
        url = f"/factures/{invoice_id}/paiement"
        values = {"contexte": "demo", "payment_state": state, "payment_date": today().isoformat(), "version": self.version(url, client), **kwargs}
        return (client or self.client).post(url, data=values, follow_redirects=False)

    def draft(self):
        c = db.connect(self.demo)
        try:
            return followups.create_drafts(c, today(), mode="mock").queued[0].followup_id
        finally:
            c.close()

    def test_new_client_contact_details_persist(self):
        cid = self.add_client(phone="06 00 00 00 00", address="12 rue du Test", postal_code="75001", city="Paris", contact_name="Sam")
        self.assertIn("12 rue du Test", self.client.get(f"/clients/{cid}").text)
        row = self.rows("SELECT * FROM customers")[0]
        self.assertEqual(row["email"], "owner@example.com")
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["penalty_terms_confirmed"], 0)

    def test_name_only_client_and_empty_states(self):
        self.assertIn("Ajoutez d’abord le client", self.client.get("/factures/nouvelle").text)
        cid = self.add_client(email="")
        self.assertIn("Non renseigné", self.client.get(f"/clients/{cid}").text)

    def test_duplicate_names_not_duplicate_customers(self):
        self.add_client()
        r = self.client.post("/clients/nouveau", data={**self.owner, "name": "  CLIENT demo PERSONNEL "})
        self.assertEqual(r.status_code, 409)
        self.assertIn("existe déjà", r.text)
        self.assertEqual(len(self.rows("SELECT * FROM customers")), 1)

    def test_shared_email_different_names_stay_distinct(self):
        self.add_client(name="Nathalie Dupré")
        self.add_client(name="SARL Béton Léger", kind="professionnel")
        self.assertEqual(len(self.rows("SELECT * FROM customers")), 2)

    def test_invalid_contacts_preserve_input_without_writes(self):
        for changes in ({"name": ""}, {"email": "invalid"}, {"email": "x@y.fr\r\nBcc: z@y.fr"}, {"name": "x"*201}, {"kind": "wrong"}):
            with self.subTest(changes=changes):
                r = self.client.post("/clients/nouveau", data={**self.owner, **changes})
                self.assertEqual(r.status_code, 422)
                self.assertIn("Vérifiez votre saisie", r.text)
        self.assertEqual(self.rows("SELECT * FROM customers"), [])

    def test_invoice_visible_and_correct_34_day_level(self):
        iid = self.add_invoice()
        page = self.client.get(f"/factures/{iid}").text
        self.assertIn("DEMO-001", page)
        c = db.connect(self.demo)
        try:
            item = overdue.load_all(c, today())[0]
            self.assertEqual((item.days_overdue, item.level, item.amount_due_cents), (34, 3, 12050))
            self.assertTrue(item.followup_is_due)
        finally:
            c.close()
        self.assertIn("DEMO-001", self.client.get("/factures").text)

    def test_duplicate_invoice_number_is_safe(self):
        cid = self.add_client()
        self.add_invoice(cid)
        r = self.client.post("/factures/nouvelle", data=self.invoice_form(cid, number="demo - 001"))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.rows("SELECT * FROM invoices")), 1)

    def test_invalid_invoice_fields_no_write(self):
        cid = self.add_client()
        for fields in ({"total":"0"}, {"total":"-12"}, {"total":"NaN"}, {"total":"1,234"}, {"customer_id":"9999"}, {"due_date":"2020-01-01"}, {"issue_date":"no date"}, {"issue_date":(today()+timedelta(days=1)).isoformat()}, {"payment_state":"sent"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.client.post("/factures/nouvelle", data={**self.invoice_form(cid), **fields}).status_code, 422)
        self.assertEqual(self.rows("SELECT * FROM invoices"), [])

    def test_paid_retires_draft_and_unpaid_restores_eligibility(self):
        iid = self.add_invoice()
        fid = self.draft()
        self.assertEqual(self.client.post(f"/api/followups/{fid}/validate?contexte=demo").status_code, 200)
        self.assertEqual(self.pay(iid,"paid").status_code, 303)
        self.assertEqual(self.rows("SELECT status FROM followups")[0]["status"], "obsolete")
        self.assertEqual(self.rows("SELECT status FROM invoices")[0]["status"], "paid")
        self.assertEqual(self.client.post(f"/api/followups/{fid}/envoyer?contexte=demo").status_code, 409)
        self.assertEqual(self.pay(iid,"unpaid").status_code, 303)
        self.assertNotEqual(self.draft(), fid)
        self.assertEqual(len(self.rows("SELECT * FROM payment_changes")), 2)

    def test_partial_payment_is_cumulative_and_recalculates(self):
        iid = self.add_invoice()
        self.draft()
        self.assertEqual(self.pay(iid,"partial",amount_paid="20,50",note="Virement vérifié").status_code, 303)
        c = db.connect(self.demo)
        try:
            item = overdue.load_all(c, today())[0]
            self.assertEqual(item.amount_due_cents, 10000)
            self.assertEqual(followups.create_drafts(c,today(),mode="mock").queued[0].invoice.amount_due_cents, 10000)
        finally:
            c.close()
        self.assertIn("Virement vérifié", self.client.get(f"/factures/{iid}").text)

    def test_invalid_partial_and_future_date(self):
        iid = self.add_invoice()
        for paid in ("0", "-1", "120,50", "999", "1.999"):
            self.assertEqual(self.pay(iid,"partial",amount_paid=paid).status_code, 422)
        self.assertEqual(self.pay(iid,"paid",payment_date=(today()+timedelta(days=1)).isoformat()).status_code, 422)
        self.assertEqual(self.rows("SELECT * FROM payment_changes"), [])

    def test_noop_has_clear_success_and_no_history(self):
        iid = self.add_invoice()
        r = self.pay(iid,"unpaid")
        self.assertIn("paiement=inchange", r.headers["location"])
        self.assertIn("déjà à jour", self.client.get(r.headers["location"]).text)
        self.assertEqual(self.rows("SELECT * FROM payment_changes"), [])

    def test_stale_payment_form_cannot_overwrite(self):
        iid = self.add_invoice()
        stale = self.version(f"/factures/{iid}/paiement")
        self.pay(iid,"paid")
        self.assertEqual(self.pay(iid,"unpaid",version=stale).status_code, 409)
        self.assertEqual(self.rows("SELECT status FROM invoices")[0]["status"], "paid")

    def test_edit_contact_invalidates_unpublished_drafts(self):
        cid = self.add_client()
        self.add_invoice(cid)
        self.draft()
        url = f"/clients/{cid}/modifier"
        form = {**self.owner,"email":"other@example.com", "version":self.version(url)}
        self.assertEqual(self.client.post(url,data=form,follow_redirects=False).status_code,303)
        self.assertEqual(self.rows("SELECT status FROM followups")[0]["status"],"obsolete")
        self.assertEqual(self.client.post(url,data=form).status_code,409)

    def test_same_contact_does_not_retire_draft(self):
        cid = self.add_client()
        self.add_invoice(cid)
        self.draft()
        url = f"/clients/{cid}/modifier"
        self.client.post(url,data={**self.owner,"version":self.version(url)})
        self.assertEqual(self.rows("SELECT status FROM followups")[0]["status"],"pending")

    def test_import_cannot_undo_manual_payment(self):
        iid = self.add_invoice()
        self.pay(iid,"paid")
        c = db.connect(self.demo)
        try:
            row = runner.ParsedRow(line=2, number="DEMO-001", customer_name=self.owner["name"], total_ttc_cents=12050, amount_paid_cents=0, issue_date=today()-timedelta(days=40),due_date=today()-timedelta(days=34))
            plan = reconcile.reconcile([row],reconcile.load_existing(c))
            self.assertEqual(len(plan.flagged),1)
            self.assertIn("manuellement",plan.flagged[0].reasons[0])
            row.amount_paid_cents=12050
            self.assertEqual(len(reconcile.reconcile([row],reconcile.load_existing(c)).unchanged),1)
        finally:
            c.close()

    def test_cross_context_and_cross_site_writes_rejected(self):
        cid = self.add_client()
        iid = self.add_invoice(cid)
        rc = real_client()
        for path, form in (("/clients/nouveau",self.owner),("/factures/nouvelle",self.invoice_form(cid)),(f"/factures/{iid}/paiement",{"contexte":"demo"}),(f"/clients/{cid}/modifier",self.owner)):
            self.assertEqual(rc.post(path,data=form).status_code,409)
        self.assertEqual(self.client.post("/clients/nouveau",data=self.owner,headers={"Origin":"https://evil.example"}).status_code,403)
        self.assertEqual(self.rows("SELECT * FROM customers",self.real),[])

    def test_real_mode_supports_same_manual_actions(self):
        rc=real_client()
        cid=self.add_client(client=rc,contexte="real")
        iid=self.add_invoice(cid,client=rc,contexte="real")
        self.assertEqual(self.pay(iid,"paid",client=rc,contexte="real").status_code,303)
        self.assertEqual(self.rows("SELECT status FROM invoices",self.real)[0]["status"],"paid")
        self.assertEqual(self.rows("SELECT * FROM invoices"),[])

    def test_concurrent_invoice_creates_are_not_duplicated(self):
        cid = self.add_client()
        def insert(_):
            return TestClient(server.app).post("/factures/nouvelle",data=self.invoice_form(cid),follow_redirects=False).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(insert,range(2))),[303,409])
        self.assertEqual(len(self.rows("SELECT * FROM invoices")),1)

    def test_payment_failure_rolls_back_amount_and_history(self):
        iid = self.add_invoice()
        c = db.connect(self.demo)
        try:
            version=manual.payment_version(c,c.execute("SELECT * FROM invoices").fetchone())
            with patch.object(manual,"retire_drafts",side_effect=RuntimeError("test rollback")), self.assertRaises(RuntimeError):
                manual.update_payment(c,iid,{"version":version,"payment_state":"paid","payment_date":today().isoformat()})
            self.assertEqual(c.execute("SELECT amount_paid_cents FROM invoices").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT count(*) FROM payment_changes").fetchone()[0],0)
        finally:
            c.close()

    def test_personalized_gmail_requires_review_and_demo(self):
        self.add_invoice()
        fid=self.draft()
        url=f"/relances/{fid}/brouillon-test?contexte=demo"
        self.assertEqual(self.client.get(url).status_code,409)
        self.client.post(f"/api/followups/{fid}/validate?contexte=demo")
        token=google_mail.GoogleToken(refresh_token="fake",email="owner@example.com")
        with patch.object(google_mail,"load_token",return_value=token):
            page=self.client.get(url)
            self.assertEqual(page.status_code,200)
            self.assertIn("DEMO-001",page.text)
            self.assertIn("owner@example.com",page.text)
            self.assertIn("NE PAS ENVOYER",page.text)
            self.assertNotIn("fake",page.text)
        self.assertEqual(real_client().get(url.replace("demo","real")).status_code,403)
        self.assertEqual(self.rows("SELECT status FROM followups")[0]["status"],"validated")

    def test_gmail_recipient_is_always_owner_and_long_messages_not_truncated(self):
        message=reminder_test_message("owner@example.com","Facture DEMO-002","Bonjour", "DEMO-002")
        query=parse_qs(urlparse(message["handover"]["url"]).query)
        self.assertEqual(query["to"],["owner@example.com"])
        self.assertIn("Bonjour",query["body"][0])
        long=reminder_test_message("owner@example.com","Objet","é"*4000,"D-1")
        self.assertFalse(long["handover"]["fits"])
        self.assertIsNone(long["handover"]["url"])

    def test_prepare_one_invoice_does_not_prepare_other_invoices(self):
        cid=self.add_client()
        first=self.add_invoice(cid)
        self.add_invoice(cid,number="DEMO-002")
        r=self.client.post("/relances/preparer",data={"contexte":"demo","invoice_id":str(first),"next":f"/factures/{first}"})
        self.assertEqual(r.status_code,200)
        self.assertEqual([r["invoice_id"] for r in self.rows("SELECT invoice_id FROM followups")],[first])

    def test_paid_invoice_creation_has_explicit_payment_date(self):
        received=(today()-timedelta(days=2)).isoformat()
        iid=self.add_invoice(payment_state="paid",payment_date=received)
        row=self.rows("SELECT * FROM invoices")[0]
        self.assertEqual((row["status"],row["paid_date"]),("paid",received))
        self.assertIn("Aucune relance — facture réglée", self.client.get(f"/factures/{iid}").text)
        self.assertEqual(self.rows("SELECT * FROM payment_changes")[0]["payment_date"],received)

    def test_payment_date_correction_and_repeated_form_noop(self):
        iid=self.add_invoice()
        self.pay(iid,"paid")
        corrected=(today()-timedelta(days=1)).isoformat()
        self.assertEqual(self.pay(iid,"paid",payment_date=corrected).status_code,303)
        self.assertEqual(self.rows("SELECT paid_date FROM invoices")[0]["paid_date"],corrected)
        self.assertEqual(len(self.rows("SELECT * FROM payment_changes")),2)
        r=self.pay(iid,"paid",payment_date=corrected)
        self.assertIn("paiement=inchange",r.headers["location"])

    def test_existing_external_sent_history_is_preserved(self):
        iid=self.add_invoice()
        fid=self.draft()
        c=db.connect(self.demo)
        c.execute("UPDATE followups SET status='sent',sent_at=? WHERE id=?",(today().isoformat(),fid))
        c.commit()
        c.close()
        self.pay(iid,"paid")
        self.assertEqual(self.rows("SELECT status FROM followups")[0]["status"],"sent")

    def test_long_gmail_page_has_copy_fallback_not_empty_link(self):
        self.add_invoice()
        fid=self.draft()
        c=db.connect(self.demo)
        c.execute("UPDATE followups SET status='validated',body=? WHERE id=?",("é"*4000,fid))
        c.commit()
        c.close()
        token=google_mail.GoogleToken(refresh_token="fake",email="owner@example.com")
        with patch.object(google_mail,"load_token",return_value=token):
            page=self.client.get(f"/relances/{fid}/brouillon-test?contexte=demo").text
        self.assertIn("Copier le message",page)
        self.assertNotIn('href="None"',page)
        self.assertIn("é"*4000,page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
