"""Server-rendered forms for owners; every write carries its visible context."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import manual
from .config import today
from .labels_fr import format_date
from .money import format_eur

router = APIRouter()


def _render(request, page, form=None, error=None, client_id=None, invoice_id=None):
    from . import server
    context, conn = server._connect_for(request)
    try:
        customer = conn.execute("SELECT * FROM customers WHERE id=?", (client_id,)).fetchone() if client_id else None
        invoice = conn.execute("SELECT i.*, c.name AS customer FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (invoice_id,)).fetchone() if invoice_id else None
        if (client_id and not customer) or (invoice_id and not invoice):
            raise HTTPException(404, "Client ou facture introuvable.")
        values = dict(form) if form is not None else {}
        if page == "client_form" and form is None:
            values = dict(customer) if customer else {"kind": "particulier"}
            if customer:
                values["version"] = manual.client_version(customer)
        if page == "invoice_form" and form is None:
            values = {"customer_id": request.query_params.get("client", ""), "issue_date": today().isoformat(), "due_date": today().isoformat(), "payment_state": "unpaid"}
        if page == "payment_form" and form is None:
            values = {"payment_state": invoice["status"], "amount_paid": f"{invoice['amount_paid_cents']/100:.2f}", "payment_date": today().isoformat(), "version": manual.payment_version(conn, invoice)}
            last = conn.execute("SELECT payment_date,note FROM payment_changes WHERE invoice_id=? ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
            values["payment_date"] = (last["payment_date"] if last else invoice["paid_date"]) or today().isoformat()
            values["note"] = last["note"] if last else ""
        customers = [dict(r) for r in conn.execute("SELECT id,name FROM customers ORDER BY name COLLATE NOCASE")] if page == "invoice_form" else []
        invoices = [server._invoice_view(item) for item in server.engine.load_all(conn, today(), include_paid=True) if item.customer_id == client_id] if page == "client_detail" else []
        response = server._template(request, page + ".html", context, "clients" if page.startswith("client") else "invoices",
                                    form=values, error=error, client=customer, invoice=invoice, customers=customers, invoices=invoices,
                                    today_iso=today().isoformat(), invoice_total=format_eur(invoice["total_ttc_cents"]) if invoice else "")
        if error:
            response.status_code = error.status
        return response
    finally:
        conn.close()


@router.get("/clients/nouveau")
def new_client(request: Request):
    return _render(request, "client_form")


@router.get("/clients/{client_id}/modifier")
def edit_client(request: Request, client_id: int):
    return _render(request, "client_form", client_id=client_id)


@router.get("/clients/{client_id}")
def client_detail(request: Request, client_id: int):
    return _render(request, "client_detail", client_id=client_id)


def _save_client(request, form, client_id=None):
    from . import server
    server._require_rendered_context(request, form.get("contexte"))
    context, conn = server._connect_for(request)
    try:
        saved_id = manual.save_client(conn, form, client_id)
    except manual.Problem as error:
        return _render(request, "client_form", form, error, client_id=client_id)
    finally:
        conn.close()
    return RedirectResponse(f"/clients/{saved_id}?enregistre=1", status_code=303)


@router.post("/clients/nouveau")
async def add_client(request: Request):
    return await run_in_threadpool(_save_client, request, await request.form())


@router.post("/clients/{client_id}/modifier")
async def change_client(request: Request, client_id: int):
    return await run_in_threadpool(_save_client, request, await request.form(), client_id)


@router.get("/factures/nouvelle")
def new_invoice(request: Request):
    return _render(request, "invoice_form")


def _save_invoice(request, form):
    from . import server
    server._require_rendered_context(request, form.get("contexte"))
    context, conn = server._connect_for(request)
    try:
        invoice_id = manual.create_invoice(conn, form)
    except manual.Problem as error:
        return _render(request, "invoice_form", form, error)
    finally:
        conn.close()
    return RedirectResponse(f"/factures/{invoice_id}?ajoutee=1", status_code=303)


@router.post("/factures/nouvelle")
async def add_invoice(request: Request):
    return await run_in_threadpool(_save_invoice, request, await request.form())


@router.get("/factures/{invoice_id}/paiement")
def payment_form(request: Request, invoice_id: int):
    return _render(request, "payment_form", invoice_id=invoice_id)


def _save_payment(request, invoice_id, form):
    from . import server
    server._require_rendered_context(request, form.get("contexte"))
    context, conn = server._connect_for(request)
    try:
        changed = manual.update_payment(conn, invoice_id, form)
    except manual.Problem as error:
        return _render(request, "payment_form", form, error, invoice_id=invoice_id)
    finally:
        conn.close()
    return RedirectResponse(f"/factures/{invoice_id}?paiement={'modifie' if changed else 'inchange'}", status_code=303)


@router.post("/factures/{invoice_id}/paiement")
async def save_payment(request: Request, invoice_id: int):
    return await run_in_threadpool(_save_payment, request, invoice_id, await request.form())
