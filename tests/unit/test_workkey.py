"""Unit tests for stages/workkey (Stage 1 deterministic blocking, M4)."""

from __future__ import annotations

import pytest

from bffi_pipeline.blocking import compute_blocking_key

# -- Stability + structure ------------------------------------------------


def test_key_is_stable() -> None:
    work = {
        "creator": "Tolstoy, Leo, 1828-1910",
        "title": "Sota ja rauha",
        "content_type": "txt",
    }
    assert compute_blocking_key(work) == compute_blocking_key(work)


def test_key_has_three_pipe_separated_segments() -> None:
    key = compute_blocking_key(
        {
            "creator": "Tolstoy, Leo,",
            "title": "Sota ja rauha",
            "content_type": "txt",
        }
    )
    assert key.count("|") == 2


# -- Surname collapsing across given-name variation -----------------------


def test_same_surname_different_given_names_share_a_block() -> None:
    a = compute_blocking_key(
        {"creator": "Tolstoy, Leo,", "title": "Anna Karenina", "content_type": "txt"}
    )
    b = compute_blocking_key(
        {"creator": "Tolstoy, Lev,", "title": "Anna Karenina", "content_type": "txt"}
    )
    assert a == b


def test_distinct_surnames_yield_different_keys() -> None:
    a = compute_blocking_key(
        {"creator": "Tolstoy, Leo,", "title": "Anna Karenina", "content_type": "txt"}
    )
    b = compute_blocking_key(
        {"creator": "Dostoevsky, Fyodor,", "title": "Anna Karenina", "content_type": "txt"}
    )
    assert a != b


# -- Accent folding (transliteration / diacritics) ------------------------


@pytest.mark.parametrize(
    ("creator_a", "creator_b"),
    [
        # Foreign-diacritic + case differences collapse: cataloguers may
        # transcribe `ï`, `É`, etc. inconsistently across source records,
        # and these variants must still block together.
        ("Tolstoï, Leo,", "Tolstoi, Leo,"),
        ("Tolstoy, Leo,", "TOLSTOY, LEO,"),
        ("Lindgren, Astrid,", "LINDGRÉN, Astrid,"),
    ],
)
def test_foreign_diacritic_and_case_folded_surnames_share_a_block(
    creator_a: str, creator_b: str
) -> None:
    base = {"title": "Sota ja rauha", "content_type": "txt"}
    a = compute_blocking_key({**base, "creator": creator_a})
    b = compute_blocking_key({**base, "creator": creator_b})
    assert a == b


def test_native_diacritics_in_title_are_preserved() -> None:
    """Finnish ``ä`` carries lexemic meaning — ``Tieteessä`` (in science)
    must not block together with the gibberish ``Tieteessa`` (no real word).
    """
    base = {"creator": "Helsingin yliopisto", "content_type": "txt"}
    a = compute_blocking_key({**base, "title": "Tieteessä tapahtuu"})
    b = compute_blocking_key({**base, "title": "Tieteessa tapahtuu"})
    assert a != b


def test_native_diacritic_titles_block_together_when_identical() -> None:
    """Two records that both use the proper Finnish form share a block."""
    base = {"creator": "Helsingin yliopisto", "content_type": "txt"}
    a = compute_blocking_key({**base, "title": "Tieteessä tapahtuu"})
    b = compute_blocking_key({**base, "title": "tieteessä tapahtuu"})  # case difference only
    assert a == b
    assert "tieteessa" not in a  # the ä is preserved in the key
    assert "tieteessä" in a


def test_finnish_skirt_vs_region_do_not_share_a_block() -> None:
    """The user-supplied canonical example: ``Häme`` (region) vs ``hame``
    (skirt) are different lexemes; their blocks must not collide."""
    base = {"creator": "Anon, A,", "content_type": "txt"}
    region = compute_blocking_key({**base, "title": "Häme"})
    skirt = compute_blocking_key({**base, "title": "Hame"})
    assert region != skirt


def test_native_diacritics_in_surnames_are_preserved() -> None:
    """``Yrjö`` and ``Yrjo`` are different surnames; KANTO disambiguates
    these too. Even at the coarser blocking-key resolution we don't want
    them in the same block."""
    base = {"title": "Sota ja rauha", "content_type": "txt"}
    real = compute_blocking_key({**base, "creator": "Yrjö, Anna,"})
    folded = compute_blocking_key({**base, "creator": "Yrjo, Anna,"})
    assert real != folded


# -- Title stop-word skipping ---------------------------------------------


def test_leading_article_is_skipped_when_picking_significant_token() -> None:
    a = compute_blocking_key(
        {"creator": "Linna, Väinö,", "title": "The Unknown Soldier", "content_type": "txt"}
    )
    b = compute_blocking_key(
        {"creator": "Linna, Väinö,", "title": "Unknown Soldier", "content_type": "txt"}
    )
    assert a == b
    # ... and "the" must not have ended up in the key.
    assert "|the|" not in a


def test_swedish_article_is_skipped() -> None:
    a = compute_blocking_key(
        {"creator": "Lindgren, Astrid,", "title": "En annan värld", "content_type": "txt"}
    )
    b = compute_blocking_key(
        {"creator": "Lindgren, Astrid,", "title": "annan värld", "content_type": "txt"}
    )
    assert a == b


# -- Punctuation stripping -------------------------------------------------


def test_trailing_punctuation_in_title_is_stripped() -> None:
    a = compute_blocking_key(
        {"creator": "Sibelius, Jean,", "title": "Finlandia, op. 26 :", "content_type": "ntm"}
    )
    b = compute_blocking_key(
        {"creator": "Sibelius, Jean,", "title": "Finlandia", "content_type": "ntm"}
    )
    assert a == b


# -- Content type --------------------------------------------------------


def test_different_content_types_yield_different_keys() -> None:
    base = {"creator": "Sibelius, Jean,", "title": "Finlandia"}
    text = compute_blocking_key({**base, "content_type": "txt"})
    score = compute_blocking_key({**base, "content_type": "ntm"})
    assert text != score


def test_full_content_type_uri_is_accepted() -> None:
    short = compute_blocking_key({"creator": "X, Y,", "title": "Z", "content_type": "txt"})
    full = compute_blocking_key(
        {
            "creator": "X, Y,",
            "title": "Z",
            "content_type": "http://id.loc.gov/vocabulary/contentTypes/txt",
        }
    )
    assert short == full


# -- Missing fields --------------------------------------------------------


def test_missing_creator_uses_anon_placeholder() -> None:
    key = compute_blocking_key({"creator": None, "title": "Anonymous Work", "content_type": "txt"})
    assert key.startswith("anon|")


def test_missing_title_uses_untitled_placeholder() -> None:
    key = compute_blocking_key({"creator": "X, Y,", "title": "", "content_type": "txt"})
    assert key == "x|untitled|txt"


def test_missing_content_type_uses_unk_placeholder() -> None:
    key = compute_blocking_key({"creator": "X, Y,", "title": "Z"})
    assert key.endswith("|unk")
