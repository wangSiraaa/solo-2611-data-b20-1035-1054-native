"""Exact positive-rational parsing (no floating-point approximation).

Conversion ratios, exchange-window ratios and credit remainders are all exact
:class:`fractions.Fraction` values.  Floats (``1.5``) and booleans are rejected
outright so a caller can never smuggle a binary approximation into a pool that
is later floored to a stampable credit.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Optional

from .errors import SchemaError

# Contract scale for the small hearing examples ("小例最多八种校别学时、十窗、六步").
MAX_STEPS = 6
MAX_WINDOWS = 10
MAX_NODE_KINDS = 8


def parse_positive_rational(value: Any, *, location: Optional[str] = None) -> Fraction:
    """Parse an exact positive rational.

    Accepted spellings:

    * ``int`` / ``Fraction``            (positive)
    * ``"3/2"`` or ``"3"``              (anything ``Fraction`` parses exactly)
    * ``[3, 2]`` / ``(3, 2)``           (numerator, denominator)
    * ``{"numerator": 3, "denominator": 2}``

    Rejected: ``bool``, ``float`` (binary approximation), zero or negative.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise SchemaError(
            "ratio must be an exact rational (int, 'n/d', [n,d]); floats are not accepted",
            location=location,
        )

    if isinstance(value, Fraction):
        frac = value
    elif isinstance(value, int):
        frac = Fraction(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise SchemaError("ratio string must not be empty", location=location)
        try:
            frac = Fraction(text)
        except (ValueError, ZeroDivisionError) as exc:
            raise SchemaError(f"invalid rational {value!r}: {exc}", location=location) from None
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        num, den = value
        for part in (num, den):
            if isinstance(part, bool) or not isinstance(part, int):
                raise SchemaError("ratio pair must be two integers", location=location)
        if den == 0:
            raise SchemaError("ratio denominator must not be zero", location=location)
        frac = Fraction(num, den)
    elif isinstance(value, dict) and "numerator" in value and "denominator" in value:
        num, den = value["numerator"], value["denominator"]
        for part in (num, den):
            if isinstance(part, bool) or not isinstance(part, int):
                raise SchemaError("ratio numerator/denominator must be integers", location=location)
        if den == 0:
            raise SchemaError("ratio denominator must not be zero", location=location)
        frac = Fraction(num, den)
    else:
        raise SchemaError(
            "ratio must be an int, Fraction, 'n/d' string, [n,d] pair or "
            "{numerator, denominator} object",
            location=location,
        )

    if frac <= 0:
        raise SchemaError("ratio must be strictly positive", location=location)
    return frac


def format_ratio(frac: Fraction) -> str:
    """Render a Fraction as ``"n"`` or ``"n/d"`` (never a decimal)."""
    if frac.denominator == 1:
        return str(frac.numerator)
    return f"{frac.numerator}/{frac.denominator}"


def floor_fraction(frac: Fraction) -> int:
    """Integer floor of a non-negative Fraction."""
    return frac.numerator // frac.denominator


def validate_steps(max_steps: Any, *, location: str = "max_steps") -> int:
    if isinstance(max_steps, bool) or not isinstance(max_steps, int):
        raise SchemaError("max_steps must be an integer", location=location)
    if not (1 <= max_steps <= MAX_STEPS):
        raise SchemaError(f"max_steps must be in 1..{MAX_STEPS} for the small-scale contract", location=location)
    return max_steps


__all__ = [
    "parse_positive_rational",
    "format_ratio",
    "floor_fraction",
    "validate_steps",
    "MAX_STEPS",
    "MAX_WINDOWS",
    "MAX_NODE_KINDS",
]
