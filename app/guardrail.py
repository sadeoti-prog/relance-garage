"""The number guardrail.

A drafted message may only contain figures that were handed to the model. Every
number appearing in the subject or the body is extracted, normalised, and looked
up in the set of numbers present in the input data. Anything else — an invented
amount, a hallucinated date, a made-up account number — fails the check and the
message is flagged instead of being queued.

This is deliberately strict in one direction only. A false alarm costs the owner
one glance at a flagged draft; a figure nobody checked reaching a customer costs
him the relationship.

What it does NOT claim: it cannot tell that a correct number was used in the
wrong place. It guarantees provenance, not meaning. Everything the customer
receives is validated by a human anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches a French-formatted number: digits, optional thin/hard/regular spaces
# as thousand separators, optional decimals after a comma or a dot.
_NUMBER = re.compile(r"\d[\d\u00a0\u202f ]*(?:[.,]\d+)?")

# Dates are matched whole, before the number scan, so that 31/12/2099 is
# reported as one invented date rather than as "31, 12, 2099".
_DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")

# Characters used as thousand separators, stripped before comparison.
_SEPARATORS = (chr(0x00A0), chr(0x202F), " ")  # nbsp, narrow nbsp, space


def normalise(token: str) -> str:
    """`2 153,04` and `2153.04` both become `2153.04`."""
    cleaned = token
    for separator in _SEPARATORS:
        cleaned = cleaned.replace(separator, "")
    cleaned = cleaned.replace(",", ".")
    return cleaned.strip(".")


def extract_numbers(text: str) -> list[str]:
    """Every number in a piece of text, normalised, in order of appearance."""
    return [token for token, _original in _scan(text)]


def _scan(text: str) -> list[tuple[str, str]]:
    """(normalised, as-written) for every number found, in order."""
    text = text or ""
    found: list[tuple[int, str, str]] = []

    # Dates first, then blank them out so their parts are not counted twice.
    masked = list(text)
    for match in _DATE.finditer(text):
        found.append((match.start(), match.group(), match.group()))
        for index in range(match.start(), match.end()):
            masked[index] = " "

    for match in _NUMBER.finditer("".join(masked)):
        original = match.group().strip()
        token = normalise(original)
        if token:
            found.append((match.start(), token, original))

    found.sort(key=lambda item: item[0])
    return [(token, original) for _start, token, original in found]


def allowed_numbers(sources) -> set[str]:
    """The numbers the model was actually given, from every input string."""
    allowed: set[str] = set()
    for source in sources:
        if source is None:
            continue
        allowed.update(extract_numbers(str(source)))
    return allowed


@dataclass(frozen=True)
class GuardrailReport:
    ok: bool
    unknown: tuple[str, ...]        # numbers in the message, absent from the input
    checked: tuple[str, ...]        # every number found in the message
    allowed_count: int

    @property
    def note(self) -> str | None:
        """French explanation, stored next to a rejected draft."""
        if self.ok:
            return None
        listed = ", ".join(self.unknown)
        return (
            "Chiffre(s) absent(s) des données transmises : "
            f"{listed}. Message bloqué avant la file de validation."
        )

    def summary(self) -> str:
        if self.ok:
            return f"{len(self.checked)} chiffre(s) vérifié(s), tous d’origine connue"
        return f"{len(self.unknown)} chiffre(s) inventé(s) sur {len(self.checked)} vérifiés"


def verify(text: str, sources) -> GuardrailReport:
    """Check one piece of generated text against the data given to the model.

    Offending numbers are reported as they were written, not in normalised
    form: the garage owner reads this on the dashboard, and "9 876,54" means
    something to him where "9876.54" does not.
    """
    allowed = allowed_numbers(sources)
    scanned = _scan(text)

    unknown: dict[str, str] = {}
    for token, original in scanned:
        if token not in allowed and token not in unknown:
            unknown[token] = original

    return GuardrailReport(
        ok=not unknown,
        unknown=tuple(unknown.values()),
        checked=tuple(token for token, _ in scanned),
        allowed_count=len(allowed),
    )


def verify_draft(subject: str, body: str, sources) -> GuardrailReport:
    """Subject and body are checked together: both reach the customer."""
    return verify(f"{subject}\n{body}", sources)
