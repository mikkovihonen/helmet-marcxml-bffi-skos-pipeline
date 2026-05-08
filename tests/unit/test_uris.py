"""URI-minting tests (M1).

Properties verified:
- stable across runs (deterministic hashing)
- sensitive to creator change
- sensitive to title (substantively different titles yield different URIs)
- insensitive to whitespace and Unicode normalisation form
- sensitive to language for Expressions, insensitive to its casing
- pinned regression: explicit input -> expected output pair
"""

from __future__ import annotations

import unicodedata

from bffi_pipeline.uris import mint_expression_uri, mint_work_uri

CREATOR = "http://example.org/agent/Tolstoy"
TITLE = "Sota ja rauha"

WORK_NS = "http://urn.fi/URN:NBN:fi:bib:work:"
EXPR_NS = "http://urn.fi/URN:NBN:fi:bib:expression:"


def test_work_uri_is_stable_across_runs() -> None:
    assert mint_work_uri(CREATOR, TITLE) == mint_work_uri(CREATOR, TITLE)


def test_work_uri_uses_committed_namespace() -> None:
    uri = mint_work_uri(CREATOR, TITLE)
    assert uri.startswith(WORK_NS)
    suffix = uri.removeprefix(WORK_NS)
    assert len(suffix) == 40
    assert all(c in "0123456789abcdef" for c in suffix)


def test_work_uri_is_sensitive_to_creator() -> None:
    a = mint_work_uri("http://example.org/agent/Tolstoy", TITLE)
    b = mint_work_uri("http://example.org/agent/Dostoevsky", TITLE)
    assert a != b


def test_work_uri_is_sensitive_to_title() -> None:
    a = mint_work_uri(CREATOR, "Sota ja rauha")
    b = mint_work_uri(CREATOR, "Anna Karenina")
    assert a != b


def test_work_uri_is_insensitive_to_whitespace() -> None:
    a = mint_work_uri(CREATOR, "Sota ja rauha")
    b = mint_work_uri(CREATOR, "  Sota   ja\trauha  ")
    assert a == b


def test_work_uri_is_insensitive_to_case() -> None:
    a = mint_work_uri(CREATOR, "Sota ja rauha")
    b = mint_work_uri(CREATOR, "SOTA JA RAUHA")
    assert a == b


def test_work_uri_is_insensitive_to_unicode_normalization_form() -> None:
    nfc = "Pää"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert mint_work_uri(CREATOR, nfc) == mint_work_uri(CREATOR, nfd)


def test_work_uri_preserves_diacritics() -> None:
    # Helmet is multilingual; ä/a must be distinct.
    a = mint_work_uri(CREATOR, "Pää")
    b = mint_work_uri(CREATOR, "Paa")
    assert a != b


def test_work_uri_is_insensitive_to_creator_uri_whitespace() -> None:
    a = mint_work_uri(CREATOR, TITLE)
    b = mint_work_uri(f"  {CREATOR}\n", TITLE)
    assert a == b


def test_work_uri_pinned_regression() -> None:
    # Recompute only when the canonicalisation scheme intentionally changes
    # (and surface that change before doing so — Work URIs are committed).
    expected = WORK_NS + "fdca44e32ce7f64bd6310dd4130bb0db383637fa"
    assert mint_work_uri(CREATOR, TITLE) == expected


def test_expression_uri_is_stable_across_runs() -> None:
    work = mint_work_uri(CREATOR, TITLE)
    assert mint_expression_uri(work, "fi") == mint_expression_uri(work, "fi")


def test_expression_uri_uses_committed_namespace() -> None:
    work = mint_work_uri(CREATOR, TITLE)
    expr = mint_expression_uri(work, "fi")
    assert expr.startswith(EXPR_NS)


def test_expression_uri_is_sensitive_to_language() -> None:
    work = mint_work_uri(CREATOR, TITLE)
    fi = mint_expression_uri(work, "fi")
    sv = mint_expression_uri(work, "sv")
    en = mint_expression_uri(work, "en")
    assert fi != sv
    assert fi != en
    assert sv != en


def test_expression_uri_is_insensitive_to_language_casing() -> None:
    work = mint_work_uri(CREATOR, TITLE)
    assert mint_expression_uri(work, "fi") == mint_expression_uri(work, "FI")
    assert mint_expression_uri(work, "fi") == mint_expression_uri(work, " fi ")


def test_expression_uri_is_sensitive_to_work() -> None:
    a = mint_work_uri(CREATOR, "Sota ja rauha")
    b = mint_work_uri(CREATOR, "Anna Karenina")
    assert mint_expression_uri(a, "fi") != mint_expression_uri(b, "fi")


def test_expression_uri_pinned_regression() -> None:
    work = mint_work_uri(CREATOR, TITLE)
    expected = EXPR_NS + "029107474465e2d0ffc8dde5da55707cf5635792"
    assert mint_expression_uri(work, "fi") == expected
