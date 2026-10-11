"""Working out what the separators mean in *this* file.

A single value can be unreadable while the column it sits in is perfectly
clear. `1,234` alone is either one thousand two hundred and thirty-four or one
euro twenty-three. But if the same column also holds `189,90`, then in this
file the comma is the decimal separator, and `1,234` is 1,23 €. If no value
anywhere in the column has a comma followed by two digits, the comma groups
thousands and `1,234` is 1 234 €.

So the convention is decided once, from the whole column, and then every value
is read with it.

When the column does not settle the question, nothing is guessed. The row is
rejected and named in the summary. The error is not symmetric: reading `1,234`
as 1 234 € when it meant 1,23 € puts a formal demand for a thousand times the
real debt in front of a customer — the garage loses the customer and is in the
wrong. The opposite mistake merely under-claims, and the owner spots it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# A separator followed by exactly three digits is the only genuinely ambiguous
# shape: a thousands group in one convention, three decimals in the other.
AMBIGUOUS_DIGITS = 3


@dataclass(frozen=True)
class AmountConvention:
    decimal: str | None          # ',' or '.', once the column has settled it
    thousands: str | None
    settled: bool
    has_ambiguous: bool          # does the column actually contain such values
    evidence: str                # French, shown on the import summary
    sample_count: int = 0

    @property
    def blocks_import(self) -> bool:
        """True when the column holds values nothing can safely resolve."""
        return self.has_ambiguous and not self.settled


@dataclass(frozen=True)
class _Evidence:
    decimal: str | None = None
    thousands: str | None = None
    ambiguous: bool = False


def classify(body: str) -> _Evidence:
    """What one cleaned value proves about the file's convention."""
    has_dot = "." in body
    has_comma = "," in body

    if has_dot and has_comma:
        # Both present: the rightmost is the decimal point, the other groups.
        if body.rfind(".") > body.rfind(","):
            return _Evidence(decimal=".", thousands=",")
        return _Evidence(decimal=",", thousands=".")

    separator = "." if has_dot else ("," if has_comma else None)
    if separator is None:
        return _Evidence()

    if body.count(separator) > 1:
        # 1.234.567 — repeated, so it can only be grouping.
        return _Evidence(thousands=separator)

    digits_after = len(body) - body.rfind(separator) - 1

    if digits_after in (1, 2):
        return _Evidence(decimal=separator)      # 89,90 — decimal, certain
    if digits_after > AMBIGUOUS_DIGITS:
        return _Evidence(decimal=separator)      # 12,3456 — decimal, certain
    if digits_after == AMBIGUOUS_DIGITS:
        return _Evidence(ambiguous=True)         # 1,234 — proves nothing
    return _Evidence()                           # trailing separator


def _clean(raw) -> str | None:
    """Reduce a cell to digits and separators, or None if it is not a number."""
    from .values import clean_amount_body

    return clean_amount_body(raw)


def detect_amount_convention(values: Iterable) -> AmountConvention:
    """Read a whole column and decide what its separators mean."""
    decimal_votes: dict[str, int] = {}
    thousands_votes: dict[str, int] = {}
    ambiguous = 0
    examined = 0
    decimal_example = thousands_example = ambiguous_example = None

    for raw in values:
        body = _clean(raw)
        if not body:
            continue
        examined += 1
        evidence = classify(body)

        if evidence.decimal:
            decimal_votes[evidence.decimal] = decimal_votes.get(evidence.decimal, 0) + 1
            if decimal_example is None:
                decimal_example = body
        if evidence.thousands:
            thousands_votes[evidence.thousands] = (
                thousands_votes.get(evidence.thousands, 0) + 1
            )
            if thousands_example is None:
                thousands_example = body
        if evidence.ambiguous:
            ambiguous += 1
            if ambiguous_example is None:
                ambiguous_example = body

    has_ambiguous = ambiguous > 0

    if not examined:
        return AmountConvention(
            None, None, False, has_ambiguous,
            "Colonne vide : aucune convention à déterminer.", 0,
        )

    # Two different characters both proven decimal: the file mixes conventions.
    if len(decimal_votes) > 1:
        return AmountConvention(
            None, None, False, has_ambiguous,
            "Cette colonne mélange les conventions : « , » et « . » y servent "
            "tous deux de séparateur décimal. Aucune règle unique ne s’applique.",
            examined,
        )

    decimal = next(iter(decimal_votes), None)

    if decimal is None and len(thousands_votes) == 1:
        # Nothing proven decimal, but one character is proven to group: the
        # other is the decimal separator by elimination.
        grouped = next(iter(thousands_votes))
        decimal = "," if grouped == "." else "."

    if decimal is None:
        return AmountConvention(
            None, None, False, has_ambiguous,
            "Aucun montant de cette colonne ne permet de déterminer si « , » "
            "sépare les décimales ou les milliers.",
            examined,
        )

    if decimal in thousands_votes:
        return AmountConvention(
            None, None, False, has_ambiguous,
            f"Contradiction : « {decimal} » sert à la fois de séparateur "
            "décimal et de séparateur de milliers dans cette colonne.",
            examined,
        )

    thousands = "," if decimal == "." else "."
    name = "La virgule" if decimal == "," else "Le point"
    example = decimal_example or thousands_example
    evidence_text = (
        f"{name} est le séparateur décimal dans ce fichier"
        + (f" (d’après « {example} »)." if example else ".")
    )
    if has_ambiguous and ambiguous_example:
        # Say what actually happens to such a value here, rather than restate
        # the rule: the owner checks the reading, not the reasoning.
        from .values import parse_amount
        from ..money import format_eur

        settled_here = AmountConvention(decimal, thousands, True, True, "", examined)
        try:
            reading = format_eur(parse_amount(ambiguous_example, settled_here).cents)
            evidence_text += f" « {ambiguous_example} » est donc lu {reading}."
        except Exception:                                    # noqa: BLE001
            pass

    return AmountConvention(
        decimal=decimal,
        thousands=thousands,
        settled=True,
        has_ambiguous=has_ambiguous,
        evidence=evidence_text,
        sample_count=examined,
    )


UNDECIDED = AmountConvention(
    None, None, False, False,
    "Aucune colonne examinée : les montants ambigus sont refusés.", 0,
)
