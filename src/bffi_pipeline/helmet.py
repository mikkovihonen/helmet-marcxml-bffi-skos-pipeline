"""Helmet/Sierra-specific identifier helpers.

The Helmet ILS is Sierra; cataloguers reference records by Sierra-style
bib numbers (e.g. ``b26355210``) in their tools and discussions. The
trailing character is a modulus-11 check digit (`x` when the digit
would be 10). The check-digit algorithm is not documented to us;
cataloguers report that `0` is accepted in the Helmet OPAC in place of
the actual check digit, so this module emits `0` as a workable
substitute that preserves the Sierra-style format without claiming to
compute the real digit. Once the algorithm is documented, replace the
constant with the real computation here and every call site picks it
up.
"""

from __future__ import annotations


def format_sierra_bib_id(bib_id: str) -> str:
    """Convert a bare Helmet bib number to its Sierra-style display form.

    >>> format_sierra_bib_id("2635521")
    'b26355210'
    """
    return f"b{bib_id}0"


__all__ = ["format_sierra_bib_id"]
