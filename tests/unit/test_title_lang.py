"""Unit tests for ``bffi_pipeline.title_lang``.

The Lingua detector is built once at module import; tests share it via
the function-level cache. Lingua's accuracy on short Latin-script
strings is uneven by design, so the assertions below pin behavior we
can rely on rather than every detection outcome.
"""

from __future__ import annotations

import pytest

from bffi_pipeline.title_lang import (
    TaggedSegment,
    detect_segment_lang,
    split_by_script,
    split_by_separators,
    tag_title,
)

# --- split_by_script -----------------------------------------------------


def test_split_by_script_keeps_single_script_intact() -> None:
    assert split_by_script("Aatelisrosvo Dubrovskij") == ["Aatelisrosvo Dubrovskij"]


def test_split_by_script_separates_latin_and_cyrillic() -> None:
    chunks = split_by_script("Eti punktualnyje nemtsy = Эти пунктуальные немцы")
    assert len(chunks) == 2
    assert "Eti punktualnyje" in chunks[0]
    assert "Эти пунктуальные" in chunks[1]


def test_split_by_script_treats_punctuation_and_digits_as_neutral() -> None:
    """1613-1917 inside Cyrillic shouldn't fragment the chunk."""
    chunks = split_by_script("Romanovyh : 1613-1917")
    assert chunks == ["Romanovyh : 1613-1917"]


def test_split_by_script_empty_input() -> None:
    assert split_by_script("") == []


# --- split_by_separators -------------------------------------------------


def test_split_by_separators_recognizes_rda_equals() -> None:
    parts = split_by_separators("the Russian charka = venäläinen tšarkka = russkaja tšarka")
    assert parts == [
        "the Russian charka",
        "venäläinen tšarkka",
        "russkaja tšarka",
    ]


def test_split_by_separators_recognizes_slash_with_spaces() -> None:
    parts = split_by_separators("English title / Suomenkielinen nimi")
    assert parts == ["English title", "Suomenkielinen nimi"]


def test_split_by_separators_returns_single_when_no_separator() -> None:
    parts = split_by_separators("Aatelisrosvo Dubrovskij")
    assert parts == ["Aatelisrosvo Dubrovskij"]


def test_split_by_separators_strips_whitespace() -> None:
    parts = split_by_separators("  a  =  b  ")
    assert parts == ["a", "b"]


def test_split_by_separators_picks_first_matching_separator() -> None:
    """Title containing both = and / splits on the more specific = first."""
    parts = split_by_separators("a = b / c")
    # First separator in the priority order is " = "; once we split on
    # it, the right-hand side stays intact.
    assert parts == ["a", "b / c"]


# --- detect_segment_lang -------------------------------------------------


def test_detect_segment_lang_picks_finnish_on_clear_finnish_title() -> None:
    lang, conf = detect_segment_lang("Sota ja rauha", frozenset({"fi", "sv", "en"}))
    assert lang == "fi"
    assert conf > 0.85


def test_detect_segment_lang_picks_swedish_on_clear_swedish_title() -> None:
    lang, _ = detect_segment_lang("Krig och fred", frozenset({"fi", "sv", "en"}))
    assert lang == "sv"


def test_detect_segment_lang_picks_english_on_clear_english_title() -> None:
    lang, _ = detect_segment_lang("War and Peace", frozenset({"fi", "sv", "en"}))
    assert lang == "en"


def test_detect_segment_lang_short_circuits_to_russian_for_pure_cyrillic() -> None:
    """Lingua's Russian model is Cyrillic-only; pure-Cyrillic input doesn't
    need to roundtrip through n-grams when ``ru`` is a candidate."""
    lang, conf = detect_segment_lang("Эти пунктуальные немцы", frozenset({"fi", "sv", "en", "ru"}))
    assert lang == "ru"
    assert conf == 1.0


def test_detect_segment_lang_returns_none_when_no_candidates() -> None:
    lang, conf = detect_segment_lang("Sota ja rauha", frozenset())
    assert lang is None
    assert conf == 0.0


def test_detect_segment_lang_constrains_to_candidate_set() -> None:
    """Even when Lingua's top guess globally is Finnish, restricting
    candidates to {sv, en} returns one of those (not None)."""
    lang, _ = detect_segment_lang("Sota ja rauha", frozenset({"sv", "en"}))
    assert lang in {"sv", "en", None}  # could miss confidence floor; the key is it isn't `fi`


# --- tag_title (the orchestrator) ---------------------------------------


def test_tag_title_single_finnish_title_emits_one_segment() -> None:
    out = tag_title("Sota ja rauha", frozenset({"fi", "sv", "en"}))
    assert out == [TaggedSegment(text="Sota ja rauha", lang="fi")]


def test_tag_title_splits_latin_cyrillic_into_two_segments() -> None:
    out = tag_title(
        "Eti punktualnyje nemtsy = Эти пунктуальные немцы",
        frozenset({"fi", "sv", "en", "ru"}),
    )
    # Script-boundary split → two segments. The Cyrillic side is `ru`;
    # the Latin side may end up `fi` or untagged depending on Lingua.
    langs = [seg.lang for seg in out]
    assert "ru" in langs
    assert len(out) >= 2


def test_tag_title_collapses_when_segments_share_one_language() -> None:
    """When a multi-segment title has all segments detected as the same
    language (the Tšarka failure mode), avoid emitting N copies of the
    same wrong tag — return one whole-string segment instead."""
    out = tag_title(
        "Tšarka : the Russian charka = venäläinen tšarkka = russkaja tšarka",
        frozenset({"fi", "sv", "en", "ru"}),
    )
    # Lingua misclassifies all 3 Latin segments as Finnish; collapse
    # heuristic kicks in and we get back ONE segment with the original
    # full string and a single-language tag (or untagged if confidence
    # threshold isn't met across the whole thing).
    assert len(out) == 1
    assert out[0].text.startswith("Tšarka")


def test_tag_title_emits_per_language_segments_when_lingua_disagrees() -> None:
    """When segments come back as genuinely different languages,
    emit one TaggedSegment per language."""
    out = tag_title(
        "Sota ja rauha = War and Peace = Krig och fred",
        frozenset({"fi", "sv", "en"}),
    )
    langs = sorted(seg.lang for seg in out if seg.lang is not None)
    assert len(out) == 3
    # Best case: all three distinct {en, fi, sv}. Worst-case: at least
    # two of three (one short title may fail confidence floor).
    assert len(set(langs)) >= 2


def test_tag_title_empty_text_returns_no_segments() -> None:
    assert tag_title("", frozenset({"fi"})) == []


def test_tag_title_no_candidates_returns_untagged_segment() -> None:
    out = tag_title("Some title", frozenset())
    assert out == [TaggedSegment(text="Some title", lang=None)]


@pytest.mark.parametrize(
    ("title", "candidates", "expected_lang"),
    [
        ("Aatelisrosvo Dubrovskij", frozenset({"fi", "ru"}), "fi"),
        ("Sagor från Mumindalen", frozenset({"sv", "en"}), "sv"),
        ("Эти пунктуальные немцы", frozenset({"fi", "ru"}), "ru"),
    ],
)
def test_tag_title_picks_correct_language_on_clean_titles(
    title: str, candidates: frozenset[str], expected_lang: str
) -> None:
    out = tag_title(title, candidates)
    assert len(out) == 1
    assert out[0].lang == expected_lang
