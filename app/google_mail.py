"""Sending through the owner's Google account, send-only.

He clicks one button, picks his Google account, and it is done. No 2-step
verification to enable, no 16-character code to copy — that setup lost the sale
before it started.

**One Gmail scope, and only one:** `gmail.send`. It is classified *sensitive*,
which needs light verification. Every scope that can read a mailbox —
`gmail.readonly`, `gmail.modify`, `gmail.metadata`, `mail.google.com` — is
*restricted* and drags in a paid third-party security audit that takes months.
This system never reads anyone's mailbox: payment is detected by re-importing
the garage's own export. `scopes_in_use()` exists so a test can prove that
stays true.

Two basic scopes travel with it, `openid` and `email`, which are non-sensitive
and change nothing about verification. They exist so the interface can say
which account was connected — and so it can warn when that is not the garage's
own address.

**The app is in testing mode**, not published, so two things follow that are
normal rather than broken:

* the refresh token expires after seven days. When it does, the follow-up stays
  validated and unsent, and the screen says the connection expired and offers
  the reconnect button. It is never a silent failure;
* only accounts added as test users in the Cloud console can connect. Google
  refuses the others, and that refusal is translated into plain French rather
  than shown raw.

Implemented on the standard library alone: the loopback redirect flow that
Desktop OAuth clients use, and one HTTPS call to send.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .config import GOOGLE_CLIENT_PATH, GOOGLE_TOKEN_PATH
from .email_address import is_valid as is_valid_email

# The only Gmail scope this application will ever ask for.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

# Two basic scopes alongside it, and only these two. They are non-sensitive:
# they do not change the verification tier and carry no audit risk. They are
# here for one reason — to learn WHICH account was just connected.
#
# Many owners have two Google accounts, a personal one and the garage one.
# Without the address on screen he cannot tell which he picked, and customer
# follow-ups going out from his personal Gmail is the sort of mistake he
# discovers weeks later. Knowing the address is what makes the mismatch warning
# possible at all.
OPENID_SCOPE = "openid"
EMAIL_SCOPE = "email"

SCOPES: tuple[str, ...] = (GMAIL_SEND_SCOPE, OPENID_SCOPE, EMAIL_SCOPE)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
SEND_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

PERMISSIONS_URL = "https://myaccount.google.com/permissions"

# While the app is in testing mode Google expires the refresh token after seven
# days. That is a property of testing mode, not a defect, and the interface says
# so rather than pretending otherwise.
TESTING_REFRESH_DAYS = 7

# How long the owner has to finish picking his account before the loopback
# listener gives up.
FLOW_TIMEOUT_SECONDS = 300


def scopes_in_use() -> tuple[str, ...]:
    """Every OAuth scope this application requests. Asserted by the tests."""
    return SCOPES


class GoogleProblem(Exception):
    """A failure the owner can act on. `reason` is French."""

    def __init__(self, reason: str, expired: bool = False):
        super().__init__(reason)
        self.reason = reason
        # True when reconnecting is the fix, so the screen can offer the button.
        self.expired = expired


# ---------------------------------------------------------------------------
# The client the owner downloaded from the Cloud console
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientApp:
    client_id: str
    client_secret: str
    project_id: str = ""


def load_client(path: Path | None = None) -> ClientApp | None:
    path = Path(path or GOOGLE_CLIENT_PATH)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    body = payload.get("installed") or payload.get("web") or {}
    if not body.get("client_id") or not body.get("client_secret"):
        return None
    return ClientApp(
        client_id=body["client_id"],
        client_secret=body["client_secret"],
        project_id=body.get("project_id", ""),
    )


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------

@dataclass
class GoogleToken:
    refresh_token: str = ""
    access_token: str = ""
    access_expires_at: str = ""      # ISO, when the access token goes stale
    connected_at: str = ""
    scope: str = ""
    email: str = ""                  # which account was connected

    @property
    def connected(self) -> bool:
        return bool(self.refresh_token)

    @property
    def expires_on(self):
        """When the connection itself lapses, in testing mode."""
        if not self.connected_at:
            return None
        try:
            started = datetime.fromisoformat(self.connected_at)
        except ValueError:
            return None
        return (started + timedelta(days=TESTING_REFRESH_DAYS)).date()

    @property
    def days_left(self) -> int | None:
        expiry = self.expires_on
        if expiry is None:
            return None
        return (expiry - datetime.now().date()).days


def _address_from_id_token(id_token: str) -> str:
    """Read the e-mail out of Google's identity token.

    The signature is not re-verified: this token came back over TLS from
    Google's own token endpoint, in reply to a request this process made, so the
    channel already authenticates it. It is never accepted from anywhere else.
    """
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:                                      # noqa: BLE001
        return ""
    return str(claims.get("email") or "")


def address_matches_garage(address: str, garage_address: str) -> bool:
    """Whether the connected account is the garage's own."""
    return (address or "").strip().lower() == (garage_address or "").strip().lower()


def load_token(path: Path | None = None) -> GoogleToken:
    path = Path(path or GOOGLE_TOKEN_PATH)
    if not path.exists():
        return GoogleToken()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return GoogleToken()
    fields = GoogleToken().__dict__
    return GoogleToken(**{k: payload.get(k, v) for k, v in fields.items()})


def save_token(token: GoogleToken, path: Path | None = None) -> None:
    path = Path(path or GOOGLE_TOKEN_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(token.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def forget_token(path: Path | None = None) -> bool:
    path = Path(path or GOOGLE_TOKEN_PATH)
    if path.exists():
        path.unlink()
        return True
    return False


def disconnect(path: Path | None = None) -> bool:
    """Drop the token locally, and tell Google to drop it too."""
    token = load_token(path)
    removed = forget_token(path)
    if token.refresh_token:
        try:
            _post_form(REVOKE_ENDPOINT, {"token": token.refresh_token})
        except Exception:                                  # noqa: BLE001
            pass          # the local copy is gone either way; that is what matters
    return removed


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def _post_json(url: str, payload: dict, access_token: str) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------

_PENDING: dict[str, dict] = {}


def _page(title: str, message: str, back_url: str) -> bytes:
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>{title}</title>
<meta http-equiv="refresh" content="2;url={back_url}">
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f7f9;
color:#16181d;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
div{{background:#fff;border:1px solid #e6e8ec;border-radius:10px;padding:32px 36px;
max-width:520px;line-height:1.55}}h1{{font-size:19px;margin:0 0 10px}}
p{{color:#6b7280;font-size:14px;margin:0}}</style></head>
<body><div><h1>{title}</h1><p>{message}</p></div></body></html>""".encode("utf-8")


def start_flow(return_url: str) -> str:
    """Open a loopback listener and return the Google URL to send the owner to."""
    client = load_client()
    if client is None:
        raise GoogleProblem(
            "Le fichier google-client.json est introuvable ou incomplet. "
            "Téléchargez à nouveau l’identifiant OAuth « Application de "
            "bureau » depuis la console Google Cloud et placez-le à la racine "
            "du projet."
        )

    state = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = FLOW_TIMEOUT_SECONDS
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"

    _PENDING[state] = {
        "client": client,
        "redirect_uri": redirect_uri,
        "return_url": return_url,
        "server": server,
    }
    server.expected_state = state                          # type: ignore[attr-defined]

    thread = threading.Thread(target=_serve_once, args=(server,), daemon=True)
    thread.start()

    query = urllib.parse.urlencode({
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent is what yields a refresh token rather than a
        # one-hour access token.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return f"{AUTH_ENDPOINT}?{query}"


def _serve_once(server: HTTPServer) -> None:
    try:
        server.handle_request()
    except Exception:                                      # noqa: BLE001
        pass
    finally:
        try:
            server.server_close()
        except Exception:                                  # noqa: BLE001
            pass


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):                          # keep the console quiet
        return

    def do_GET(self):                                      # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        pending = _PENDING.pop(state, None)

        if pending is None:
            self._reply(_page(
                "Connexion non reconnue",
                "Cette page ne correspond à aucune demande en cours. "
                "Relancez la connexion depuis les paramètres d’envoi.",
                "/",
            ))
            return

        back = pending["return_url"]
        error = (params.get("error") or [""])[0]
        if error:
            reason = _explain_authorisation_error(error)
            self._reply(_page("Connexion refusée", reason, f"{back}?google=refus"))
            return

        code = (params.get("code") or [""])[0]
        if not code:
            self._reply(_page(
                "Connexion incomplète",
                "Google n’a pas renvoyé de code d’autorisation.",
                f"{back}?google=echec",
            ))
            return

        try:
            payload = _post_form(TOKEN_ENDPOINT, {
                "code": code,
                "client_id": pending["client"].client_id,
                "client_secret": pending["client"].client_secret,
                "redirect_uri": pending["redirect_uri"],
                "grant_type": "authorization_code",
            })
        except Exception:                                  # noqa: BLE001
            self._reply(_page(
                "Connexion échouée",
                "L’échange avec Google n’a pas abouti. Réessayez.",
                f"{back}?google=echec",
            ))
            return

        now = datetime.now()
        token = GoogleToken(
            refresh_token=payload.get("refresh_token", ""),
            access_token=payload.get("access_token", ""),
            access_expires_at=(
                now + timedelta(seconds=int(payload.get("expires_in", 3600)))
            ).isoformat(timespec="seconds"),
            connected_at=now.isoformat(timespec="seconds"),
            scope=payload.get("scope", ""),
            email=_address_from_id_token(payload.get("id_token", "")),
        )
        if not token.refresh_token:
            self._reply(_page(
                "Connexion incomplète",
                "Google n’a pas fourni de jeton de rafraîchissement. "
                "Révoquez l’accès dans votre compte puis reconnectez-vous.",
                f"{back}?google=echec",
            ))
            return

        save_token(token)
        self._reply(_page(
            "Messagerie connectée",
            "Vous pouvez fermer cet onglet. Retour aux paramètres…",
            f"{back}?google=ok",
        ))

    def _reply(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _explain_authorisation_error(error: str) -> str:
    if error == "access_denied":
        return (
            "L’accès a été refusé. Soit vous avez annulé, soit ce compte Google "
            "n’est pas encore autorisé : tant que l’application est en mode test, "
            "seuls les comptes ajoutés comme utilisateurs de test peuvent se "
            "connecter. Ajoutez cette adresse dans la console Google Cloud, "
            "puis réessayez."
        )
    if error == "admin_policy_enforced":
        return (
            "L’administrateur de ce domaine Google Workspace interdit cette "
            "connexion. Utilisez une autre adresse, ou demandez-lui d’autoriser "
            "l’application."
        )
    return f"Google a refusé la connexion ({error})."


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _refresh(token: GoogleToken, client: ClientApp) -> GoogleToken:
    try:
        payload = _post_form(TOKEN_ENDPOINT, {
            "refresh_token": token.refresh_token,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "grant_type": "refresh_token",
        })
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")
        except Exception:                                  # noqa: BLE001
            pass
        if error.code in (400, 401) and "invalid_grant" in detail:
            raise GoogleProblem(
                "La connexion à votre messagerie a expiré — reconnectez-vous.",
                expired=True,
            ) from error
        raise GoogleProblem(
            "Google a refusé de renouveler la connexion. Reconnectez votre "
            "messagerie.",
            expired=True,
        ) from error
    except Exception as error:                             # noqa: BLE001
        raise GoogleProblem(
            "Impossible de joindre Google. Vérifiez votre connexion internet."
        ) from error

    token.access_token = payload.get("access_token", "")
    if payload.get("id_token"):
        token.email = _address_from_id_token(payload["id_token"]) or token.email
    token.access_expires_at = (
        datetime.now() + timedelta(seconds=int(payload.get("expires_in", 3600)))
    ).isoformat(timespec="seconds")
    save_token(token)
    return token


def _needs_refresh(token: GoogleToken) -> bool:
    if not token.access_token or not token.access_expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(token.access_expires_at)
    except ValueError:
        return True
    return datetime.now() >= expiry - timedelta(seconds=60)


def _raw_message(recipient: str, subject: str, body: str) -> str:
    """RFC 822, base64url. No From: Gmail fills in the connected account."""
    message = EmailMessage(policy=SMTP_POLICY)
    message["To"] = recipient
    message["Subject"] = subject or ""
    message.set_content(body or "", subtype="plain", charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def send(recipient: str, subject: str, body: str) -> None:
    """Send one message through the connected Google account."""
    if not is_valid_email(recipient):
        raise GoogleProblem(
            "L’adresse e-mail de ce client est invalide ou contient plusieurs destinataires."
        )

    client = load_client()
    if client is None:
        raise GoogleProblem(
            "Identifiant Google introuvable : le fichier google-client.json "
            "est absent."
        )

    token = load_token()
    if not token.connected:
        raise GoogleProblem(
            "Aucune messagerie Google connectée.", expired=True
        )

    if _needs_refresh(token):
        token = _refresh(token, client)

    payload = {"raw": _raw_message(recipient, subject, body)}
    try:
        _post_json(SEND_ENDPOINT, payload, token.access_token)
        return
    except urllib.error.HTTPError as error:
        if error.code == 401:
            # The access token died early; one refresh, one retry, then give up.
            token = _refresh(token, client)
            try:
                _post_json(SEND_ENDPOINT, payload, token.access_token)
                return
            except Exception as retry_error:               # noqa: BLE001
                raise GoogleProblem(explain_http(retry_error)) from retry_error
        raise GoogleProblem(explain_http(error)) from error
    except Exception as error:                             # noqa: BLE001
        raise GoogleProblem(explain_http(error)) from error


def explain_http(error: Exception) -> str:
    """Turn a Gmail API failure into something the owner can act on."""
    if isinstance(error, urllib.error.HTTPError):
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace").lower()
        except Exception:                                  # noqa: BLE001
            pass

        if error.code in (401, 403) and "insufficient" in detail:
            return (
                "L’autorisation d’envoi est incomplète. Déconnectez puis "
                "reconnectez votre messagerie Google."
            )
        if error.code == 403 and ("limit" in detail or "quota" in detail):
            return (
                "Quota d’envoi Google atteint pour aujourd’hui (environ 500 "
                "destinataires par jour sur un compte gratuit). Reprenez demain, "
                "ou envoyez le reste depuis votre messagerie."
            )
        if error.code == 429:
            return (
                "Google demande de ralentir : trop d’envois d’un coup. "
                "Patientez quelques minutes puis réessayez."
            )
        if error.code in (400, 404) and "invalid" in detail:
            return (
                "Google a refusé le message. Vérifiez l’adresse du destinataire."
            )
        if error.code >= 500:
            return (
                "Google est momentanément indisponible. Réessayez dans quelques "
                "minutes ; le message n’est pas parti."
            )
        return (
            f"Google a refusé l’envoi (code {error.code}). Le message n’est pas "
            "parti."
        )

    return (
        "Impossible de joindre Google : le message n’est pas parti. Vérifiez "
        "votre connexion internet."
    )
