"""Unit tests for ``bffi_pipeline.helmet`` — Sierra-style bib number
formatting. Trivial today; the placeholder check digit is the
interesting bit and the docstring on ``format_sierra_bib_id`` covers
why we emit ``0`` rather than computing the real modulus-11 digit."""

from __future__ import annotations

from bffi_pipeline.helmet import format_sierra_bib_id


def test_format_sierra_bib_id_prefixes_b_and_appends_zero() -> None:
    assert format_sierra_bib_id("2635521") == "b26355210"


def test_format_sierra_bib_id_handles_leading_zero_bib_numbers() -> None:
    """Some legacy Helmet records have shorter bib numbers; the helper
    must not strip leading zeros — Sierra treats them as significant."""
    assert format_sierra_bib_id("00012345") == "b000123450"
