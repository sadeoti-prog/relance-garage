"""Draft-only regression checks. No mailbox, credentials or real ledger used."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app import google_mail, server
from app.eml import SAFE_URL_LENGTH, gmail_handover
from app.mail_demo import test_message
from check_product_safety import isolated_application, pending_email_followup, real_client


class DraftDemoTests(unittest.TestCase):
    def test_message_is_fictional_and_self_addressed(self):
        data = test_message("owner@example.com")
        query = parse_qs(urlparse(data["handover"]["url"]).query)
        self.assertEqual(query["to"], ["owner@example.com"])
        self.assertEqual(query["authuser"], ["owner@example.com"])
        self.assertEqual(query["su"], [data["subject"]])
        self.assertEqual(query["body"], [data["body"]])
        self.assertIn("NE PAS ENVOYER", data["subject"])
        self.assertIn("TEST-DEMO-001", data["body"])
        self.assertNotIn("iban", data["body"].lower())
        self.assertTrue(data["handover"]["fits"])

    def test_missing_and_header_injected_addresses_are_rejected(self):
        for account in ("", "invalid", "a@example.com\r\nBcc: b@example.com"):
            with self.subTest(account=account), self.assertRaises(ValueError):
                test_message(account)

    def test_account_is_included_in_url_length_limit(self):
        offered = gmail_handover("a@b.fr", "S", "a" * SAFE_URL_LENGTH,
                                 account="owner@example.com")
        self.assertFalse(offered["fits"])
        self.assertIsNone(offered["url"])
        self.assertEqual(parse_qs(urlparse(offered["empty_url"]).query)["authuser"],
                         ["owner@example.com"])

    def test_explicit_test_page_does_not_access_ledgers_or_send(self):
        token = google_mail.GoogleToken(refresh_token="fake", email="owner@example.com")
        with patch.object(google_mail, "load_token", return_value=token), \
             patch.object(server.db_module, "connect", side_effect=AssertionError("ledger access")), \
             patch.object(google_mail, "send", side_effect=AssertionError("send")), \
             patch.object(server.sending, "send", side_effect=AssertionError("send")):
            client = TestClient(server.app)
            page = client.get("/parametres/envoi/brouillon-test?to=customer@example.com").text
            self.assertIn("owner@example.com", page)
            self.assertNotIn("customer@example.com", page)
            self.assertIn("noopener noreferrer", page)
            self.assertIn("ne peut pas confirmer cet enregistrement", page)
            self.assertNotIn("access_token", page)
            self.assertNotIn("fake", page)
            self.assertEqual(client.post("/parametres/envoi/brouillon-test").status_code, 405)

    def test_disconnected_account_has_useful_empty_state(self):
        with patch.object(google_mail, "load_token", return_value=google_mail.GoogleToken()):
            page = TestClient(server.app).get("/parametres/envoi/brouillon-test").text
            self.assertIn("Connectez d’abord votre compte Gmail", page)
            self.assertNotIn('id="gmail-test-link"', page)

    def test_default_demo_settings_do_not_reveal_connected_account(self):
        token = google_mail.GoogleToken(refresh_token="fake", email="owner@example.com")
        with patch.object(google_mail, "load_token", return_value=token):
            page = TestClient(server.app).get("/parametres/envoi").text
            self.assertNotIn("owner@example.com", page)
            self.assertIn("Préparer le test Gmail", page)

    def test_repeated_previews_never_change_status_or_send_in_either_context(self):
        with tempfile.TemporaryDirectory(prefix="relance-draft-only-") as folder:
            root = Path(folder)
            demo, real = root / "demo.db", root / "real.db"
            demo_id = pending_email_followup(demo)
            real_id = pending_email_followup(real)
            with isolated_application(demo, real), \
                 patch.object(google_mail, "send", side_effect=AssertionError("send")), \
                 patch.object(server.sending, "send", side_effect=AssertionError("send")):
                for mode, client, followup_id, path in (
                    ("demo", TestClient(server.app), demo_id, demo),
                    ("real", real_client(), real_id, real),
                ):
                    with self.subTest(mode=mode):
                        base = f"/api/followups/{followup_id}"
                        self.assertEqual(client.post(base + f"/validate?contexte={mode}").status_code, 200)
                        for _ in range(2):
                            result = client.post(base + f"/envoyer?contexte={mode}").json()
                            self.assertEqual(result["status"], "validated")
                            self.assertTrue(result["preview_only"])
                        conn = server.db_module.connect(path)
                        try:
                            row = conn.execute("SELECT status, sent_at FROM followups WHERE id=?",
                                               (followup_id,)).fetchone()
                            self.assertEqual(tuple(row), ("validated", None))
                        finally:
                            conn.close()
                        self.assertNotIn('data-url="https://mail.google.com', client.get("/relances").text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
