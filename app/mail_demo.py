"""A deliberate, self-addressed Gmail rehearsal; never a customer send.

The browser hands the message to Gmail's composer. The existing send-only
OAuth connection cannot save drafts, so we never pretend that it did. No
provider API or invoice database is accessed here.
"""

from .email_address import is_valid
from .eml import gmail_handover


def reminder_test_message(account: str, subject: str, body: str, number: str) -> dict:
    """Copy a reviewed DEMO reminder to its owner, never to the client."""
    if not is_valid(account):
        raise ValueError("Connectez d’abord une messagerie Google valide.")
    subject = "[TEST DÉMO — NE PAS ENVOYER] " + subject
    body = "DÉMONSTRATION — copie pour vous-même. Aucun paiement n’est demandé.\n\n" + body
    return {"account": account, "recipient": account, "subject": subject, "body": body,
            "invoice_number": number, "handover": gmail_handover(account, subject, body, account=account)}


def test_message(account: str) -> dict:
    if not is_valid(account):
        raise ValueError("Connectez d’abord une messagerie Google valide.")
    subject = "[TEST DÉMO — NE PAS ENVOYER] Relance Garage"
    body = (
        "Bonjour,\n\n"
        "Ceci est un brouillon de démonstration de Relance Garage.\n"
        "Exemple fictif : rappel pour la facture TEST-DEMO-001 de 120,00 €.\n\n"
        "Aucun paiement n’est demandé. Aucun client réel n’est concerné.\n"
        "Conservez ce message dans les brouillons, puis supprimez-le après le test.\n\n"
        "Bonne journée,\nL’équipe de démonstration"
    )
    return {
        "account": account, "recipient": account, "subject": subject, "body": body,
        "handover": gmail_handover(account, subject, body, account=account),
    }
