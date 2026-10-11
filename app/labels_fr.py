"""French vocabulary of the product.

Database codes stay English (`paid`, `unpaid`, `pending`...). Everything an
owner or a customer reads is here, in one place, so the wording can be tuned
without touching the logic.
"""

from __future__ import annotations

from datetime import date

LEVEL_NAME = {
    0: "Sous surveillance",
    1: "Rappel courtois",
    2: "Relance ferme",
    3: "Dernier avis",
    4: "Mise en demeure",
}

LEVEL_SHORT = {
    0: "Surveillance",
    1: "Rappel",
    2: "Relance",
    3: "Dernier avis",
    4: "Mise en demeure",
}

LEVEL_INTENT = {
    0: "Échue depuis peu, on laisse passer quelques jours",
    1: "On part du principe que la facture a simplement été oubliée",
    2: "Ton plus ferme, rappel des conditions de règlement",
    3: "Dernier avis avant procédure de recouvrement",
    4: "Mise en demeure, à imprimer et envoyer en recommandé",
}

INVOICE_STATUS = {
    "paid": "Réglée",
    "unpaid": "Impayée",
    "partial": "Partiellement payée",
}

FOLLOWUP_STATUS = {
    "pending": "À valider",
    "validated": "Validée",
    "skipped": "Ignorée",
    # The .eml was downloaded. That is not sending: the owner may have pressed
    # Send, or closed the window. Only he can say.
    "handed_over": "Remise à votre messagerie",
    "sent": "Envoyée",
    "obsolete": "Ancien message — remplacé ou sans objet",
}

CUSTOMER_KIND = {
    "particulier": "Particulier",
    "professionnel": "Professionnel",
}

CHANNEL = {
    "email": "E-mail",
    "courrier": "Courrier recommandé AR",
}

MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


def format_date(value: date | str) -> str:
    """2026-08-27 -> 27/08/2026"""
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return value.strftime("%d/%m/%Y")


def format_date_long(value: date | str) -> str:
    """2026-08-27 -> 27 août 2026"""
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def format_delay(days_overdue: int) -> str:
    """Say the delay the way a garage owner would."""
    if days_overdue > 1:
        return f"{days_overdue} jours de retard"
    if days_overdue == 1:
        return "1 jour de retard"
    if days_overdue == 0:
        return "échéance aujourd’hui"
    if days_overdue == -1:
        return "échéance demain"
    return f"échéance dans {abs(days_overdue)} jours"


def plural(count: int, singular: str, many: str | None = None) -> str:
    return f"{count} {singular if abs(count) <= 1 else (many or singular + 's')}"
