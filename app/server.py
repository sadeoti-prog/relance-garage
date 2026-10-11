"""The dashboard.

One page, served by FastAPI, rendered with Jinja2. No frontend framework, no
build step, no CDN: the page is HTML and a little vanilla JavaScript.

Every figure reaching the template is already a formatted string. The view layer
does no arithmetic either — same rule as the drafting layer.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import db as db_module
from . import followups as followup_module
from . import kpi
from . import overdue as engine
from . import google_mail
from . import sending
from .config import (
    DEMO_MODE, GARAGE, LEVEL_THRESHOLD_DAYS, LLM_MODE,
    MINUTES_PER_MANUAL_FOLLOWUP, today,
)
from .data_context import (
    COOKIE_NAME, DEMO, REAL, cookie_value, from_request, safe_return_path,
    template_context,
)
from .demo_reset import reset_demo
from .email_address import is_valid as is_valid_email
from .drafting import build_context
from .eml import build_eml, eml_response_headers, gmail_handover
from .guardrail import verify_draft
from .labels_fr import (
    CHANNEL, CUSTOMER_KIND, FOLLOWUP_STATUS, INVOICE_STATUS, LEVEL_INTENT,
    LEVEL_NAME, LEVEL_SHORT,
    format_date, format_date_long, format_delay, plural,
)
from .money import format_eur
from .mail_demo import test_message, reminder_test_message
from . import manual

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="Relance Garage", docs_url=None, redoc_url=None)
logger = logging.getLogger("relance_garage")

# The import screens live in their own module: they touch a different database
# and must never be confused with the demo ledger.
from .web_import import router as import_router          # noqa: E402

app.include_router(import_router)
from .manual_routes import router as manual_router
app.include_router(manual_router)


@app.middleware("http")
async def local_safety_headers(request: Request, call_next):
    """Reject cross-site mutations and keep customer pages out of caches."""
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = (request.headers.get("origin") or "").rstrip("/")
        expected = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
        fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
        if (origin and origin != expected) or fetch_site == "cross-site":
            return JSONResponse(
                {
                    "detail": (
                        "Action refusée : revenez dans l’application puis réessayez."
                    )
                },
                status_code=403,
            )
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


def _context(request: Request):
    return from_request(request)


def _require_rendered_context(request: Request, expected: str | None = None):
    """Reject an action submitted by a page rendered for another ledger.

    The mode cookie is shared by browser tabs. Without this second value a
    stale Demo tab could act on a same-numbered record in the real ledger after
    another tab switched modes. Every ledger mutation therefore carries the
    context that was visible when its button was rendered.
    """
    context = _context(request)
    expected = expected or request.query_params.get("contexte") or request.headers.get(
        "X-Relance-Context"
    )
    if expected not in {"demo", "real"} or expected != context.key:
        raise HTTPException(
            status_code=409,
            detail=(
                "Le contexte de données a changé dans un autre onglet. "
                "Rechargez la page avant de continuer."
            ),
        )
    return context


def _connect_for(request: Request):
    context = _context(request)
    # `db_module.DB_PATH` remains the single test seam used by the historical
    # verification suite. In the running application it is the exact same
    # immutable configured path as `DEMO.path`.
    path = db_module.DB_PATH if context.is_demo else context.path
    conn = db_module.connect(path)
    db_module.init_schema(conn)
    return context, conn


def _template(request: Request, name: str, context, active: str, **values):
    payload = template_context(request, context, active)
    payload.update(values)
    return templates.TemplateResponse(request, name, payload)


def _vehicle_label(row) -> str:
    return " ".join(
        part for part in (row["make"] or "", row["model"] or "") if part
    ) or "Véhicule non renseigné"


def _business_error(request: Request, message: str, status_code: int):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=status_code)
    context = _context(request)
    response = _template(
        request,
        "error.html",
        context,
        "",
        error={
            "title": "Cette action n’a pas abouti",
            "message": message,
            "status": status_code,
        },
    )
    response.status_code = status_code
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException):
    return _business_error(request, str(error.detail), error.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    # Pydantic includes the rejected input in ``errors()``. A failed edit may
    # contain an entire customer message, so logs keep only location and type.
    summary = [
        {"loc": tuple(item.get("loc", ())), "type": item.get("type", "invalid")}
        for item in error.errors()
    ]
    logger.info("Invalid request on %s: %s", request.url.path, summary)
    return _business_error(
        request,
        "Une information est absente ou invalide. Vérifiez le formulaire puis réessayez.",
        422,
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return _business_error(
        request,
        "Un problème inattendu est survenu. Rien n’a été envoyé. Réessayez ; "
        "si le problème revient, consultez le journal de l’application.",
        500,
    )


# ---------------------------------------------------------------------------
# View models — everything is turned into strings here, once
# ---------------------------------------------------------------------------

def _queue_rows(conn, reference) -> list[dict]:
    rows = []
    for row in followup_module.queue(conn, "pending"):
        # Pending wording is only trustworthy for the date used to calculate
        # it.  Older drafts are offered for recalculation, never for approval.
        if (row["created_at"] or "")[:10] != reference.isoformat():
            continue
        extra = row["penalty_cents"] + row["recovery_fee_cents"]
        rows.append(
            {
                "id": row["id"],
                "number": row["number"],
                "level": row["level"],
                "level_name": LEVEL_NAME[row["level"]],
                "level_short": LEVEL_SHORT[row["level"]],
                "level_intent": LEVEL_INTENT[row["level"]],
                "channel": CHANNEL[row["channel"]],
                "customer": row["customer_name"],
                "customer_kind": CUSTOMER_KIND[row["customer_kind"]],
                "email": row["email"],
                "vehicle": _vehicle_label(row),
                "plate": row["plate"] or "",
                "job": row["job_label"],
                "due_date": format_date(row["due_date"]),
                "delay": format_delay(row["days_overdue"]),
                "days_overdue": row["days_overdue"],
                "amount_due": format_eur(row["amount_due_cents"]),
                "penalty": format_eur(row["penalty_cents"]) if row["penalty_cents"] else None,
                "recovery_fee": (
                    format_eur(row["recovery_fee_cents"]) if row["recovery_fee_cents"] else None
                ),
                "claimed": format_eur(row["total_claimed_cents"]),
                "has_extra": bool(extra),
                "subject": row["subject"],
                "body": row["body"],
                "edited": bool(row["edited_by_human"]),
                # Said before he validates, not after: how a follow-up can leave
                # may change whether he wants to send it at all.
                "routes": _routes_for(row),
            }
        )
    return rows


def _flagged_rows(conn) -> list[dict]:
    rows = []
    for row in followup_module.queue(conn, "rejected"):
        rows.append(
            {
                "id": row["id"],
                "number": row["number"],
                "level_name": LEVEL_NAME[row["level"]],
                "customer": row["customer_name"],
                "vehicle": _vehicle_label(row),
                "amount_due": format_eur(row["amount_due_cents"]),
                "note": row["guardrail_note"] or "Chiffre non vérifiable.",
                "subject": row["subject"],
                "body": row["body"],
            }
        )
    return rows


# Why a follow-up has no send button. An empty toolbar explains nothing: the
# owner reads it as a broken product, and he is right to.
POSTAL_ONLY_REASON = (
    "Une mise en demeure part en lettre recommandée avec accusé de réception, "
    "jamais par e-mail. Imprimez-la et postez-la."
)
NO_EMAIL_REASON = (
    "Ce client n’a pas d’adresse e-mail utilisable : cette relance ne peut "
    "partir que par courrier. Imprimez-la et postez-la."
)


def _routes_for(row) -> dict:
    """Which sending routes this follow-up has, and why it lacks the others."""
    has_email = is_valid_email(row["email"])
    postal_only = row["channel"] == "courrier"

    if postal_only:
        reason = POSTAL_ONLY_REASON
    elif not has_email:
        reason = NO_EMAIL_REASON
    else:
        reason = None

    return {
        "by_email": not postal_only and has_email,
        "can_print": True,          # any follow-up can be printed and posted
        "reason": reason,
    }


def _handover_options(row) -> dict:
    """What this message can be handed over with, .eml and Gmail both.

    No detection of what the owner uses: both are always offered. The Gmail
    link is withheld only when the message is too long to travel in a URL
    safely — a silently truncated follow-up is worse than an extra copy-paste.
    """
    options = gmail_handover(row["email"], row["subject"], row["body"])
    options["eml_url"] = f"/relances/{row['id']}/message.eml"
    return options


def _validated_rows(conn) -> list[dict]:
    return [
        {
            "id": row["id"],
            "number": row["number"],
            "customer": row["customer_name"],
            "channel": CHANNEL[row["channel"]],
            "channel_code": row["channel"],
            "claimed": format_eur(row["total_claimed_cents"]),
            "level_name": LEVEL_NAME[row["level"]],
            "edited": bool(row["edited_by_human"]),
            "subject": row["subject"],
            "body": row["body"],
            "email": row["email"],
            "status": row["status"],
            "status_label": FOLLOWUP_STATUS[row["status"]],
            "handed_over": row["status"] == "handed_over",
            "routes": _routes_for(row),
            "handover": (
                _handover_options(row) if _routes_for(row)["by_email"] else None
            ),
        }
        for row in followup_module.queue(conn, "validated")
    ]



def _handed_over_rows(conn) -> list[dict]:
    """Opened but not confirmed — the ones that may be sitting as drafts."""
    rows = []
    for row in followup_module.queue(conn, "handed_over"):
        rows.append({
            "id": row["id"],
            "number": row["number"],
            "customer": row["customer_name"],
            "level_name": LEVEL_NAME[row["level"]],
            "claimed": format_eur(row["total_claimed_cents"]),
            "email": row["email"],
            "subject": row["subject"],
            "body": row["body"],
            "opened_on": (
                format_date(row["handed_over_at"][:10])
                if row["handed_over_at"] else "—"
            ),
            "routes": _routes_for(row),
            "handover": (
                _handover_options(row) if _routes_for(row)["by_email"] else None
            ),
        })
    return rows


def _skipped_rows(conn) -> list[dict]:
    return [
        {
            "id": row["id"],
            "number": row["number"],
            "customer": row["customer_name"],
            "level_name": LEVEL_NAME[row["level"]],
            "amount": format_eur(row["amount_due_cents"]),
        }
        for row in followup_module.queue(conn, "skipped")
    ]



def _google_banner(context=REAL) -> dict:
    """What the dashboard says about the connected account.

    The mismatch warning is a warning, never a block: it is his account and his
    call. But he has to be able to see it before twenty-three follow-ups leave
    from his personal Gmail.
    """
    # Demonstrations never reveal which real account happens to be configured
    # on this computer. The demo sender is always the fictional garage.
    if context.is_demo:
        return {"connected": False}

    token = google_mail.load_token()
    if not token.connected:
        return {"connected": False}
    return {
        "connected": True,
        "address": token.email,
        "garage_address": GARAGE["email"],
        "mismatch": bool(token.email) and not google_mail.address_matches_garage(
            token.email, GARAGE["email"]
        ),
        "days_left": token.days_left,
        "expiring": token.days_left is not None and token.days_left <= 2,
    }


def build_view(conn, context=DEMO) -> dict:
    reference = today()
    book = engine.portfolio(conn, reference)
    activity = kpi.last_30_days(conn, reference)
    blocked = kpi.unconfirmed_terms(conn, reference)

    buckets = [
        {
            "level": bucket.level,
            "name": LEVEL_NAME[bucket.level],
            "threshold": (
                f"J+{LEVEL_THRESHOLD_DAYS[bucket.level]}" if bucket.level else "—"
            ),
            "count": plural(bucket.count, "facture"),
            "amount": format_eur(bucket.amount_due_cents),
            "extra": format_eur(bucket.extra_cents) if bucket.extra_cents else None,
            "share": (
                round(100 * bucket.amount_due_cents / book.overdue_cents)
                if book.overdue_cents else 0
            ),
        }
        for bucket in book.buckets
    ]

    return {
        "garage": GARAGE,
        "demo_mode": context.is_demo,
        "preview_only": context.preview_only,
        # Always offered: a prospect must see one-click sending, and his
        # screen must not depend on whether this machine has credentials.
        "sending_configured": (
            (not context.is_demo)
            and (
                sending.load_settings().configured
                or google_mail.load_token().connected
            )
        ),
        "google": _google_banner(context),
        "llm_mode": LLM_MODE,
        "today_label": format_date_long(reference),
        "outstanding": format_eur(book.outstanding_cents),
        "overdue_amount": format_eur(book.overdue_cents),
        "overdue_count": book.overdue_count,
        "not_due_amount": format_eur(book.not_due_cents),
        "not_due_count": book.not_due_count,
        "oldest_days": book.oldest_days_overdue,
        "invoice_count": book.invoice_count,
        "settled_share": (
            round(100 * book.settled_count / book.invoice_count) if book.invoice_count else 0
        ),
        "buckets": buckets,
        "queue": _queue_rows(conn, reference),
        "flagged": _flagged_rows(conn),
        "validated": _validated_rows(conn),
        "handed_over": _handed_over_rows(conn),
        "skipped": _skipped_rows(conn),
        "unconfirmed": [
            {
                "name": item.customer_name,
                "kind": CUSTOMER_KIND[item.customer_kind],
                "reference": item.cgv_reference or "Aucune référence au dossier",
                "needs_cgv": item.needs_cgv,
                "needs_kind": item.needs_kind,
                "count": plural(item.invoice_count, "facture échue", "factures échues"),
                "amount": format_eur(item.amount_due_cents),
            }
            for item in blocked
        ],
        "activity": {
            "label": activity.window_label,
            "range": activity.window_range,
            "sent": activity.followups_sent,
            "recovered": format_eur(activity.recovered_cents),
            "recovered_count": plural(
                activity.recovered_count, "facture réglée", "factures réglées"
            ),
            "prepared": activity.drafts_prepared,
            "hours_saved": activity.hours_saved_label,
            "minutes_basis": MINUTES_PER_MANUAL_FOLLOWUP,
        },
        "followups_to_prepare": book.followups_due_count,
    }


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    context, conn = _connect_for(request)
    try:
        view = build_view(conn, context)
    finally:
        conn.close()
    return _template(request, "dashboard.html", context, "dashboard", view=view)


@app.get("/relances", response_class=HTMLResponse)
def reminders(request: Request):
    context, conn = _connect_for(request)
    try:
        view = build_view(conn, context)
    finally:
        conn.close()
    return _template(request, "reminders.html", context, "reminders", view=view)


@app.post("/mode")
def select_mode(
    mode: str = Form(...), next_path: str = Form("/", alias="next")
):
    context = REAL if mode == "real" else DEMO
    if not context.is_demo:
        conn = db_module.connect(context.path)
        try:
            db_module.init_schema(conn)
        finally:
            conn.close()
    response = RedirectResponse(safe_return_path(next_path), status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        cookie_value(context.key),
        httponly=True,
        samesite="strict",
    )
    return response


def _invoice_view(item) -> dict:
    if item.status == "paid":
        timing = "Réglée"
        tone = "ok"
    elif item.days_overdue > 0:
        timing = format_delay(item.days_overdue)
        tone = "stop" if item.level >= 3 else "warn"
    else:
        timing = format_delay(item.days_overdue)
        tone = "neutral"

    if item.pending_followup_id:
        next_action = "Relance à vérifier"
    elif item.followup_is_due:
        next_action = "Relance à préparer"
    elif item.status == "paid":
        next_action = "Aucune action"
    elif item.highest_followup_level:
        next_action = "Suivi déjà engagé"
    else:
        next_action = "Aucune relance nécessaire"

    return {
        "id": item.invoice_id,
        "number": item.number,
        "customer_id": item.customer_id,
        "customer": item.customer_name,
        "email": item.email or "",
        "vehicle": item.vehicle_label or "Véhicule non renseigné",
        "plate": item.plate,
        "job": item.job_label,
        "issue_date": format_date(item.issue_date),
        "due_date": format_date(item.due_date),
        "status": item.status,
        "status_label": INVOICE_STATUS[item.status],
        "total": format_eur(item.total_ttc_cents),
        "paid": format_eur(item.amount_paid_cents),
        "remaining": format_eur(item.amount_due_cents),
        "days_overdue": item.days_overdue,
        "timing": timing,
        "tone": tone,
        "level": item.level,
        "level_label": "Aucune relance — facture réglée" if item.status == "paid" else LEVEL_NAME[item.level],
        "pending_followup_id": item.pending_followup_id,
        "followup_is_due": item.followup_is_due,
        "next_action": next_action,
        "penalty_blocked": item.penalty_blocked,
    }


@app.get("/factures", response_class=HTMLResponse)
def invoices_page(request: Request, etat: str = "toutes"):
    context, conn = _connect_for(request)
    try:
        items = engine.load_all(conn, today(), include_paid=True)
        rows = [_invoice_view(item) for item in items]
    finally:
        conn.close()

    allowed = {"toutes", "en_retard", "a_venir", "reglees"}
    etat = etat if etat in allowed else "toutes"
    if etat == "en_retard":
        shown = [row for row in rows if row["status"] != "paid" and row["days_overdue"] > 0]
    elif etat == "a_venir":
        shown = [row for row in rows if row["status"] != "paid" and row["days_overdue"] <= 0]
    elif etat == "reglees":
        shown = [row for row in rows if row["status"] == "paid"]
    else:
        shown = rows

    shown.sort(
        key=lambda row: (
            row["status"] == "paid",
            -(row["days_overdue"] if row["days_overdue"] > 0 else -10_000),
            row["number"],
        )
    )
    summary = {
        "all": len(rows),
        "overdue": sum(1 for row in rows if row["status"] != "paid" and row["days_overdue"] > 0),
        "upcoming": sum(1 for row in rows if row["status"] != "paid" and row["days_overdue"] <= 0),
        "paid": sum(1 for row in rows if row["status"] == "paid"),
        "selected": etat,
    }
    return _template(
        request, "invoices.html", context, "invoices",
        invoices=shown, summary=summary,
    )


@app.get("/factures/{invoice_id}", response_class=HTMLResponse)
def invoice_page(request: Request, invoice_id: int):
    context, conn = _connect_for(request)
    try:
        item = next(
            (
                candidate for candidate in engine.load_all(
                    conn, today(), include_paid=True
                )
                if candidate.invoice_id == invoice_id
            ),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Facture introuvable.")
        invoice = _invoice_view(item)
        stored = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        invoice["payment_version"] = manual.payment_version(conn, stored)
        invoice["today_iso"] = today().isoformat()
        payments = [
            {"before": format_eur(r["previous_paid_cents"]), "after": format_eur(r["amount_paid_cents"]),
             "date": format_date(r["created_at"][:10]), "payment_date": format_date(r["payment_date"]) if r["payment_date"] else "",
             "note": r["note"]}
            for r in conn.execute("SELECT * FROM payment_changes WHERE invoice_id=? ORDER BY id DESC", (invoice_id,))
        ]
        history = [
            {
                "id": row["id"],
                "level": row["level"],
                "level_label": LEVEL_NAME[row["level"]],
                "status": row["status"],
                "status_label": FOLLOWUP_STATUS.get(row["status"], "Bloquée"),
                "subject": row["subject"],
                "body": row["body"],
                "created_on": format_date(row["created_at"][:10]),
            }
            for row in conn.execute(
                "SELECT * FROM followups WHERE invoice_id = ? "
                "ORDER BY level DESC, id DESC",
                (invoice_id,),
            )
        ]
        invoice["validated_followup_id"] = next((r["id"] for r in history if r["status"] == "validated"), None)
    finally:
        conn.close()
    return _template(
        request, "invoice_detail.html", context, "invoices",
        invoice=invoice, followups=history, payments=payments,
    )


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    context, conn = _connect_for(request)
    try:
        rows = conn.execute(
            """
            SELECT c.*,
                   COUNT(i.id) AS invoice_count,
                   SUM(CASE WHEN i.status != 'paid' THEN 1 ELSE 0 END) AS open_count,
                   COALESCE(SUM(CASE WHEN i.status != 'paid'
                       THEN MAX(i.total_ttc_cents - i.amount_paid_cents, 0)
                       ELSE 0 END), 0) AS outstanding_cents
            FROM customers c
            LEFT JOIN invoices i ON i.customer_id = c.id
            GROUP BY c.id
            ORDER BY c.name COLLATE NOCASE
            """
        ).fetchall()
        clients = [
            {
                "id": row["id"],
                "name": row["name"],
                "kind": CUSTOMER_KIND[row["kind"]],
                "kind_confirmed": bool(row["kind_confirmed"]),
                "email": row["email"] or "Non renseignée",
                "phone": row["phone"] or "Non renseigné",
                "address": " ".join(
                    part for part in (
                        row["address"], row["postal_code"], row["city"]
                    ) if part
                ) or "Non renseignée",
                "invoice_count": row["invoice_count"],
                "open_count": row["open_count"],
                "outstanding": format_eur(row["outstanding_cents"]),
                "terms_confirmed": bool(row["penalty_terms_confirmed"]),
            }
            for row in rows
        ]
    finally:
        conn.close()
    return _template(
        request, "clients.html", context, "clients", clients=clients,
    )


# ---------------------------------------------------------------------------
# Actions — nothing here sends anything
# ---------------------------------------------------------------------------

class EditPayload(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12_000)


def _fetch(conn, followup_id: int):
    row = conn.execute(
        "SELECT * FROM followups WHERE id = ?", (followup_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Relance introuvable")
    return row


def _require_invoice_due(conn, followup_row) -> None:
    """Never hand off a reminder whose invoice is now settled."""
    invoice = conn.execute(
        "SELECT number, status, total_ttc_cents, amount_paid_cents "
        "FROM invoices WHERE id = ?",
        (followup_row["invoice_id"],),
    ).fetchone()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    if (
        invoice["status"] == "paid"
        or invoice["amount_paid_cents"] >= invoice["total_ttc_cents"]
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"La facture {invoice['number']} est réglée. Cette relance ne "
                "doit plus être remise ni envoyée. Rechargez la page."
            ),
        )


def _set_status(
    request: Request,
    followup_id: int,
    status: str,
    allowed_from: tuple[str, ...],
) -> dict:
    _require_rendered_context(request)
    _context_value, conn = _connect_for(request)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch(conn, followup_id)
        if row["status"] == status:
            conn.rollback()
            return {"ok": True, "status": status, "unchanged": True}
        if row["status"] not in allowed_from:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cette relance a changé d’état. Rechargez la page pour voir "
                    "sa situation actuelle avant de continuer."
                ),
            )
        if status == "validated":
            _require_invoice_due(conn, row)
        conn.execute(
            "UPDATE followups SET status = ?, decided_at = ? WHERE id = ?",
            (status, datetime.now().isoformat(timespec="seconds"), followup_id),
        )
        conn.commit()
        return {"ok": True, "status": status}
    finally:
        conn.close()


@app.post("/api/followups/{followup_id}/validate")
def validate(request: Request, followup_id: int):
    """Approve a message. In demo mode this never sends: it queues a preview."""
    context = _context(request)
    result = _set_status(request, followup_id, "validated", ("pending",))
    result["preview_only"] = context.preview_only
    return JSONResponse(result)


@app.post("/api/followups/{followup_id}/skip")
def skip(request: Request, followup_id: int):
    """Drop this level for good. The system will not ask again."""
    return JSONResponse(
        _set_status(request, followup_id, "skipped", ("pending",))
    )


@app.post("/api/followups/{followup_id}/edit")
def edit(request: Request, followup_id: int, payload: EditPayload):
    """Save an edited message and validate it.

    The guardrail is re-run, but it does not block here: the owner is the
    authority on his own words. If his edit introduces a figure that was not in
    the data, he is told — and left to decide.
    """
    _require_rendered_context(request)
    data_context, conn = _connect_for(request)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch(conn, followup_id)
        if row["status"] != "pending":
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="Seule une relance encore à vérifier peut être modifiée.",
            )
        _require_invoice_due(conn, row)
        subject = payload.subject.strip()
        body = payload.body.strip()
        if not subject or not body or "\r" in subject or "\n" in subject:
            conn.rollback()
            raise HTTPException(
                status_code=422,
                detail=(
                    "L’objet et le message ne peuvent pas être vides, et "
                    "l’objet doit tenir sur une seule ligne."
                ),
            )
        invoice = conn.execute(
            "SELECT number FROM invoices WHERE id = ?", (row["invoice_id"],)
        ).fetchone()

        warning = None
        matches = [
            item for item in engine.load_all(conn, today())
            if item.invoice_id == row["invoice_id"]
        ]
        if matches:
            item = matches[0]
            draft_context = build_context(item, item.customer_address_line, today())
            report = verify_draft(subject, body, draft_context.sources())
            if not report.ok:
                warning = (
                    "Votre version contient des chiffres absents des données de "
                    f"la facture : {', '.join(report.unknown)}. Le message est "
                    "enregistré tel quel — vérifiez avant envoi."
                )

        conn.execute(
            """
            UPDATE followups
            SET subject = ?, body = ?, edited_by_human = 1,
                status = 'validated', decided_at = ?
            WHERE id = ?
            """,
            (subject, body,
             datetime.now().isoformat(timespec="seconds"), followup_id),
        )
        conn.commit()
        return JSONResponse({
            "ok": True,
            "status": "validated",
            "warning": warning,
            "number": invoice["number"] if invoice else "",
            "preview_only": data_context.preview_only,
        })
    finally:
        conn.close()


@app.get("/relances/{followup_id}/message.eml")
def download_eml(request: Request, followup_id: int):
    """Hand the message to the owner's own mail client.

    This is not sending. The file is a draft; the owner opens it, reads it, and
    presses Send himself — from his address, into his sent folder. The system
    holds no mailbox credential and never will.
    """
    _require_rendered_context(request)
    _data_context, conn = _connect_for(request)
    try:
        row = _fetch(conn, followup_id)
        if row["status"] not in ("validated", "handed_over"):
            raise HTTPException(
                status_code=400,
                detail="Ce message n’a pas encore été validé.",
            )
        _require_invoice_due(conn, row)
        if row["channel"] != "email":
            raise HTTPException(
                status_code=400,
                detail="Une mise en demeure part par courrier recommandé, "
                       "pas par e-mail. Utilisez le bouton Imprimer.",
            )
        recipient = conn.execute(
            """
            SELECT c.email, i.number
            FROM invoices i JOIN customers c ON c.id = i.customer_id
            WHERE i.id = ?
            """,
            (row["invoice_id"],),
        ).fetchone()
        if not (recipient and is_valid_email(recipient["email"])):
            raise HTTPException(
                status_code=400,
                detail="Ce client n’a pas d’adresse e-mail : seule la voie "
                       "postale est possible.",
            )

        payload = build_eml(
            recipient["email"], row["subject"], row["body"],
            reply_to=GARAGE["email"],
        )
        return Response(
            content=payload,
            media_type="message/rfc822",
            headers=eml_response_headers(recipient["number"]),
        )
    finally:
        conn.close()


@app.post("/api/followups/{followup_id}/remis")
def mark_handed_over(request: Request, followup_id: int):
    """The .eml was downloaded. Nothing more is claimed than that."""
    _require_rendered_context(request)
    _data_context, conn = _connect_for(request)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch(conn, followup_id)
        if row["status"] == "sent":
            conn.rollback()
            return JSONResponse({"ok": True, "status": "sent", "unchanged": True})
        if row["status"] not in ("validated", "handed_over"):
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="Validez cette relance avant de l’ouvrir dans votre messagerie.",
            )
        _require_invoice_due(conn, row)

        # Opening the same message twice leaves two identical drafts in the
        # owner's mailbox. We cannot clean it up for him, so we say so first.
        already = row["handed_over_at"]
        conn.execute(
            "UPDATE followups SET status = 'handed_over', handed_over_at = ? "
            "WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), followup_id),
        )
        conn.commit()
        return JSONResponse({
            "ok": True,
            "status": "handed_over",
            "repeat": bool(already),
            "previous": format_date(already[:10]) if already else None,
        })
    finally:
        conn.close()


@app.post("/api/followups/{followup_id}/envoye")
def confirm_sent(request: Request, followup_id: int):
    """The owner says he pressed Send. Only this counts as sent."""
    _require_rendered_context(request)
    _data_context, conn = _connect_for(request)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch(conn, followup_id)
        if row["status"] == "sent":
            conn.rollback()
            return JSONResponse({"ok": True, "status": "sent", "unchanged": True})
        if row["status"] != "handed_over":
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Confirmez l’envoi seulement après avoir ouvert cette relance "
                    "dans votre messagerie ou l’avoir imprimée."
                ),
            )
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE followups SET status = 'sent', sent_at = ? WHERE id = ?",
            (now, followup_id),
        )
        conn.commit()
        return JSONResponse({"ok": True, "status": "sent"})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Optional route: sending for real, over SMTP
# ---------------------------------------------------------------------------

def _settings_view(request: Request, settings=None, message=None, error=None):
    import json as _json

    context = _context(request)
    if context.is_demo:
        # Never expose a real account address, token lifetime or saved sender
        # while somebody is watching a demonstration.
        settings = sending.SmtpSettings()
        token = google_mail.GoogleToken()
    else:
        settings = settings or sending.load_settings()
        token = google_mail.load_token()
    return _template(
        request, "settings_smtp.html", context, "settings",
        **{
            "settings": settings,
            "providers": sending.PROVIDERS,
            "presets_json": _json.dumps(sending.PROVIDERS, ensure_ascii=False),
            "demo_mode": context.is_demo,
            "preview_only": context.preview_only,
            "message": message,
            "error": error,
            "google": token,
            "google_client_ready": (
                not context.is_demo and google_mail.load_client() is not None
            ),
            "google_days_left": token.days_left,
            "google_expires_on": (
                format_date(token.expires_on) if token.expires_on else None
            ),
            "google_address": token.email,
            "google_mismatch": (
                bool(token.connected and token.email)
                and not google_mail.address_matches_garage(
                    token.email, GARAGE["email"]
                )
            ),
            "garage_address": GARAGE["email"],
        },
    )


@app.get("/parametres/envoi/brouillon-test", response_class=HTMLResponse)
def gmail_draft_test(request: Request):
    """Explicit opt-in screen: reveal the account, never customer data.

    No ledger is opened or changed and no email API is called. The owner
    reviews the message before deliberately opening Gmail in a new tab.
    """
    token = google_mail.load_token()
    message = (test_message(token.email)
               if token.connected and is_valid_email(token.email) else None)
    return _template(
        request, "gmail_test.html", _context(request), "settings", test=message,
    )


@app.get("/relances/{followup_id}/brouillon-test", response_class=HTMLResponse)
def reminder_gmail_test(request: Request, followup_id: int):
    context = _require_rendered_context(request)
    if not context.is_demo:
        raise HTTPException(403, "Ce test utilise uniquement des factures de démonstration. Repassez en démonstration.")
    _, conn = _connect_for(request)
    try:
        row = _fetch(conn, followup_id)
        if row["status"] != "validated" or row["channel"] != "email":
            raise HTTPException(409, "Vérifiez et validez d’abord une relance par e-mail. Les courriers recommandés restent des lettres.")
        _require_invoice_due(conn, row)
        token = google_mail.load_token()
        number = conn.execute("SELECT number FROM invoices WHERE id=?", (row["invoice_id"],)).fetchone()[0]
        message = reminder_test_message(token.email, row["subject"], row["body"], number) if token.connected and is_valid_email(token.email) else None
        return _template(request, "gmail_test.html", context, "reminders", test=message)
    finally:
        conn.close()


def _require_real_context(request: Request):
    context = _context(request)
    if context.is_demo:
        raise HTTPException(
            status_code=409,
            detail=(
                "Les réglages réels sont masqués en démonstration. Passez aux "
                "données réelles pour les modifier."
            ),
        )
    return context


def _settings_from(form, current) -> sending.SmtpSettings:
    """Read the form. An empty password keeps the stored one."""
    try:
        port = int(str(form.get("port") or current.port).strip())
    except ValueError:
        port = current.port
    # App passwords are shown in groups of four; the spaces are display only.
    password = (form.get("password") or "").replace(" ", "").strip()

    provider = (form.get("provider") or current.provider).strip()
    if provider not in sending.PROVIDERS:
        provider = "autre"
    security = (form.get("security") or "starttls").strip()
    if security not in ("starttls", "ssl"):
        security = "starttls"

    return sending.SmtpSettings(
        provider=provider,
        sender=(form.get("sender") or "").strip(),
        sender_name=(form.get("sender_name") or "").strip(),
        host=(form.get("host") or "").strip(),
        port=port,
        security=security,
        username=(form.get("username") or "").strip(),
        password=password or current.password,
    )


@app.get("/parametres/envoi", response_class=HTMLResponse)
def sending_settings(request: Request, etat: str | None = None):
    messages = {
        "enregistre": "Paramètres enregistrés.",
        "test_ok": "Message de test envoyé à votre propre adresse.",
        "supprime": "Identifiants supprimés de cet ordinateur.",
        "google_off": "Messagerie Google déconnectée.",
        "demo_reset": "Démonstration réinitialisée. Les données réelles sont intactes.",
    }
    return _settings_view(request, message=messages.get(etat or ""))


@app.post("/parametres/envoi", response_class=HTMLResponse)
async def save_sending_settings(request: Request):
    _require_real_context(request)
    form = await request.form()
    settings = _settings_from(form, sending.load_settings())
    problem = _settings_validation(settings)
    if problem:
        return _settings_view(request, settings, error=problem)
    sending.save_settings(settings)
    return RedirectResponse("/parametres/envoi?etat=enregistre", status_code=303)


@app.post("/parametres/envoi/test", response_class=HTMLResponse)
async def test_sending(request: Request):
    _require_real_context(request)
    form = await request.form()
    settings = _settings_from(form, sending.load_settings())
    problem = _settings_validation(settings)
    if problem:
        return _settings_view(request, settings, error=problem)
    sending.save_settings(settings)
    try:
        sending.send_test(settings)
    except sending.SendingProblem as problem:
        return _settings_view(request, settings, error=problem.reason)
    return RedirectResponse("/parametres/envoi?etat=test_ok", status_code=303)


@app.post("/parametres/envoi/supprimer", response_class=HTMLResponse)
def delete_sending_settings(request: Request):
    _require_real_context(request)
    sending.clear_settings()
    return RedirectResponse("/parametres/envoi?etat=supprime", status_code=303)


@app.post("/api/followups/{followup_id}/envoyer")
def send_now(request: Request, followup_id: int):
    """Send a validated follow-up for real.

    The human gate is here, in code: a message that the owner has not validated
    cannot be sent, whatever the caller asks for. A formal demand is never an
    e-mail. And in demo mode `sending.send` refuses before opening a socket.
    """
    _require_rendered_context(request)
    data_context, conn = _connect_for(request)
    try:
        # The writer lock spans the send. A second click/request cannot pass the
        # status check until this one has either committed or failed, which
        # prevents two concurrent sends from leaving the machine.
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch(conn, followup_id)

        if row["status"] != "validated":
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cette relance n’est plus prête à envoyer. Rechargez la page "
                    "pour vérifier son état."
                ),
            )
        _require_invoice_due(conn, row)
        if row["channel"] != "email":
            conn.rollback()
            raise HTTPException(
                status_code=400,
                detail="Une mise en demeure part par courrier recommandé, "
                       "jamais par e-mail.",
            )

        recipient = conn.execute(
            "SELECT c.email FROM invoices i JOIN customers c ON c.id = i.customer_id "
            "WHERE i.id = ?",
            (row["invoice_id"],),
        ).fetchone()
        address = recipient["email"] if recipient else ""

        # No recipient, no send — including in demo mode. Marking a follow-up
        # "sent" to an empty address would be a lie on screen, and the counters
        # would carry it.
        if not is_valid_email(address):
            conn.rollback()
            raise HTTPException(status_code=400, detail=NO_EMAIL_REASON)

        settings = sending.load_settings()

        if data_context.preview_only:
            # The brief has always been "no real sending, show a preview" — a
            # preview, not an absence. The owner sees the complete message,
            # while the queue and counters still accurately say it is unsent.
            # No socket is opened on this path: `sending.send` is never called.
            # With no credentials on this machine — a prospect's laptop, say —
            # the preview still shows a realistic sender: the garage's own.
            shown_as = sending.SmtpSettings(
                sender=GARAGE["email"], sender_name=GARAGE["name"]
            )
            if not data_context.is_demo:
                connected = google_mail.load_token()
                if connected.connected and connected.email:
                    shown_as = sending.SmtpSettings(
                        sender=connected.email, sender_name=GARAGE["name"]
                    )
                elif settings.sender:
                    shown_as = settings
            shown = sending.preview(shown_as, address, row["subject"], row["body"])
            # Looking at a message is not sending it, even in a rehearsal.
            # Keep both the queue and sent counters truthful in every context.
            conn.rollback()
            status = "validated"
            return JSONResponse({
                "ok": True, "status": status, "to": address,
                "demo": data_context.is_demo,
                "preview_only": True,
                "preview": shown,
            })

        # Three routes, the owner's choice. A connected Google account wins,
        # because it is the one that took a single click to set up.
        google = google_mail.load_token()
        if google.connected:
            try:
                google_mail.send(address, row["subject"], row["body"])
            except google_mail.GoogleProblem as problem:
                # Expiry is normal in testing mode. Whatever the cause, the
                # follow-up stays validated and unsent — never marked sent.
                conn.rollback()
                return JSONResponse(
                    {
                        "ok": False,
                        "detail": problem.reason,
                        "reconnect": problem.expired,
                        "reconnect_url": "/parametres/envoi/google/connecter",
                    },
                    status_code=400,
                )
        else:
            try:
                sending.send(settings, address, row["subject"], row["body"])
            except sending.SendingProblem as problem:
                # The follow-up stays validated and unsent. Never marked sent on
                # a failure, and never silently.
                conn.rollback()
                return JSONResponse(
                    {"ok": False, "detail": problem.reason}, status_code=400
                )

        conn.execute(
            "UPDATE followups SET status = 'sent', sent_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), followup_id),
        )
        conn.commit()
        return JSONResponse({"ok": True, "status": "sent", "to": address,
                             "demo": False})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Connecting a Google account, send-only
# ---------------------------------------------------------------------------

@app.get("/parametres/envoi/google/connecter")
def google_connect(request: Request):
    """Send the owner to Google to pick his account.

    One click. The loopback listener started here catches the answer and lands
    him back on this page.
    """
    _require_real_context(request)
    back = str(request.base_url).rstrip("/") + "/parametres/envoi"
    try:
        destination = google_mail.start_flow(back)
    except google_mail.GoogleProblem as problem:
        return _settings_view(
            request, sending.load_settings(), error=problem.reason
        )
    return RedirectResponse(destination, status_code=303)


@app.post("/parametres/envoi/google/deconnecter", response_class=HTMLResponse)
def google_disconnect(request: Request):
    _require_real_context(request)
    google_mail.disconnect()
    return RedirectResponse("/parametres/envoi?etat=google_off", status_code=303)


@app.post("/api/followups/{followup_id}/reopen")
def reopen(request: Request, followup_id: int):
    """Put a decided follow-up back in the queue.

    Undo, for a misclick or a change of mind — and what makes the demo
    rehearsable without regenerating the whole database.
    """
    _require_rendered_context(request)
    _data_context, conn = _connect_for(request)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch(conn, followup_id)
        if row["status"] not in ("validated", "skipped", "handed_over"):
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="Cette relance ne peut pas être remise en attente.",
            )
        conn.execute(
            "UPDATE followups SET status = 'pending', decided_at = NULL, "
            "handed_over_at = NULL WHERE id = ?",
            (followup_id,),
        )
        conn.commit()
        return JSONResponse({"ok": True, "status": "pending"})
    finally:
        conn.close()


@app.get("/relances/{followup_id}/imprimer", response_class=HTMLResponse)
def print_letter(request: Request, followup_id: int):
    """A printable sheet for a formal demand.

    The brief asks for the mise en demeure to be ready to print: this is the
    page that comes out of the printer, folded into an envelope and sent
    recommandé avec accusé de réception.
    """
    _require_rendered_context(request)
    context, conn = _connect_for(request)
    try:
        row = _fetch(conn, followup_id)
        if row["status"] not in ("validated", "handed_over", "sent"):
            raise HTTPException(
                status_code=409,
                detail="Validez cette relance avant de l’imprimer.",
            )
        if row["status"] != "sent":
            _require_invoice_due(conn, row)
        invoice = conn.execute(
            """
            SELECT i.number, c.name AS customer_name
            FROM invoices i JOIN customers c ON c.id = i.customer_id
            WHERE i.id = ?
            """,
            (row["invoice_id"],),
        ).fetchone()

        letter = {
            "number": invoice["number"],
            "customer": invoice["customer_name"],
            "level_name": LEVEL_NAME[row["level"]],
            "channel": CHANNEL[row["channel"]],
            "subject": row["subject"],
            "body": row["body"],
            "garage_name": GARAGE["name"],
            "garage_address": (
                f"{GARAGE['address']}, {GARAGE['postal_code']} {GARAGE['city']}"
            ),
            "garage_siret": GARAGE["siret"],
            "garage_vat": GARAGE["vat_number"],
            "garage_rcs": GARAGE["rcs"],
            "printed_on": format_date(today()),
        }
        return templates.TemplateResponse(
            request, "letter.html", {"letter": letter, "data_context": context}
        )
    finally:
        conn.close()


@app.post("/api/demo/guardrail")
def demo_guardrail(request: Request):
    """Put one deliberately wrong message through the pipeline.

    A sales aid: the mock engine never invents a figure, so the flagged section
    would otherwise never appear. This runs a single real invoice through a
    backend that makes numbers up, and files the result exactly as the system
    would file a bad draft from the API — rejected, never queued.
    """
    _require_rendered_context(request)
    context, conn = _connect_for(request)
    try:
        if not context.is_demo:
            raise HTTPException(
                status_code=409,
                detail="Cet outil est réservé aux données fictives de démonstration.",
            )
        reference = today()
        candidates = [
            item for item in engine.load_all(conn, reference)
            if item.level >= 2 and item.pending_followup_id
        ]
        if not candidates:
            raise HTTPException(
                status_code=400,
                detail="Aucune relance en attente sur laquelle faire la démonstration.",
            )
        candidates.sort(key=lambda item: -item.amount_due_cents)
        item = candidates[0]

        def inventing(context):
            return (
                f"Facture {context.invoice_number} — régularisation",
                "Après recalcul de votre dossier, le solde exigible s’élève à "
                "9 876,54 €, à régler avant le 31/12/2099 sur le compte "
                "FR76 1111 2222 3333. Des pénalités de 22 % l’an courent depuis "
                "l’échéance.",
            )

        # Replace the honest pending draft with the bad one, so the queue count
        # and the flagged count stay consistent with each other.
        conn.execute("DELETE FROM followups WHERE id = ?", (item.pending_followup_id,))
        conn.commit()
        followup_module.create_drafts(conn, reference, limit=None, backend=inventing)
        return JSONResponse({"ok": True, "number": item.number})
    finally:
        conn.close()


@app.post("/api/drafts/refresh")
def refresh_drafts(request: Request):
    """Redraft today's follow-ups from scratch. Used to replay the demo."""
    _require_rendered_context(request)
    context, conn = _connect_for(request)
    try:
        if not context.is_demo:
            raise HTTPException(
                status_code=409,
                detail="La régénération complète est réservée à la démonstration.",
            )
        followup_module.clear_drafts(conn)
        run = followup_module.create_drafts(conn, today(), mode="mock")
        return JSONResponse({
            "ok": True,
            "queued": len(run.queued),
            "rejected": len(run.rejected),
        })
    finally:
        conn.close()


@app.post("/relances/preparer")
def prepare_drafts(
    request: Request,
    next_path: str = Form("/relances", alias="next"),
    expected_context: str = Form(..., alias="contexte"),
    invoice_id: int | None = Form(None),
):
    _require_rendered_context(request, expected_context)
    context, conn = _connect_for(request)
    try:
        if invoice_id is not None and not conn.execute("SELECT id FROM invoices WHERE id=?", (invoice_id,)).fetchone():
            raise HTTPException(404, "Facture introuvable.")
        run = followup_module.create_drafts(
            conn,
            today(),
            mode="mock" if context.preview_only else None,
            invoice_id=invoice_id,
        )
    except HTTPException:
        raise
    except Exception as error:  # the import/data stay intact on drafting failure
        logger.exception("Draft preparation failed")
        target = safe_return_path(next_path, "/relances")
        separator = "&" if "?" in target else "?"
        return RedirectResponse(
            f"{target}{separator}{urlencode({'erreur': 'Les relances n’ont pas pu être préparées. Réessayez.'})}",
            status_code=303,
        )
    finally:
        conn.close()
    target = safe_return_path(next_path, "/relances")
    separator = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{separator}{urlencode({'preparees': len(run.queued), 'bloquees': len(run.rejected)})}",
        status_code=303,
    )


def _settings_validation(settings: sending.SmtpSettings) -> str | None:
    if not is_valid_email(settings.sender):
        return "Saisissez une adresse d’envoi valide."
    if not settings.host or len(settings.host) > 255 or any(
        character.isspace() for character in settings.host
    ):
        return "Le serveur de messagerie n’est pas valide."
    if not 1 <= settings.port <= 65_535:
        return "Le port doit être compris entre 1 et 65 535."
    return None


@app.post("/parametres/demo/reinitialiser")
def reset_demo_route(
    request: Request,
    expected_context: str = Form(..., alias="contexte"),
):
    context = _require_rendered_context(request, expected_context)
    if not context.is_demo:
        raise HTTPException(
            status_code=409,
            detail="La réinitialisation est disponible uniquement en démonstration.",
        )
    reset_demo(today())
    return RedirectResponse("/parametres/envoi?etat=demo_reset", status_code=303)
