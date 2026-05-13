"""Unit tests for ``bffi_pipeline.blocking``'s Phase C tier-0 helpers.

``fold_label`` and ``strip_label_decoration`` are the canonical
cataloguer-literal-to-authority-label fold composed by the Phase C
tier-0 expansion path. The same fold runs both at load-finto time
(materialising ``bffi:foldedLabel``) and at lookup time (resolver's
folded SPARQL query), so the byte-for-byte alignment between the
two sides is a hard contract — these tests pin it.
"""

from __future__ import annotations

import pytest

from bffi_pipeline.blocking import fold_label, strip_label_decoration

# --- strip_label_decoration ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Trailing parenthetical date — every shape the rule catches.
        ("Tolkien, J. R. R. (1892-1973)", "Tolkien, J. R. R."),
        ("Hamilton, Guy. (1923-)", "Hamilton, Guy."),
        ("Beethoven (1770-1827)", "Beethoven"),
        ("Forster, E. M. (1879-1970)", "Forster, E. M."),
        # En-dash date form (KANTO uses U+2013 occasionally).
        ("Sibelius, Jean (1865–1957)", "Sibelius, Jean"),  # noqa: RUF001
        # No-date birth-only (just the year).
        ("Author, New (1990)", "Author, New"),
    ],
)
def test_strip_label_decoration_drops_trailing_parenthetical_date(raw: str, expected: str) -> None:
    assert strip_label_decoration(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Finnish role markers.
        ("Hamilton, Guy, ohjaaja", "Hamilton, Guy"),
        ("Barry, John, säveltäjä", "Barry, John"),
        ("Ekland, Britt, näyttelijä", "Ekland, Britt"),
        # Swedish role markers.
        ("Bergman, Ingmar, regissör", "Bergman, Ingmar"),
        ("Lindgren, Astrid, författare", "Lindgren, Astrid"),
        # Whitespace tolerated around the role token.
        ("Author, Foo ,   ohjaaja  ", "Author, Foo"),
        # Multiple commas — only the trailing role-marker segment is stripped.
        ("Doe, John, A., säveltäjä", "Doe, John, A."),
    ],
)
def test_strip_label_decoration_drops_marc_e_role_marker(raw: str, expected: str) -> None:
    assert strip_label_decoration(raw) == expected


def test_strip_label_decoration_preserves_fictional_character_marker() -> None:
    """``(fiktiivinen hahmo)`` is routed through the dedicated fictional-
    character path in M9; the parenthetical-date rule must not match it
    (no digits inside the parens), and there's no role-marker whitelist
    entry for it."""
    assert (
        strip_label_decoration("Bond, James (fiktiivinen hahmo)")
        == "Bond, James (fiktiivinen hahmo)"
    )
    assert (
        strip_label_decoration("Holmes, Sherlock (fiktiv gestalt)")
        == "Holmes, Sherlock (fiktiv gestalt)"
    )


def test_strip_label_decoration_drops_both_when_both_present() -> None:
    """Date strip runs first; role-marker strip then sees the dateless tail.
    Order matters because the date often trails the role marker in the
    cataloguer-side literal."""
    assert strip_label_decoration("Hamilton, Guy, ohjaaja (1923-)") == "Hamilton, Guy"


def test_strip_label_decoration_passes_through_clean_labels() -> None:
    """A literal that needs no stripping must return verbatim — the
    Phase C path must not perturb the common case."""
    assert strip_label_decoration("Tolstoy, Leo,") == "Tolstoy, Leo,"
    assert strip_label_decoration("Helsinki") == "Helsinki"


# --- fold_label ------------------------------------------------------------


def test_fold_label_casefolds_and_strips_decoration() -> None:
    assert fold_label("Tolkien, J. R. R. (1892-1973)") == "tolkien, j. r. r."


def test_fold_label_strips_role_marker_then_folds() -> None:
    assert fold_label("Hamilton, Guy, ohjaaja") == "hamilton, guy"


def test_fold_label_collapses_internal_whitespace() -> None:
    assert fold_label("Tolstoy,    Leo") == "tolstoy, leo"


def test_fold_label_folds_foreign_diacritics() -> None:
    """Müller / Muller align; LINDGRÉN / Lindgren align; Tolstoï /
    Tolstoi align — same fold the post-Phase-C tier-0 path matches on."""
    assert fold_label("Müller") == fold_label("Muller") == "muller"
    assert fold_label("LINDGRÉN, Astrid") == fold_label("Lindgren, Astrid") == "lindgren, astrid"
    assert fold_label("Tolstoï") == fold_label("Tolstoi") == "tolstoi"


def test_fold_label_preserves_native_finnish_swedish_diacritics() -> None:
    """Lexemic distinctions must survive the fold. ``Häme`` (region) and
    ``hame`` (skirt) must not collide; ``Yrjö`` (given name) and
    ``Yrjo`` (gibberish) must not collide; ``Ångström`` keeps both
    ``Å`` and ``ö``."""
    assert fold_label("Häme") != fold_label("Hame")
    assert fold_label("Yrjö") != fold_label("Yrjo")
    assert fold_label("Ångström") == "ångström"


def test_fold_label_nfkc_normalises_compatibility_variants() -> None:
    """Fullwidth digit U+FF11 normalises to ASCII ``1`` so a date
    variant with fullwidth digits strips the same as ``(1923-)`` after
    NFKC. Pathological case but cheap insurance."""
    assert fold_label("Author (１９２３-)") == fold_label("Author (1923-)")  # noqa: RUF001


def test_fold_label_empty_input_returns_empty() -> None:
    """No crash on degenerate input; ``""`` and pure-whitespace fold to ``""``."""
    assert fold_label("") == ""
    assert fold_label("   ") == ""
