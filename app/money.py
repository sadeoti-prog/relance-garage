"""Integer money arithmetic and French formatting.

Every amount in the system is an integer number of cents. There is no float
anywhere in the money path, and no amount is ever produced by the language
model: this module is the single place where money is computed and rendered.
"""

from __future__ import annotations

from decimal import Decimal

NBSP = "\u00a0"  # espace insécable, French typography


def euros(value: str | int | float | Decimal) -> int:
    """Convert a euro amount to cents, rounding half up."""
    d = Decimal(str(value)) * 100
    return int(d.to_integral_value(rounding="ROUND_HALF_UP"))


def apply_rate_bp(amount_cents: int, rate_bp: int) -> int:
    """Apply a rate expressed in basis points, rounded half up."""
    return (amount_cents * rate_bp + 5_000) // 10_000


def line_total(unit_price_cents: int, quantity_milli: int) -> int:
    """Unit price x quantity, where quantity is stored x1000 (1,5 h -> 1500)."""
    return (unit_price_cents * quantity_milli + 500) // 1000


def vat_of(total_ht_cents: int, vat_rate_bp: int) -> int:
    return apply_rate_bp(total_ht_cents, vat_rate_bp)


def prorata_annual(amount_cents: int, annual_rate_bp: int, days: int) -> int:
    """Interest accrued over `days` at an annual rate, rounded half up.

    Uses a 365-day year, integer arithmetic throughout.
    """
    if days <= 0 or amount_cents <= 0 or annual_rate_bp <= 0:
        return 0
    numerator = amount_cents * annual_rate_bp * days
    return (numerator + 365 * 10_000 // 2) // (365 * 10_000)


def format_eur(cents: int, with_symbol: bool = True) -> str:
    """Format cents the French way: 1 234,56 €"""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    whole, frac = divmod(cents, 100)
    groups = []
    s = str(whole)
    while len(s) > 3:
        groups.insert(0, s[-3:])
        s = s[:-3]
    groups.insert(0, s)
    body = NBSP.join(groups)
    out = f"{sign}{body},{frac:02d}"
    return f"{out}{NBSP}€" if with_symbol else out


def format_qty(quantity_milli: int) -> str:
    """Format a x1000 quantity: 1500 -> '1,5', 2000 -> '2'."""
    whole, frac = divmod(quantity_milli, 1000)
    if frac == 0:
        return str(whole)
    return f"{whole},{frac:03d}".rstrip("0")
