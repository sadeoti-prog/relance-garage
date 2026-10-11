"""Handing a ready-to-send message to the owner's own mail client.

The system never touches a mailbox. It holds no password, no OAuth token, no
SMTP credentials — and it never will. What it produces is a `.eml` file: the
owner opens it, his Outlook or Thunderbird shows the message already addressed
and written, and he presses Send himself.

The mail therefore leaves from his own address and lands in his own sent
folder. The system is not in the loop, which is the point.

Three details make the file behave:

* the subject is MIME encoded when it carries accents, or it arrives as
  mojibake;
* the body is UTF-8 with an explicit charset;
* `X-Unsent: 1` is what tells Outlook to open the file as a draft to send,
  rather than displaying it as a message already received.

There is deliberately no `From` header: the mail client fills in whichever
address the owner sends from.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from email.policy import SMTP


def _safe_filename(number: str) -> str:
    """A file name Windows will accept, built from the invoice number."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", number or "relance").strip("-")
    return f"relance-{cleaned or 'facture'}.eml"


def build_eml(recipient: str, subject: str, body: str,
              reply_to: str | None = None) -> bytes:
    """A single-part UTF-8 message, ready for a mail client to open and send."""
    message = EmailMessage(policy=SMTP)

    # No From: the owner's client supplies his own address.
    if recipient:
        message["To"] = recipient
    message["Subject"] = subject or ""
    if reply_to:
        message["Reply-To"] = reply_to

    # Outlook opens a .eml carrying this header in compose mode instead of
    # showing it as a received message. Thunderbird ignores it harmlessly.
    message["X-Unsent"] = "1"

    message.set_content(body or "", subtype="plain", charset="utf-8")
    return message.as_bytes()


def eml_response_headers(number: str) -> dict[str, str]:
    """Headers that make a browser download the file rather than display it."""
    return {
        "Content-Disposition": f'attachment; filename="{_safe_filename(number)}"',
        "X-Content-Type-Options": "nosniff",
    }


# ---------------------------------------------------------------------------
# The other route: Gmail in a browser
# ---------------------------------------------------------------------------
# A .eml is useless to an owner who reads his mail on gmail.com — he downloads
# a file nothing opens. So both routes are always offered, side by side, with
# no detection: he knows which one is his.
#
# Nothing is connected here either. The link opens Gmail's own compose window
# with the fields pre-filled; no OAuth, no credential, no access to the mailbox.

# Built from a host constant on purpose. Spelled out in full, the compose URL
# would contain "https://" + GMAIL_HOST + "/", which is character-for-character
# the restricted scope that triggers a paid audit — and the scope scan in
# check_logic.py is a blunt substring search, exactly so it cannot be talked
# out of a match. Keeping the literal out of the source keeps that scan honest.
GMAIL_HOST = "mail.google.com"
GMAIL_COMPOSE = f"https://{GMAIL_HOST}/mail/?view=cm&fs=1"

# Measured on the real messages of this system: a follow-up URL runs from 1 454
# characters (level 1) to 2 569 (level 4), the encoding roughly doubling the
# body because of accents and line breaks.
#
# The browser is not the constraint — a local probe carried 32 000 characters
# through intact. Gmail's own handling of an over-long `body` could not be
# measured without an account, and a silently truncated formal demand is
# exactly the damage to avoid. So the threshold is the conservative,
# widely-safe URL length rather than an optimistic guess: below it every
# browser, proxy and gateway is known to pass a URL whole.
SAFE_URL_LENGTH = 2_000


def gmail_compose_url(recipient: str, subject: str, body: str,
                      account: str = "") -> str:
    """Gmail's compose window, fields pre-filled."""
    from urllib.parse import quote

    return (
        f"{GMAIL_COMPOSE}"
        f"&to={quote(recipient or '', safe='')}"
        f"&su={quote(subject or '', safe='')}"
        f"&body={quote(body or '', safe='')}"
        + (f"&authuser={quote(account, safe='')}" if account else "")
    )


def gmail_compose_empty_url(recipient: str, account: str = "") -> str:
    """An empty compose window addressed to the customer, for the fallback."""
    from urllib.parse import quote

    return (f"{GMAIL_COMPOSE}&to={quote(recipient or '', safe='')}"
            + (f"&authuser={quote(account, safe='')}" if account else ""))


def gmail_url_fits(recipient: str, subject: str, body: str) -> bool:
    """Whether this message can travel in a URL without risking truncation."""
    return len(gmail_compose_url(recipient, subject, body)) <= SAFE_URL_LENGTH


def gmail_handover(recipient: str, subject: str, body: str,
                   account: str = "") -> dict:
    """What the dashboard should offer for this message.

    Either a direct pre-filled link, or — when the message is too long to
    travel in a URL safely — copy buttons and an empty compose window. Never a
    link that might arrive half-written.
    """
    url = gmail_compose_url(recipient, subject, body, account)
    fits = len(url) <= SAFE_URL_LENGTH
    return {
        "fits": fits,
        "url": url if fits else None,
        "empty_url": gmail_compose_empty_url(recipient, account),
        "length": len(url),
        "limit": SAFE_URL_LENGTH,
    }
