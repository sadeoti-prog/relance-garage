"""Small, deliberately strict validation for one customer e-mail address."""

from __future__ import annotations

import re

_SINGLE_MAILBOX = re.compile(
    r"^[^\s@<>,;:]+@(?:[^\s@<>,;:.]+\.)+[^\s@<>,;:.]{2,63}$",
    re.IGNORECASE,
)


def normalise(value: str | None) -> str:
    return (value or "").strip().lower()


def is_valid(value: str | None) -> bool:
    """Accept exactly one ordinary mailbox and reject header/multi-address input."""
    address = (value or "").strip()
    if not address or len(address) > 254 or "\r" in address or "\n" in address:
        return False
    return bool(_SINGLE_MAILBOX.fullmatch(address))


def validated(value: str | None) -> str:
    address = (value or "").strip()
    return address if is_valid(address) else ""
