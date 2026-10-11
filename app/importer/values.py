"""Turning cells from a spreadsheet into money and dates.

Three rules govern this module.

**No float ever touches an amount.** A value read from a file goes from its
digits straight to integer cents, through string handling and integer
arithmetic. `1 234,56 €` becomes 123456, never 1234.56.

**Separators are decided per column, not per value.** A file uses one
convention throughout. `convention.py` reads the whole column first and works
out what `,` and `.` mean in that file; every value is then parsed with that
single answer. See `parse_amount`.

**Nothing ambiguous is ever guessed.** When the column does not settle the
question, the value is refused and the row is named in the summary. Reading
`1,234` as 1 234 € when it meant 1,23 € would put a formal demand for a
thousand times the debt in front of a customer.

Failure messages are in French: they end up on the import summary screen, read
by the garage owner.
"""


from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

# Whitespace that shows up as a thousands separator in French exports.
_SPACES = (chr(0x0020), chr(0x00A0), chr(0x202F), chr(0x2009), chr(0x0009))
# space, nbsp, narrow nbsp, thin space, tab

# Currency markers stripped before parsing, longest first.
_CURRENCY = ("EUROS", "EURO", "EUR", "€", "$", "£")

_DIGITS_ONLY = re.compile(r"^\d+$")
_AMOUNT_SHAPE = re.compile(r"^[+-]?[\d.,]+$")
_DATE_SPLIT = re.compile(r"[/\-.]")

# Excel counts days from 1899-12-30: the 1900 leap-year bug is baked into the
# format, and this epoch is what makes serial numbers line up with real dates.
_EXCEL_EPOCH = date(1899, 12, 30)
_EXCEL_MIN_SERIAL = 10_000      # 1927 — below this, a number is not a date
_EXCEL_MAX_SERIAL = 2_958_465   # 9999-12-31, the format's own ceiling

# Two-digit years: 26 -> 2026, 98 -> 1998.
_CENTURY_PIVOT = 70


class ValueProblem(Exception):
    """A cell that cannot be understood. `reason` is shown to the user, in French."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Amount:
    cents: int
    warning: str | None = None


@dataclass(frozen=True)
class ParsedDate:
    value: date
    warning: str | None = None


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def parse_text(raw) -> str | None:
    """Trim a cell to clean text, or None when it holds nothing."""
    if raw is None:
        return None
    text = str(raw).replace(chr(0x00A0), " ").strip()
    return text or None


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------

def _strip_currency(text: str) -> str:
    upper = text.upper()
    for marker in _CURRENCY:
        upper = upper.replace(marker, "")
    return upper


def _split_sign(text: str) -> tuple[str, int]:
    """Pull the sign out, accepting -1, 1-, and (1) as accountants write it."""
    sign = 1
    if text.startswith("(") and text.endswith(")"):
        return text[1:-1], -1
    if text.startswith("-"):
        return text[1:], -1
    if text.startswith("+"):
        return text[1:], 1
    if text.endswith("-"):
        return text[:-1], -1
    return text, sign


def clean_amount_body(raw) -> str | None:
    """Reduce a cell to digits and separators, or None if it is not a number.

    Shared with `convention.py`, so the column scan and the parser always agree
    on what part of a cell is the number.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw)

    text = str(raw).strip()
    if not text:
        return None

    body = _strip_currency(text)
    for space in _SPACES:
        body = body.replace(space, "")
    body = body.strip()
    body, _sign = _split_sign(body)
    if not body or not _AMOUNT_SHAPE.match(body):
        return None
    return body


def _decimal_separator(body: str) -> tuple[str | None, str | None]:
    """Which character separates the decimals, and any warning that comes with it.

    Returns (separator, warning). A separator of None means the value is a whole
    number of euros.
    """
    last_dot = body.rfind(".")
    last_comma = body.rfind(",")

    # Both present: the rightmost one is the decimal point, the other groups
    # thousands. `1.234,56` and `1,234.56` are both unambiguous this way.
    if last_dot >= 0 and last_comma >= 0:
        return (".", None) if last_dot > last_comma else (",", None)

    separator = "." if last_dot >= 0 else ("," if last_comma >= 0 else None)
    if separator is None:
        return None, None

    if body.count(separator) > 1:
        return None, None                      # 1.234.567 — grouping only

    position = body.rfind(separator)
    digits_after = len(body) - position - 1

    if digits_after == 3:
        # Genuinely ambiguous on its own. `parse_amount` resolves it from the
        # column's convention, or refuses the value; never decided here.
        return None, None

    if digits_after == 0:
        return None, None                      # trailing separator, ignore

    return separator, None


def _validate_grouping(whole: str, raw: str) -> str:
    """Check a thousands-grouped integer part, and return it stripped of separators.

    `1.234.567` groups correctly; `12,34,56` does not, and is a malformed value
    rather than a large number. Accepting it would silently turn a typo into an
    amount on a customer's formal demand.
    """
    if "." in whole and "," in whole:
        raise ValueProblem(f"Montant illisible : « {raw} ».")

    separator = "." if "." in whole else ("," if "," in whole else None)
    if separator is None:
        return whole

    groups = whole.split(separator)
    if len(groups[0]) < 1 or len(groups[0]) > 3:
        raise ValueProblem(f"Montant illisible : « {raw} ».")
    if any(len(group) != 3 for group in groups[1:]):
        raise ValueProblem(f"Montant mal formé : « {raw} ».")
    return "".join(groups)


def _cents_from_parts(whole: str, decimals: str) -> tuple[int, str | None]:
    """Assemble cents from digit strings. Integer arithmetic only."""
    whole_value = int(whole) if whole else 0
    warning = None

    if not decimals:
        return whole_value * 100, None

    if len(decimals) == 1:
        return whole_value * 100 + int(decimals) * 10, None

    if len(decimals) == 2:
        return whole_value * 100 + int(decimals), None

    # More than two decimals: round half up, on the digits, and say so.
    kept = int(decimals[:2])
    if int(decimals[2]) >= 5:
        kept += 1
    warning = (
        f"Montant à {len(decimals)} décimales, arrondi au centime le plus proche."
    )
    return whole_value * 100 + kept, warning


def _ambiguous_shape(body: str) -> str | None:
    """The separator, when a value is the one shape a column has to resolve."""
    has_dot, has_comma = "." in body, "," in body
    if has_dot and has_comma:
        return None
    separator = "." if has_dot else ("," if has_comma else None)
    if separator is None or body.count(separator) > 1:
        return None
    if len(body) - body.rfind(separator) - 1 != 3:
        return None
    return separator


def parse_amount(raw, convention=None) -> Amount:
    """`1 234,56 €` -> 123456 cents. Never goes through a float.

    `convention` comes from `convention.detect_amount_convention()` over the
    whole column. Without one, a value whose separator could mean either thing
    is refused rather than guessed.
    """
    if raw is None:
        raise ValueProblem("Montant absent.")

    if isinstance(raw, bool):
        raise ValueProblem("Montant illisible.")

    if isinstance(raw, int):
        return Amount(raw * 100)

    text = str(raw).strip()
    if not text:
        raise ValueProblem("Montant absent.")

    body = clean_amount_body(text)
    if body is None:
        raise ValueProblem(f"Montant illisible : « {text} ».")

    _stripped, sign = _split_sign(
        "".join(ch for ch in _strip_currency(text) if ch not in _SPACES).strip()
    )

    ambiguous_separator = _ambiguous_shape(body)
    if ambiguous_separator is not None:
        if convention is None or not convention.settled:
            raise ValueProblem(
                f"« {text} » est ambigu : « {ambiguous_separator} » peut séparer "
                "les décimales ou les milliers, et rien dans cette colonne ne "
                "permet de trancher. Ligne écartée."
            )
        if convention.decimal == ambiguous_separator:
            separator = ambiguous_separator          # three decimals, to round
        else:
            separator = None                         # thousands grouping
    else:
        separator, _ = _decimal_separator(body)

    if separator is None:
        digits = _validate_grouping(body, text)
        if not _DIGITS_ONLY.match(digits or ""):
            raise ValueProblem(f"Montant illisible : « {text} ».")
        cents, rounding = _cents_from_parts(digits, "")
    else:
        position = body.rfind(separator)
        whole = _validate_grouping(body[:position], text)
        decimals = body[position + 1:]
        if whole and not _DIGITS_ONLY.match(whole):
            raise ValueProblem(f"Montant illisible : « {text} ».")
        if not _DIGITS_ONLY.match(decimals or "0"):
            raise ValueProblem(f"Montant illisible : « {text} ».")
        cents, rounding = _cents_from_parts(whole, decimals)

    return Amount(sign * cents, rounding)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _from_serial(number: int) -> date:
    from datetime import timedelta

    return _EXCEL_EPOCH + timedelta(days=number)


def _expand_year(year: int) -> int:
    if year >= 100:
        return year
    return 2000 + year if year < _CENTURY_PIVOT else 1900 + year


def _from_parts(first: int, second: int, third: int, raw: str) -> ParsedDate:
    """Day/month/year, French order, with the obvious exceptions handled."""
    year = _expand_year(third)

    if first > 12 and second > 12:
        raise ValueProblem(f"Date illisible : « {raw} ».")

    if second > 12:
        # Only reading makes sense: month first, American style.
        try:
            return ParsedDate(
                date(year, first, second),
                f"« {raw} » a été lu au format américain (mois/jour/année).",
            )
        except ValueError as error:
            raise ValueProblem(f"Date invalide : « {raw} ».") from error

    try:
        return ParsedDate(date(year, second, first))
    except ValueError as error:
        raise ValueProblem(f"Date invalide : « {raw} ».") from error


def parse_date(raw) -> ParsedDate:
    """Understands 31/12/2026, 2026-12-31, 20261231 and Excel serial numbers."""
    if raw is None:
        raise ValueProblem("Date absente.")

    if isinstance(raw, datetime):
        return ParsedDate(raw.date())
    if isinstance(raw, date):
        return ParsedDate(raw)

    if isinstance(raw, int) and not isinstance(raw, bool):
        return _serial_or_compact(raw, str(raw))

    text = str(raw).strip()
    if not text:
        raise ValueProblem("Date absente.")

    # Drop any time part: "2026-12-31 00:00:00", "31/12/2026 14:30".
    head = text.split("T")[0].split(" ")[0].strip()

    try:
        return ParsedDate(date.fromisoformat(head))
    except ValueError:
        pass

    if _DIGITS_ONLY.match(head):
        return _serial_or_compact(int(head), text)

    parts = [piece for piece in _DATE_SPLIT.split(head) if piece]
    if len(parts) == 3 and all(_DIGITS_ONLY.match(piece) for piece in parts):
        first, second, third = (int(piece) for piece in parts)
        if len(parts[0]) == 4:                      # 2026-12-31 with odd dashes
            try:
                return ParsedDate(date(first, second, third))
            except ValueError as error:
                raise ValueProblem(f"Date invalide : « {text} ».") from error
        return _from_parts(first, second, third, text)

    raise ValueProblem(f"Date illisible : « {text} ».")


def _serial_or_compact(number: int, raw: str) -> ParsedDate:
    """A bare number in a date column: either YYYYMMDD or an Excel serial."""
    digits = str(number)

    if len(digits) == 8:
        try:
            return ParsedDate(
                date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
            )
        except ValueError:
            pass                                    # fall through to serial

    if _EXCEL_MIN_SERIAL <= number <= _EXCEL_MAX_SERIAL:
        return ParsedDate(
            _from_serial(number),
            f"« {raw} » a été lu comme un numéro de série Excel.",
        )

    raise ValueProblem(f"Date illisible : « {raw} ».")
