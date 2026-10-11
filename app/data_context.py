"""Request-scoped selection of demonstration or real business data.

The browser starts in demonstration mode on every application restart.  A
short-lived cookie lets the owner opt into real data for the current run only;
the cookie contains a boot nonce, so a stale "real" choice can never reveal
customer data the next time the application is launched for a demonstration.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request

from .config import DB_PATH, DEMO_MODE, GARAGE, REAL_DB_PATH, today
from .labels_fr import format_date_long

COOKIE_NAME = "relance_data_mode"
_BOOT_NONCE = secrets.token_urlsafe(18)


@dataclass(frozen=True)
class DataContext:
    key: str
    label: str
    description: str
    path: Path
    is_demo: bool

    @property
    def preview_only(self) -> bool:
        """Whether customer messages must stay previews in this context."""
        return self.is_demo or DEMO_MODE


DEMO = DataContext(
    key="demo",
    label="Mode démonstration",
    description="Données fictives · aucun envoi réel",
    path=DB_PATH,
    is_demo=True,
)

REAL = DataContext(
    key="real",
    label="Données réelles",
    description=(
        "Envoi désactivé pendant la pré-production"
        if DEMO_MODE
        else "Envoi possible après validation"
    ),
    path=REAL_DB_PATH,
    is_demo=False,
)


def assert_isolated_paths() -> None:
    """Fail closed if configuration points both contexts at the same file."""
    if DB_PATH.resolve() == REAL_DB_PATH.resolve():
        raise RuntimeError(
            "Configuration dangereuse : les données de démonstration et les "
            "données réelles doivent utiliser deux emplacements distincts."
        )


def cookie_value(mode: str) -> str:
    if mode == "real":
        return f"real.{_BOOT_NONCE}"
    return "demo"


def from_request(request: Request) -> DataContext:
    """Return the selected context; every unknown or stale value means demo."""
    if request.cookies.get(COOKIE_NAME) == cookie_value("real"):
        return REAL
    return DEMO


def safe_return_path(value: str | None, fallback: str = "/") -> str:
    """Allow only local application paths after a mode switch."""
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return fallback
    return value


def template_context(request: Request, context: DataContext, active: str) -> dict:
    """Shared, business-language state used by every application page."""
    # Numeric identifiers belong to one ledger only.  Reusing a detail URL
    # after switching ledgers can therefore open the wrong record or a 404.
    # Mode changes always return to the stable entry page for the section.
    section_returns = {
        "dashboard": "/",
        "invoices": "/factures",
        "clients": "/clients",
        "import": "/import",
        "reminders": "/relances",
        "settings": "/parametres/envoi",
    }
    return {
        "shell": {
            "garage": GARAGE,
            "today": format_date_long(today()),
            "active": active,
            "return_path": section_returns.get(active, "/"),
            "context": {
                "key": context.key,
                "label": context.label,
                "description": context.description,
                "is_demo": context.is_demo,
                "preview_only": context.preview_only,
            },
        }
    }


assert_isolated_paths()
