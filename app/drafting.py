"""Message drafting.

The boundary between the arithmetic and the prose lives here.

`DraftContext` is the ONLY thing a backend ever sees. Every field is a finished
string: amounts already formatted in euros, dates already formatted the French
way, delays already spelled out. There is not a single raw integer in it, so
there is nothing for a model to add up, compare or convert. It writes prose
around figures that `overdue.py` decided.

Two backends:

* `mock`      — pre-written French templates. Free, offline, the default.
* `anthropic` — a real API call. Enabled with LLM_MODE=anthropic.

Whichever produced it, the result goes through the number guardrail before it is
allowed anywhere near the validation queue.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date

from .config import (
    FORMAL_DEMAND_DEADLINE,
    GARAGE,
    LEVEL_CHANNEL,
    LLM_MODE,
    REMINDER_DEADLINES,
    today as today_fn,
)
from .guardrail import GuardrailReport, verify_draft
from .labels_fr import LEVEL_NAME, format_date, format_delay, plural
from .money import format_eur
from .overdue import OverdueInvoice

CIVILITY_LONG = {"M.": "Monsieur", "Mme": "Madame", "": ""}

# Printed at the top of a formal demand. The letter is only meaningful if it is
# sent in a way that proves it was received.
REGISTERED_MAIL_MENTION = "LETTRE RECOMMANDÉE AVEC ACCUSÉ DE RÉCEPTION"

# Provided as a finished string so no backend ever cites an article of its own.
CIVIL_CODE_REFERENCE = "article 1344 du code civil"


@dataclass(frozen=True)
class DraftContext:
    """Everything a backend is given. Strings only, all pre-computed."""

    # what kind of message this is
    level: int
    level_name: str
    channel: str
    delivery_mention: str | None      # registered mail, level 4 only
    deadline: str                     # "8 jours"

    # who it goes to
    salutation: str                   # "Madame Perrin"
    customer_name: str
    customer_address_line: str
    customer_kind_label: str
    relationship: str | None          # "client de l’atelier depuis 2017"

    # what it is about
    invoice_number: str
    issue_date: str
    due_date: str
    payment_terms: str                # "15 jours"
    delay: str                        # "57 jours de retard"
    delay_short: str                  # "57 jours", for use inside a sentence
    job_label: str
    vehicle: str
    plate: str
    # Ready-made phrasings, empty when the file carried no vehicle. Built here
    # so no template has to write "sur votre  immatriculé ".
    vehicle_clause: str               # " sur votre Renault Clio IV immatriculé DJ-482-QR"
    vehicle_clause_formal: str        # " sur le véhicule ... immatriculé ..."
    vehicle_line: str                 # a labelled line, or ""

    # the figures, already formatted
    amount_due: str
    invoice_total: str | None         # only shown when a deposit exists
    deposit: str | None
    penalty: str | None
    penalty_rate: str | None          # "15,00 % par an"
    recovery_fee: str | None
    total_claimed: str
    cgv_reference: str | None
    penalty_terms_confirmed: bool

    # the garage
    garage_name: str
    garage_address_line: str
    garage_city: str
    garage_phone: str
    garage_email: str
    garage_iban: str
    garage_bic: str
    garage_siret: str
    manager: str
    manager_role: str
    opening_hours: str
    letter_date: str
    legal_reference: str

    def sources(self) -> tuple[str, ...]:
        """Every string the backend received — the guardrail's reference set.

        A number may appear in the message only if it appears in here.
        """
        values = []
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, str):
                values.append(value)
        return tuple(values)


@dataclass(frozen=True)
class Draft:
    subject: str
    body: str
    generator: str                    # 'mock' | 'anthropic'
    guardrail: GuardrailReport

    @property
    def accepted(self) -> bool:
        return self.guardrail.ok


# ---------------------------------------------------------------------------
# Building the context
# ---------------------------------------------------------------------------

def _salutation(item: OverdueInvoice) -> str:
    """How a garage actually addresses this customer in writing."""
    civility = CIVILITY_LONG.get(item.civility, "")
    surname = (item.contact_name or item.customer_name).split()[-1]
    if not civility:
        return "Madame, Monsieur"
    return f"{civility} {surname}"


def _vehicle_clause(item: OverdueInvoice, lead: str) -> str:
    """« sur votre Renault Clio IV immatriculé DJ-482-QR », or nothing at all."""
    if not item.has_vehicle:
        return ""
    parts = [lead]
    if item.vehicle_label:
        parts.append(item.vehicle_label)
    if item.plate:
        parts.append(f"immatriculé {item.plate}")
    return " " + " ".join(parts)


def _vehicle_line(item: OverdueInvoice) -> str:
    if not item.has_vehicle:
        return ""
    label = item.vehicle_label
    if item.plate:
        label = f"{label} — {item.plate}".strip(" —")
    return label


def _rate_label(rate_bp: int | None) -> str | None:
    if not rate_bp:
        return None
    return f"{rate_bp // 100},{rate_bp % 100:02d} % par an".replace(".", ",")


def build_context(item: OverdueInvoice, customer_address: str,
                  reference: date | None = None) -> DraftContext:
    """Turn an evaluated invoice into finished strings, and nothing else."""
    reference = reference or today_fn()

    has_penalty = item.penalty_cents > 0
    has_fee = item.recovery_fee_cents > 0

    return DraftContext(
        level=item.level,
        level_name=LEVEL_NAME[item.level],
        channel=LEVEL_CHANNEL[item.level],
        delivery_mention=REGISTERED_MAIL_MENTION if item.level == 4 else None,
        deadline=REMINDER_DEADLINES.get(item.level, FORMAL_DEMAND_DEADLINE),
        salutation=_salutation(item),
        customer_name=item.customer_name,
        customer_address_line=customer_address,
        customer_kind_label=(
            "professionnel" if item.customer_kind == "professionnel" else "particulier"
        ),
        relationship=(
            f"client de l’atelier depuis {item.customer_since}" if item.is_regular else None
        ),
        invoice_number=item.number,
        issue_date=format_date(item.issue_date),
        due_date=format_date(item.due_date),
        payment_terms=f"{item.payment_terms_days} jours",
        delay=format_delay(item.days_overdue),
        delay_short=plural(item.days_overdue, "jour"),
        job_label=item.job_label,
        vehicle=item.vehicle_label,
        plate=item.plate,
        vehicle_clause=_vehicle_clause(item, "sur votre"),
        vehicle_clause_formal=_vehicle_clause(item, "sur le véhicule"),
        vehicle_line=_vehicle_line(item),
        amount_due=format_eur(item.amount_due_cents),
        invoice_total=format_eur(item.total_ttc_cents) if item.has_deposit else None,
        deposit=format_eur(item.amount_paid_cents) if item.has_deposit else None,
        penalty=format_eur(item.penalty_cents) if has_penalty else None,
        penalty_rate=_rate_label(item.penalty_rate_bp) if has_penalty else None,
        recovery_fee=format_eur(item.recovery_fee_cents) if has_fee else None,
        total_claimed=format_eur(item.total_claimed_cents),
        cgv_reference=item.cgv_reference if item.penalty_terms_confirmed else None,
        penalty_terms_confirmed=item.penalty_terms_confirmed,
        garage_name=GARAGE["name"],
        garage_address_line=(
            f"{GARAGE['address']}, {GARAGE['postal_code']} {GARAGE['city']}"
        ),
        garage_city=GARAGE["city"],
        garage_phone=GARAGE["phone"],
        garage_email=GARAGE["email"],
        garage_iban=GARAGE["iban"],
        garage_bic=GARAGE["bic"],
        garage_siret=GARAGE["siret"],
        manager=GARAGE["manager"],
        manager_role=GARAGE["manager_role"],
        opening_hours=GARAGE["opening_hours"],
        letter_date=format_date(reference),
        legal_reference=CIVIL_CODE_REFERENCE,
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

Backend = Callable[[DraftContext], tuple[str, str]]


def generate(context: DraftContext, mode: str | None = None,
             backend: Backend | None = None) -> Draft:
    """Draft one message and put it through the guardrail before returning it.

    `backend` overrides the configured engine. It exists so the guardrail can be
    exercised against a backend that deliberately invents figures — a check that
    would be worthless if it could only ever be run against templates known to
    behave.
    """
    mode = (mode or LLM_MODE).strip().lower()

    if backend is not None:
        subject, body = backend(context)
    elif mode == "anthropic":
        from .anthropic_backend import draft as backend_draft
        subject, body = backend_draft(context)
    else:
        from .mock_drafts import draft as backend_draft
        mode = "mock"
        subject, body = backend_draft(context)

    report = verify_draft(subject, body, context.sources())
    return Draft(subject=subject.strip(), body=body.strip(),
                 generator=mode, guardrail=report)
