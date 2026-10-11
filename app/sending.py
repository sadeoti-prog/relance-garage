"""Real sending, over SMTP, with an app password.

The `.eml` and Gmail routes need no credential at all, and they stay. But
downloading a file or switching tabs twenty-three times is something an owner
abandons after two weeks, so this is the route for a garage that wants the
button to just send.

An **app password** is not the account password. It is a 16-character code the
owner generates in his own Google account (or equivalent), it can only send
mail, it requires 2-step verification to be enabled, and he revokes it in one
click. No Google review, unlike the Gmail API.

Three rules are enforced here, in code, not by lack of capability:

* a message is sent only if it has been validated by the owner first;
* a formal demand is never an e-mail;
* in demo mode a follow-up is never sent — the check is the first statement of
  `send()`, before any socket, and the dashboard shows a preview instead.

The one exemption is `send_test`, which writes to the owner's own address to
check his app password. That is a setup step, not a customer send, and making
him leave demo mode to verify a credential would be absurd.

Credentials live in a gitignored JSON file on the garage's own machine, never
in a database and never in the demo dataset.
"""

from __future__ import annotations

import json
import smtplib
import socket
import ssl
from dataclasses import dataclass, asdict
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from pathlib import Path

from .config import DEMO_MODE, SMTP_SETTINGS_PATH
from .email_address import is_valid as is_valid_email

# Presets for the providers a French garage actually uses. Ports and encryption
# are the ones each provider documents; the owner only types his address and
# his app password.
PROVIDERS = {
    "gmail": {
        "label": "Gmail / Google Workspace",
        "host": "smtp.gmail.com",
        "port": 587,
        "security": "starttls",
        "help": (
            "Créez un mot de passe d’application dans votre compte Google "
            "(Sécurité → Validation en deux étapes → Mots de passe des "
            "applications). La validation en deux étapes doit être active."
        ),
    },
    "microsoft": {
        "label": "Microsoft 365 / Outlook",
        "host": "smtp.office365.com",
        "port": 587,
        "security": "starttls",
        "help": (
            "Créez un mot de passe d’application dans votre compte Microsoft "
            "(Sécurité → Options de sécurité avancées)."
        ),
    },
    "ovh": {
        "label": "OVH",
        "host": "ssl0.ovh.net",
        "port": 587,
        "security": "starttls",
        "help": "Utilisez le mot de passe de la boîte e-mail OVH.",
    },
    "gandi": {
        "label": "Gandi",
        "host": "mail.gandi.net",
        "port": 587,
        "security": "starttls",
        "help": "Utilisez le mot de passe de la boîte e-mail Gandi.",
    },
    "autre": {
        "label": "Autre fournisseur",
        "host": "",
        "port": 587,
        "security": "starttls",
        "help": "Demandez à votre hébergeur le serveur SMTP et le port.",
    },
}


@dataclass
class SmtpSettings:
    provider: str = "gmail"
    sender: str = ""              # the address messages leave from
    host: str = "smtp.gmail.com"
    port: int = 587
    security: str = "starttls"    # 'starttls' | 'ssl'
    username: str = ""            # usually the same as sender
    password: str = ""            # the app password
    sender_name: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.sender and self.host and self.password)

    @property
    def login(self) -> str:
        return self.username or self.sender

    def masked(self) -> str:
        """What the settings screen shows instead of the password."""
        if not self.password:
            return ""
        return "•" * 12 + f" ({len(self.password)} caractères)"


class SendingProblem(Exception):
    """A send that failed. `reason` is French and tells the owner what to do."""

    def __init__(self, reason: str, blocking: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.blocking = blocking


# ---------------------------------------------------------------------------
# Settings on disk
# ---------------------------------------------------------------------------

def load_settings(path: Path | None = None) -> SmtpSettings:
    path = Path(path or SMTP_SETTINGS_PATH)
    if not path.exists():
        return SmtpSettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return SmtpSettings()
    known = {field: payload.get(field) for field in SmtpSettings().__dict__}
    return SmtpSettings(**{k: v for k, v in known.items() if v is not None})


def save_settings(settings: SmtpSettings, path: Path | None = None) -> None:
    path = Path(path or SMTP_SETTINGS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clear_settings(path: Path | None = None) -> bool:
    path = Path(path or SMTP_SETTINGS_PATH)
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Turning an SMTP failure into something the owner can act on
# ---------------------------------------------------------------------------

def explain(error: Exception) -> str:
    """A French sentence saying what to do, not what the server said."""
    text = str(error).lower()

    if isinstance(error, smtplib.SMTPAuthenticationError):
        if "application-specific" in text or "app password" in text:
            return (
                "Ce mot de passe n’est pas un mot de passe d’application. "
                "Dans votre compte Google : Sécurité → Validation en deux "
                "étapes → Mots de passe des applications, puis recopiez le "
                "code de 16 caractères."
            )
        if "credentials" in text or "username and password" in text or "535" in text:
            return (
                "Identifiants refusés. Vérifiez l’adresse d’envoi et recopiez "
                "le mot de passe d’application sans espaces. S’il ne fonctionne "
                "toujours pas, révoquez-le et générez-en un nouveau."
            )
        return (
            "Connexion refusée par le serveur de messagerie. Vérifiez que la "
            "validation en deux étapes est bien active sur le compte, puis "
            "regénérez un mot de passe d’application."
        )

    if isinstance(error, smtplib.SMTPSenderRefused):
        return (
            "Le serveur refuse cette adresse d’expéditeur. Elle doit être "
            "exactement celle du compte qui a créé le mot de passe d’application."
        )

    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return (
            "Le serveur a refusé l’adresse du destinataire. Vérifiez "
            "l’adresse e-mail de ce client."
        )

    if isinstance(error, smtplib.SMTPDataError) or isinstance(error, smtplib.SMTPResponseException):
        if "quota" in text or "limit" in text or "550 5.4.5" in text or "454 4.7.0" in text:
            return (
                "Quota d’envoi atteint pour aujourd’hui. Un compte Gmail "
                "gratuit est limité à environ 500 destinataires par jour. "
                "Reprenez demain, ou envoyez le reste depuis votre messagerie."
            )
        return (
            "Le serveur de messagerie a refusé le message "
            f"({getattr(error, 'smtp_code', '?')}). Réessayez dans quelques "
            "minutes ; si cela persiste, envoyez ce message depuis votre "
            "messagerie."
        )

    if isinstance(error, (socket.gaierror, ConnectionRefusedError, OSError, TimeoutError)):
        return (
            "Serveur de messagerie injoignable. Vérifiez le nom du serveur et "
            "le port, et que votre connexion internet fonctionne."
        )

    if isinstance(error, ssl.SSLError):
        return (
            "Échec de la connexion sécurisée. Essayez le port 587 en STARTTLS, "
            "ou 465 en SSL."
        )

    return (
        "L’envoi a échoué et le message n’est pas parti. Vous pouvez le "
        "transmettre par votre messagerie en attendant."
    )


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _connect(settings: SmtpSettings, timeout: float = 20.0):
    context = ssl.create_default_context()
    if settings.security == "ssl":
        return smtplib.SMTP_SSL(settings.host, settings.port,
                                timeout=timeout, context=context)
    server = smtplib.SMTP(settings.host, settings.port, timeout=timeout)
    server.ehlo()
    server.starttls(context=context)
    server.ehlo()
    return server


def _build(settings: SmtpSettings, recipient: str, subject: str,
           body: str) -> EmailMessage:
    message = EmailMessage(policy=SMTP_POLICY)
    sender = (
        f"{settings.sender_name} <{settings.sender}>"
        if settings.sender_name else settings.sender
    )
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject or ""
    message.set_content(body or "", subtype="plain", charset="utf-8")
    return message


def preview(settings: SmtpSettings, recipient: str, subject: str,
            body: str) -> dict:
    """What would leave the machine, without anything leaving it.

    Demo mode shows this instead of sending. It opens no socket and resolves
    no name — it only formats what the real send would have transmitted.
    """
    sender = (
        f"{settings.sender_name} <{settings.sender}>"
        if settings.sender_name and settings.sender else settings.sender
    )
    return {
        "from": sender or "(adresse d’envoi non configurée)",
        "to": recipient,
        "subject": subject,
        "body": body,
    }


def send(settings: SmtpSettings, recipient: str, subject: str, body: str,
         allow_in_demo: bool = False) -> None:
    """Send one message. Raises `SendingProblem` with a French reason.

    The demo-mode guard is the first statement on purpose: no socket is opened,
    no DNS lookup happens, nothing leaves the machine.

    `allow_in_demo` exists for exactly one caller — `send_test`, which writes to
    the owner's own address to check his app password. Checking a credential is
    a setup step, not a customer send, and forcing him out of demo mode to do it
    would be absurd. No follow-up ever passes this flag.
    """
    if DEMO_MODE and not allow_in_demo:
        raise SendingProblem(
            "Mode démonstration : aucun envoi réel n’est possible. "
            "Désactivez DEMO_MODE pour envoyer pour de vrai.",
        )

    if not settings.configured:
        raise SendingProblem(
            "L’envoi n’est pas configuré. Renseignez votre adresse et votre "
            "mot de passe d’application dans les paramètres d’envoi."
        )
    if not is_valid_email(settings.sender):
        raise SendingProblem("L’adresse d’envoi configurée n’est pas valide.")
    if not is_valid_email(recipient):
        raise SendingProblem(
            "L’adresse e-mail de ce client est invalide ou contient plusieurs destinataires."
        )

    message = _build(settings, recipient, subject, body)

    try:
        server = _connect(settings)
    except Exception as error:                             # noqa: BLE001
        raise SendingProblem(explain(error)) from error

    try:
        server.login(settings.login, settings.password)
        server.send_message(message)
    except Exception as error:                             # noqa: BLE001
        raise SendingProblem(explain(error)) from error
    finally:
        try:
            server.quit()
        except Exception:                                  # noqa: BLE001
            pass


def send_test(settings: SmtpSettings) -> str:
    """Send one message to the owner's own address, and say what happened.

    Works in demo mode: it goes to the owner, never to a customer, and it is
    the only way to check an app password without disabling the demo.
    """
    send(
        settings,
        settings.sender,
        "Relance Garage — test de connexion",
        "Ce message confirme que l’envoi automatique fonctionne.\n\n"
        "Il a été envoyé depuis votre propre adresse, avec votre mot de passe "
        "d’application. Vous pouvez révoquer ce mot de passe à tout moment "
        "depuis votre compte : l’envoi s’arrêtera aussitôt, et les relances "
        "resteront disponibles en téléchargement.\n\n"
        "Ceci est un test de configuration : aucune relance client n’a été "
        "envoyée.\n",
        allow_in_demo=True,
    )
    return (
        f"Message de test envoyé à {settings.sender}. "
        "S’il arrive bien, l’envoi automatique est prêt."
    )
