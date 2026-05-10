"""Unit tests for ``bffi_pipeline.helmet`` — Sierra-style bib number
formatting. The 13 ``VERIFIED_IDS`` below were supplied by Helmet
cataloguers and pin down the III/Sierra modulus-11 check-digit
algorithm; if any of these fail, the algorithm in ``helmet.py`` is
wrong and the bib numbers we surface to cataloguers will mislead."""

from __future__ import annotations

import pytest

from bffi_pipeline.helmet import compute_check_digit, format_sierra_bib_id

# Cataloguer-confirmed (bare bib number, full Sierra-style display form) pairs.
VERIFIED_IDS: list[tuple[str, str]] = [
    ("2628274", "b26282744"),
    ("1059592", "b10595922"),
    ("1769634", "b17696343"),
    ("2372028", "b23720281"),
    ("2452306", "b24523069"),
    ("2484550", "b24845504"),
    ("2616222", "b26162222"),
    ("2602288", "b26022886"),
    ("2576727", "b25767276"),
    ("2564382", "b25643824"),
    ("2360958", "b23609588"),
    ("2620193", "b26201938"),
    ("2371438", "b23714384"),
]


@pytest.mark.parametrize(("bib_id", "expected"), VERIFIED_IDS)
def test_format_sierra_bib_id_matches_cataloguer_confirmed_examples(
    bib_id: str, expected: str
) -> None:
    assert format_sierra_bib_id(bib_id) == expected


def test_compute_check_digit_returns_x_when_remainder_is_ten() -> None:
    """III/Sierra renders a check digit of 10 as the literal letter
    ``x``. None of the 13 verified IDs hit that branch, so we
    construct a bib number that does: weights 2..8 over digits
    9999995 sum to 307, and 307 mod 11 == 10."""
    assert compute_check_digit("9999995") == "x"
    assert format_sierra_bib_id("9999995") == "b9999995x"
