"""The import screens: load a file, say what its columns mean.

Nothing here writes to a database. A loaded file lives in memory, under a
token, until the owner confirms — which is the next step. The demo ledger is
never touched by any of this: real invoices go to `REAL_DB_PATH`.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db as db_module
from . import followups as followup_module
from .config import (
    DEFAULT_PAYMENT_TERM_DAYS, DEMO_MODE, PAYMENT_TERM_CHOICES, PROFILES_PATH,
    REAL_DB_PATH, today,
)
from .data_context import from_request, template_context
from .importer.convention import detect_amount_convention
from .importer.mapping import (
    FIELDS, FIELDS_BY_KEY, ProfileStore, check_mapping, guess_mapping,
)
from .importer import reconcile, runner
from .importer.reader import FileProblem, read_bytes
from .labels_fr import format_date
from .money import format_eur

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()
profiles = ProfileStore(PROFILES_PATH)

# A file the owner has loaded but not yet confirmed. Kept in memory on purpose:
# an abandoned import must leave nothing behind.
MAX_HELD_FILES = 5
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
HELD_FILE_LIFETIME = timedelta(minutes=30)


@dataclass
class HeldFile:
    token: str
    source: object
    loaded_at: datetime
    mapping: dict[str, str | None]
    profile_name: str | None = None
    profile_match: str = "aucun"
    payment_term_days: int = DEFAULT_PAYMENT_TERM_DAYS


@dataclass
class CompletedImport:
    source: object
    plan: object
    result: object
    payment_term_days: int
    completed_at: datetime


_HELD: dict[str, HeldFile] = {}
_COMPLETED: dict[str, CompletedImport] = {}
_IMPORT_LOCK = threading.Lock()


def _require_real(request: Request):
    context = from_request(request)
    if context.is_demo:
        raise HTTPException(
            status_code=409,
            detail=(
                "Passez aux données réelles avant d’importer. La démonstration "
                "reste ainsi entièrement fictive."
            ),
        )
    return context


def _page_context(request: Request, context, **values) -> dict:
    payload = template_context(request, context, "import")
    # Mapping/result URLs only exist in real-data mode. Switching to Demo from
    # one of them must land on the safe import entry page, not bounce into a
    # real-only URL and show an error.
    payload["shell"]["return_path"] = "/import"
    payload.update(values)
    return payload


def _cleanup_expired() -> None:
    """Forget abandoned and completed files after the short working window."""
    cutoff = datetime.now() - HELD_FILE_LIFETIME
    for token, held in list(_HELD.items()):
        if held.loaded_at < cutoff:
            _HELD.pop(token, None)
    for token, completed in list(_COMPLETED.items()):
        if completed.completed_at < cutoff:
            _COMPLETED.pop(token, None)


def _hold(source) -> HeldFile:
    _cleanup_expired()
    while len(_HELD) >= MAX_HELD_FILES:
        oldest = min(_HELD.values(), key=lambda held: held.loaded_at)
        _HELD.pop(oldest.token, None)

    token = uuid.uuid4().hex[:12]
    held = HeldFile(token=token, source=source, loaded_at=datetime.now(), mapping={})
    _HELD[token] = held
    return held


def _term_from(form, fallback: int) -> int:
    """The answer to « payables sous quel délai ? », validated against the list."""
    raw = form.get("delai_paiement")
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    allowed = {days for days, _label in PAYMENT_TERM_CHOICES}
    return value if value in allowed else fallback


def _get(token: str) -> HeldFile:
    _cleanup_expired()
    held = _HELD.get(token)
    if held is not None and datetime.now() - held.loaded_at > HELD_FILE_LIFETIME:
        _HELD.pop(token, None)
        held = None
    if held is None:
        raise HTTPException(
            status_code=404,
            detail="Fichier expiré ou déjà fermé. Rechargez-le pour recommencer.",
        )
    return held


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------

def _column_facts(source) -> dict[str, dict]:
    """Per column: a few sample values, and what its separators mean."""
    facts = {}
    for index, header in enumerate(source.headers):
        values = [row[index] for row in source.rows if index < len(row)]
        filled = [value for value in values if value]
        convention = detect_amount_convention(values)
        facts[header] = {
            "samples": filled[:3],
            "filled": len(filled),
            "total": len(values),
            "convention": convention.evidence if convention.sample_count else "",
            "blocks": convention.blocks_import,
        }
    return facts


def _mapping_view(request: Request, held: HeldFile, message: str | None = None,
                  error: str | None = None) -> HTMLResponse:
    context = _require_real(request)
    source = held.source
    suggestions = guess_mapping(source)

    if not held.mapping:
        held.mapping = {key: item.header for key, item in suggestions.items()}

    facts = _column_facts(source)
    rows = []
    for item in FIELDS:
        suggestion = suggestions.get(item.key)
        rows.append({
            "key": item.key,
            "label": item.label,
            "required": item.required,
            "kind": item.kind,
            "hint": item.hint,
            "selected": held.mapping.get(item.key) or "",
            "suggested": suggestion.header if suggestion else None,
            "reason": suggestion.reason if suggestion else "",
        })

    check = check_mapping(held.mapping, source.headers)
    blocking = [header for header, fact in facts.items() if fact["blocks"]]

    return templates.TemplateResponse(
        request,
        "import_mapping.html",
        _page_context(request, context, **{
            "token": held.token,
            "source": source,
            "fields": rows,
            "headers": source.headers,
            "facts": facts,
            "check": check,
            "blocking": blocking,
            "message": message,
            "error": error,
            "profile_name": held.profile_name,
            "profile_match": held.profile_match,
            "profiles": profiles.load(),
            "needs_term": not held.mapping.get("due_date"),
            "term_choices": PAYMENT_TERM_CHOICES,
            "term_days": held.payment_term_days,
        }),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/import", response_class=HTMLResponse)
def upload_form(request: Request, erreur: str | None = None):
    context = from_request(request)
    return templates.TemplateResponse(
        request,
        "import_upload.html",
        _page_context(request, context, **{
            "profiles": profiles.load(),
            "error": erreur,
            "can_import": not context.is_demo,
        }),
    )


@router.post("/import/analyser")
async def analyse(request: Request, fichier: UploadFile = File(...)):
    _require_real(request)
    # Bound memory before parsing. Reading the entire request and checking its
    # size afterwards would let an oversized upload consume arbitrary memory.
    data = await fichier.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        return RedirectResponse("/import?erreur=Fichier+vide.", status_code=303)
    if len(data) > MAX_UPLOAD_BYTES:
        return RedirectResponse(
            "/import?erreur=Fichier+trop+volumineux+%28max+12+Mo%29.", status_code=303
        )

    try:
        source = read_bytes(data, fichier.filename or "fichier.csv")
    except FileProblem as error:
        from urllib.parse import quote

        return RedirectResponse(f"/import?erreur={quote(error.reason)}", status_code=303)

    held = _hold(source)

    # A file from software already mapped once needs no mapping at all.
    profile, match = profiles.match(source.headers)
    if profile is not None:
        held.mapping = {
            item.key: profile.mapping.get(item.key) for item in FIELDS
        }
        held.profile_name = profile.name
        held.profile_match = match
        if profile.payment_term_days is not None:
            held.payment_term_days = profile.payment_term_days
        profiles.record_use(profile.name)

    return RedirectResponse(f"/import/{held.token}", status_code=303)


@router.get("/import/{token}", response_class=HTMLResponse)
def mapping_screen(request: Request, token: str, message: str | None = None):
    return _mapping_view(request, _get(token), message=message)


@router.post("/import/{token}/mapping", response_class=HTMLResponse)
async def save_mapping(request: Request, token: str):
    _require_real(request)
    held = _get(token)
    form = await request.form()

    held.mapping = {
        item.key: (form.get(f"champ_{item.key}") or None) for item in FIELDS
    }
    held.payment_term_days = _term_from(form, held.payment_term_days)
    check = check_mapping(held.mapping, held.source.headers)
    if not check.ok:
        return _mapping_view(request, held, error=check.message)

    return RedirectResponse(f"/import/{token}/recapitulatif", status_code=303)


@router.post("/import/{token}/profil", response_class=HTMLResponse)
async def save_profile(request: Request, token: str, nom: str = Form(...)):
    _require_real(request)
    held = _get(token)
    form = await request.form()

    held.mapping = {
        item.key: (form.get(f"champ_{item.key}") or None) for item in FIELDS
    }
    held.payment_term_days = _term_from(form, held.payment_term_days)
    check = check_mapping(held.mapping, held.source.headers)
    if not check.ok:
        return _mapping_view(request, held, error=check.message)

    name = nom.strip()
    if not name:
        return _mapping_view(request, held, error="Donnez un nom au profil.")

    profiles.save(name, held.source.headers, held.mapping, held.source.kind,
                  held.payment_term_days)
    held.profile_name = name
    held.profile_match = "exact"
    return _mapping_view(
        request, held,
        message=f"Profil « {name} » enregistré. Le prochain export de ce "
                "logiciel sera reconnu automatiquement.",
    )


@router.post("/import/{token}/profil/appliquer", response_class=HTMLResponse)
def apply_profile(request: Request, token: str, nom: str = Form(...)):
    _require_real(request)
    held = _get(token)
    for profile in profiles.load():
        if profile.name == nom:
            held.mapping = {
                item.key: profile.mapping.get(item.key) for item in FIELDS
            }
            held.profile_name = profile.name
            held.profile_match = "manuel"
            if profile.payment_term_days is not None:
                held.payment_term_days = profile.payment_term_days
            profiles.record_use(profile.name)
            return _mapping_view(
                request, held,
                message=f"Profil « {profile.name} » appliqué.",
            )
    return _mapping_view(request, held, error=f"Profil « {nom} » introuvable.")


# ---------------------------------------------------------------------------
# The summary, and the one button that writes
# ---------------------------------------------------------------------------

def _real_connection():
    """The real ledger, created on first use. Never the demo one."""
    conn = db_module.connect(REAL_DB_PATH)
    db_module.init_schema(conn)
    return conn


def _build_plan(held: HeldFile):
    parsed = runner.parse_rows(
        held.source, held.mapping, today(), held.payment_term_days
    )
    conn = _real_connection()
    try:
        plan = reconcile.reconcile(parsed, reconcile.load_existing(conn))
    finally:
        conn.close()
    return parsed, plan


def _is_first_import() -> bool:
    """True while the real ledger has never received anything.

    What the import refuses to do is worth reading once. Repeating it above
    every weekly import trains the owner to scroll past the whole page.
    """
    conn = _real_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
    finally:
        conn.close()


def _preview_rows(parsed, plan, limit: int = 5) -> list[dict]:
    """The first rows that will actually be written, as understood.

    Filtered against the plan, not merely against parse errors: a row can read
    perfectly and still be refused later — a duplicate invoice number, for one.
    Showing such a row here under "telles qu'elles seront comprises" would
    promise something the import is not going to do.
    """
    written = {row.line for row in plan.inserts} | {row.line for row in plan.updates}
    rows = []
    for item in parsed:
        if item.problem or item.line not in written:
            continue
        rows.append({
            "number": item.number,
            "customer": item.customer_name,
            "email": item.email or "—",
            "issue_date": format_date(item.issue_date),
            "due_date": format_date(item.due_date),
            "due_inferred": item.due_date_inferred,
            "total": format_eur(item.total_ttc_cents),
            "paid": format_eur(item.amount_paid_cents) if item.amount_paid_cents else "—",
            "vehicle": " ".join(filter(None, [item.vehicle, item.plate])) or "—",
            "warnings": item.warnings,
        })
        if len(rows) >= limit:
            break
    return rows


@router.get("/import/{token}/recapitulatif", response_class=HTMLResponse)
def summary_screen(request: Request, token: str):
    context = _require_real(request)
    held = _get(token)
    check = check_mapping(held.mapping, held.source.headers)
    if not check.ok:
        return _mapping_view(request, held, error=check.message)

    parsed, plan = _build_plan(held)

    return templates.TemplateResponse(
        request,
        "import_summary.html",
        _page_context(request, context, **{
            "token": token,
            "source": held.source,
            "plan": plan,
            "preview": _preview_rows(parsed, plan),
            "terms_days": held.payment_term_days,
            "format_eur": format_eur,
            "first_import": _is_first_import(),
            "result": None,
        }),
    )


@router.post("/import/{token}/confirmer", response_class=HTMLResponse)
def confirm_import(request: Request, token: str):
    _require_real(request)
    with _IMPORT_LOCK:
        if token in _COMPLETED:
            return RedirectResponse(f"/import/{token}/resultat", status_code=303)

        held = _get(token)
        check = check_mapping(held.mapping, held.source.headers)
        if not check.ok:
            return _mapping_view(request, held, error=check.message)

        _parsed, plan = _build_plan(held)
        conn = _real_connection()
        try:
            result = runner.apply(conn, plan)
            try:
                drafted = followup_module.create_drafts(
                    conn,
                    today(),
                    mode="mock" if DEMO_MODE else None,
                )
                result.drafts_created = len(drafted.queued)
                result.drafts_blocked = len(drafted.rejected)
            except Exception:
                # The confirmed invoices stay imported and visible. Drafting
                # can safely be retried from the Relances page.
                conn.rollback()
                result.draft_warning = (
                    "Les factures sont bien importées, mais les relances n’ont "
                    "pas pu être préparées. Réessayez depuis Relances."
                )
        finally:
            conn.close()

        _COMPLETED[token] = CompletedImport(
            source=held.source,
            plan=plan,
            result=result,
            payment_term_days=held.payment_term_days,
            completed_at=datetime.now(),
        )
        _HELD.pop(token, None)
        while len(_COMPLETED) > 10:
            oldest = min(
                _COMPLETED.items(), key=lambda item: item[1].completed_at
            )[0]
            _COMPLETED.pop(oldest, None)

    return RedirectResponse(f"/import/{token}/resultat", status_code=303)


@router.get("/import/{token}/resultat", response_class=HTMLResponse)
def completed_import(request: Request, token: str):
    context = _require_real(request)
    _cleanup_expired()
    completed = _COMPLETED.get(token)
    if completed is None:
        return RedirectResponse(
            "/import?erreur=Ce+r%C3%A9sultat+a+expir%C3%A9.+Les+factures+d%C3%A9j%C3%A0+import%C3%A9es+restent+enregistr%C3%A9es.",
            status_code=303,
        )
    return templates.TemplateResponse(
        request,
        "import_summary.html",
        _page_context(request, context, **{
            "token": token,
            "source": completed.source,
            "plan": completed.plan,
            "preview": [],
            "terms_days": completed.payment_term_days,
            "format_eur": format_eur,
            "first_import": False,
            "result": completed.result,
        }),
    )


@router.post("/import/{token}/fermer")
def close_file(request: Request, token: str):
    _require_real(request)
    _HELD.pop(token, None)
    return RedirectResponse("/import", status_code=303)
