"""Unit tests for stages/workkey (Stage 1 deterministic blocking, M4)."""

from __future__ import annotations

import pytest

from bffi_pipeline.stages.workkey import compute_blocking_key

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
        # Diacritic-only differences collapse — these are the transliteration
        # variants accent folding can resolve at Stage 1. Variants that differ
        # by an actual letter ("Tolstoy" vs "Tolstoï") are not caught here;
        # the M5 embedding stage owns cross-block recall for those.
        ("Tolstoï, Leo,", "Tolstoi, Leo,"),
        ("Tolstoy, Leo,", "TOLSTOY, LEO,"),
        ("Linna, Väinö,", "Linna, Vaino,"),
        ("Lindgren, Astrid,", "LINDGRÉN, Astrid,"),
    ],
)
def test_accent_and_case_folded_surnames_share_a_block(creator_a: str, creator_b: str) -> None:
    base = {"title": "Sota ja rauha", "content_type": "txt"}
    a = compute_blocking_key({**base, "creator": creator_a})
    b = compute_blocking_key({**base, "creator": creator_b})
    assert a == b


def test_accent_folding_in_title() -> None:
    base = {"creator": "Helsingin yliopisto", "content_type": "txt"}
    a = compute_blocking_key({**base, "title": "Tieteessä tapahtuu"})
    b = compute_blocking_key({**base, "title": "Tieteessa tapahtuu"})
    assert a == b


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
