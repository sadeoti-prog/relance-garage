"""Pre-written French messages, one tone per escalation level.

The default backend. No API call, no credits, no network — the demo runs the
same on a train as in an office, and the wording is identical every time, which
matters when the same demo is given twice in a day.

These templates only ever print strings taken from the context. They perform no
arithmetic and invent no figure, which is why they pass the number guardrail by
construction — and why the guardrail is a real test of the API backend rather
than a formality.

Tone per level:
  1  polite, assumes an oversight, no mention of penalties
  2  firm but still commercial, restates the payment terms
  3  serious, final notice before formal demand
  4  formal legal register, factual, no emotion
"""

from __future__ import annotations

from .drafting import DraftContext


def _vehicle_row(context: DraftContext, label: str) -> str:
    """A labelled vehicle line, or nothing when there is no vehicle."""
    if not context.vehicle_line:
        return ""
    return f"    {label}{context.vehicle_line}\n"


def _payment_block(context: DraftContext) -> str:
    return (
        "Règlement par virement :\n"
        f"    IBAN : {context.garage_iban}\n"
        f"    BIC  : {context.garage_bic}\n"
        f"En précisant la référence {context.invoice_number}."
    )


# The closing formula carries as much register as the body does: a formal
# demand that ends with "Cordialement" does not read like a formal demand.
_CLOSINGS = {
    1: "Cordialement,",
    2: "Cordialement,",
    3: "Veuillez agréer, {salutation}, nos salutations distinguées.",
    4: "Veuillez agréer, {salutation}, l’expression de nos salutations distinguées.",
}


def _signature(context: DraftContext) -> str:
    closing = _CLOSINGS[context.level].format(salutation=context.salutation)
    return (
        f"{closing}\n\n"
        f"{context.manager}\n"
        f"{context.manager_role} — {context.garage_name}\n"
        f"{context.garage_phone} — {context.garage_email}"
    )


def _deposit_line(context: DraftContext) -> str:
    """Shown only when a deposit was received, so the customer sees it counted."""
    if not context.deposit:
        return ""
    return (
        f"\nVotre acompte de {context.deposit} a bien été enregistré : "
        f"sur un total de {context.invoice_total}, le solde restant dû est de "
        f"{context.amount_due}.\n"
    )


# ---------------------------------------------------------------------------
# Level 1 — rappel courtois
# ---------------------------------------------------------------------------

def _level_1(context: DraftContext) -> tuple[str, str]:
    subject = f"Facture {context.invoice_number} — simple rappel"

    body = f"""Bonjour {context.salutation},

Nous revenons vers vous au sujet de la facture {context.invoice_number} du {context.issue_date}, établie pour {context.job_label}{context.vehicle_clause}.

Son échéance était fixée au {context.due_date} et son règlement ne nous est pas encore parvenu. Il s’agit très probablement d’un simple oubli.
{_deposit_line(context)}
Montant restant dû : {context.amount_due}

Si votre règlement a été effectué ces derniers jours, merci de ne pas tenir compte de ce message.

{_payment_block(context)}

Vous pouvez également régler directement à l’atelier, {context.opening_hours}, ou nous appeler au {context.garage_phone} pour toute question.

{_signature(context)}"""

    return subject, body


# ---------------------------------------------------------------------------
# Level 2 — relance ferme, encore commerciale
# ---------------------------------------------------------------------------

def _level_2(context: DraftContext) -> tuple[str, str]:
    subject = f"Facture {context.invoice_number} impayée — {context.delay}"

    if context.penalty:
        penalty_paragraph = (
            f"Conformément à nos conditions de règlement ({context.cgv_reference}), "
            f"des pénalités de retard au taux de {context.penalty_rate} sont dues, "
            f"soit {context.penalty} à ce jour."
        )
        if context.recovery_fee:
            penalty_paragraph += (
                f" S’y ajoute l’indemnité forfaitaire de recouvrement de "
                f"{context.recovery_fee} applicable entre professionnels. "
                f"Le total s’élève donc à {context.total_claimed}."
            )
        else:
            penalty_paragraph += f" Le total s’élève donc à {context.total_claimed}."
        penalty_paragraph += "\n\n"
    else:
        penalty_paragraph = ""

    body = f"""Bonjour {context.salutation},

Sauf erreur de notre part, la facture {context.invoice_number} du {context.issue_date} reste impayée à ce jour.

    Prestation      : {context.job_label}
{_vehicle_row(context, 'Véhicule        : ')}    Échéance        : {context.due_date} (règlement à {context.payment_terms})
    Retard constaté : {context.delay}
    Montant dû      : {context.amount_due}
{_deposit_line(context)}
{penalty_paragraph}Nous vous remercions de bien vouloir procéder au règlement sous {context.deadline}.

{_payment_block(context)}

Si ce règlement pose une difficulté particulière, appelez-nous au {context.garage_phone} : nous trouverons une solution ensemble.

{_signature(context)}"""

    return subject, body


# ---------------------------------------------------------------------------
# Level 3 — dernier avis avant mise en demeure
# ---------------------------------------------------------------------------

def _level_3(context: DraftContext) -> tuple[str, str]:
    subject = (f"DERNIER AVIS — facture {context.invoice_number} "
               f"impayée depuis {context.delay_short}")

    if context.penalty:
        claim = (
            f"    Pénalités de retard : {context.penalty}\n"
        )
        if context.recovery_fee:
            claim += f"    Indemnité forfaitaire de recouvrement : {context.recovery_fee}\n"
        claim += f"    Total réclamé : {context.total_claimed}\n"
    else:
        claim = f"    Total réclamé : {context.total_claimed}\n"

    body = f"""{context.salutation},

Malgré nos précédents rappels, la facture {context.invoice_number} du {context.issue_date} demeure impayée, soit {context.delay}.

    Prestation : {context.job_label}
{_vehicle_row(context, 'Véhicule   : ')}    Échéance   : {context.due_date}
    Principal restant dû : {context.amount_due}
{claim}{_deposit_line(context)}
Nous vous demandons de régulariser cette situation sous {context.deadline}.

À défaut de règlement dans ce délai, ce dossier fera l’objet d’une mise en demeure adressée par lettre recommandée avec accusé de réception, préalable à toute procédure de recouvrement.

{_payment_block(context)}

Nous préférons de loin régler cette question directement avec vous. Vous pouvez nous joindre au {context.garage_phone}, {context.opening_hours}.

{_signature(context)}"""

    return subject, body


# ---------------------------------------------------------------------------
# Level 4 — mise en demeure
# ---------------------------------------------------------------------------

def _level_4(context: DraftContext) -> tuple[str, str]:
    subject = f"MISE EN DEMEURE — facture {context.invoice_number}"

    breakdown = f"    Principal restant dû ................ {context.amount_due}\n"
    if context.penalty:
        breakdown += f"    Pénalités de retard ................. {context.penalty}\n"
    if context.recovery_fee:
        breakdown += f"    Indemnité forfaitaire de recouvrement  {context.recovery_fee}\n"
    breakdown += f"    TOTAL ............................... {context.total_claimed}"

    deposit_note = ""
    if context.deposit:
        deposit_note = (
            f"\nL’acompte de {context.deposit} déjà réglé sur un total de "
            f"{context.invoice_total} a été déduit du principal ci-dessus.\n"
        )

    body = f"""{context.delivery_mention}

{context.garage_name}
{context.garage_address_line}
SIRET {context.garage_siret}

{context.customer_name}
{context.customer_address_line}

{context.garage_city}, le {context.letter_date}

Objet : mise en demeure de payer — facture {context.invoice_number}

{context.salutation},

Malgré nos relances successives, la facture {context.invoice_number} du {context.issue_date}, établie pour {context.job_label}{context.vehicle_clause_formal}, échue le {context.due_date}, demeure impayée à ce jour, soit {context.delay}.

Par la présente, nous vous mettons en demeure de nous régler la somme de {context.total_claimed}, se décomposant comme suit :

{breakdown}
{deposit_note}
Ce règlement doit intervenir dans un délai de {context.deadline} à compter de la réception de la présente lettre.

{_payment_block(context)}

À défaut de règlement dans ce délai, nous nous réservons le droit d’engager toute procédure de recouvrement utile, sans nouvel avis.

La présente vaut mise en demeure au sens de l’{context.legal_reference}.

{_signature(context)}"""

    return subject, body


_TEMPLATES = {1: _level_1, 2: _level_2, 3: _level_3, 4: _level_4}


def draft(context: DraftContext) -> tuple[str, str]:
    """Return (subject, body) for this context."""
    template = _TEMPLATES.get(context.level)
    if template is None:
        raise ValueError(f"no template for level {context.level!r}")
    return template(context)
