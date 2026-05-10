"""Helmet/Sierra-specific identifier helpers.

The Helmet ILS is Sierra (Innovative Interfaces); cataloguers reference
records by Sierra-style bib numbers (e.g. ``b26282744``) in their tools
and discussions. The trailing character is a modulus-11 check digit;
``x`` when the digit would be 10.

Algorithm (III/Sierra modulus-11):

1. Multiply each digit of the bare bib number by its position from the
   right, starting at 2 (rightmost digit ✕ 2, next ✕ 3, ...).
2. Sum.
3. ``sum % 11``: a result of 0-9 is the check digit; 10 is rendered as
   ``x``.

Verified against 13 cataloguer-confirmed Helmet IDs (see
``tests/unit/test_helmet.py``).
"""

from __future__ import annotations

from typing import Final

_MODULUS: Final[int] = 11
_X_REMAINDER: Final[int] = 10  # remainder of 10 is rendered as the letter 'x'


def compute_check_digit(bib_id: str) -> str:
    """Return the III/Sierra modulus-11 check digit for a bare bib number.

    >>> compute_check_digit("2628274")
    '4'
    >>> compute_check_digit("2452306")
    '9'
    """
    total = sum(int(digit) * (i + 2) for i, digit in enumerate(reversed(bib_id)))
    remainder = total % _MODULUS
    return "x" if remainder == _X_REMAINDER else str(remainder)


def format_sierra_bib_id(bib_id: str) -> str:
    """Convert a bare Helmet bib number to its Sierra-style display form.

    >>> format_sierra_bib_id("2628274")
    'b26282744'
    """
    return f"b{bib_id}{compute_check_digit(bib_id)}"


__all__ = ["compute_check_digit", "format_sierra_bib_id"]
