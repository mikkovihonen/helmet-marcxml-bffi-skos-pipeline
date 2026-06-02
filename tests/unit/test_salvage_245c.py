"""Unit tests for P-41 Phase B1 — deterministic 245$c parser."""

from __future__ import annotations

import pytest

from bffi_pipeline.stages.m2.salvage_245c import ParsedAgent, parse_245c


class TestRoleMarkedShapes:
    """Each role marker in :data:`_ROLE_MARKERS` should drive both the
    role tag and the confidence band correctly."""

    def test_finnish_kirjoittanut_marker_yields_author_role(self) -> None:
        agents = parse_245c("kirjoittanut Mika Waltari")
        assert agents == [ParsedAgent(name="Mika Waltari", role="author", confidence=0.8)]

    def test_finnish_toim_abbreviation_yields_editor_role(self) -> None:
        agents = parse_245c("toim. Helena Ruuska")
        assert agents == [ParsedAgent(name="Helena Ruuska", role="editor", confidence=0.8)]

    def test_swedish_av_marker_yields_author_role(self) -> None:
        agents = parse_245c("av August Strindberg")
        assert agents == [ParsedAgent(name="August Strindberg", role="author", confidence=0.8)]

    def test_english_by_marker_yields_author_role(self) -> None:
        agents = parse_245c("by Margaret Atwood")
        assert agents == [ParsedAgent(name="Margaret Atwood", role="author", confidence=0.8)]

    def test_german_herausgegeben_von_marker_strips_full_phrase(self) -> None:
        """Regression test for the 2026-06-02 P-41 B.8 5k-bench
        finding: pre-fix, B1 regex didn't recognise German role
        markers, so ``"herausgegeben von L. Richter"`` (corpus
        bib 1000072) was synthesised as a 4-token name (the full
        string) rather than stripped to ``"L. Richter"``."""
        agents = parse_245c("herausgegeben von L. Richter")
        assert agents == [ParsedAgent(name="L. Richter", role="editor", confidence=0.8)]

    def test_german_uebersetzt_von_marker_strips_full_phrase(self) -> None:
        agents = parse_245c("übersetzt von Hans Müller")
        assert agents == [ParsedAgent(name="Hans Müller", role="translator", confidence=0.8)]

    def test_nested_role_markers_are_fully_stripped(self) -> None:
        """Regression test for the 2026-06-02 P-41 B.8 5 k-bench
        finding: ``"ED. BY BYRON MIKELLIDES"`` (corpus bib 1003207)
        was salvaged with ``synthesised_value = "BY BYRON MIKELLIDES"``
        because the parser only ran a single role-marker pass —
        ``"ed."`` was stripped, ``"by "`` survived as part of the
        name. Recursive stripping now handles nested markers; the
        first-matched role tag (``"editor"`` from ``"ed."``) wins
        over the secondary ``"by"`` (author)."""
        agents = parse_245c("ED. BY BYRON MIKELLIDES")
        assert agents == [ParsedAgent(name="BYRON MIKELLIDES", role="editor", confidence=0.8)]

    def test_arranged_by_marker_strips_correctly(self) -> None:
        """Regression test for 2026-06-02 bench bib 1000369:
        ``"Arranged by Steve Tayton"`` had the marker leak into the
        synthesised name pre-fix. The ``"arranged by"`` role maps
        to ``"unknown"`` because the salvage role enum lacks an
        ``"arranger"`` tag and a generic ``tekijä`` relator term is
        more honest than mis-labelling as compiler / author.
        Confidence is 0.6 because ``"unknown"`` is the role tag —
        the recognised marker tells us this is a contribution but
        not which kind, and the confidence formula treats unknown
        roles as low-confidence regardless of marker presence."""
        agents = parse_245c("Arranged by Steve Tayton")
        assert agents == [ParsedAgent(name="Steve Tayton", role="unknown", confidence=0.6)]

    def test_recordings_by_marker_strips_correctly(self) -> None:
        agents = parse_245c("Recordings by Roberto Leydi")
        assert agents == [ParsedAgent(name="Roberto Leydi", role="unknown", confidence=0.6)]

    def test_ed_dot_by_compound_marker_strips_correctly(self) -> None:
        """Both belt-and-braces paths cover this: ``"ed. by"`` is in
        ``_ROLE_MARKERS`` as a single phrase (matched first because
        of longest-first ordering), AND recursive stripping would
        handle the case even without the explicit entry."""
        agents = parse_245c("ed. by John Smith")
        assert agents == [ParsedAgent(name="John Smith", role="editor", confidence=0.8)]

    def test_leading_isbd_slash_does_not_block_role_marker(self) -> None:
        """Defence-in-depth: MARC 245$c sometimes carries leading
        ``" / "`` ISBD punctuation when re-imported from a display-
        format string. Pre-fix, ``_split_role_prefix`` did a strict
        ``startswith`` check and missed the role marker. Now leading
        punctuation is stripped before the marker match."""
        agents = parse_245c("/ by Margaret Atwood")
        assert agents == [ParsedAgent(name="Margaret Atwood", role="author", confidence=0.8)]

    def test_leading_paren_does_not_block_role_marker(self) -> None:
        agents = parse_245c("(by Margaret Atwood)")
        assert agents == [ParsedAgent(name="Margaret Atwood", role="author", confidence=0.8)]

    def test_leading_whitespace_does_not_block_role_marker(self) -> None:
        agents = parse_245c("   by Margaret Atwood")
        assert agents == [ParsedAgent(name="Margaret Atwood", role="author", confidence=0.8)]


class TestUnmarkedShapes:
    """245$c often carries just a bare name (Finnish convention)."""

    def test_bare_name_yields_unknown_role(self) -> None:
        agents = parse_245c("Tove Jansson")
        assert agents == [ParsedAgent(name="Tove Jansson", role="unknown", confidence=0.6)]

    def test_surname_first_with_comma_yields_one_agent(self) -> None:
        agents = parse_245c("Atwood, Margaret")
        # Note: the regex splits on commas, so surname-first parses to
        # ["Atwood", "Margaret"] each of which fails the 2+-token shape.
        # B1 conservatively returns []; the LLM cascade or B3 picks it up.
        assert agents == []


class TestMultiAuthorSplits:
    """Multi-author 245$c values split on ``ja`` / ``and`` / ``,``."""

    def test_finnish_ja_separator_yields_two_agents(self) -> None:
        agents = parse_245c("Liisa Louhela ja Pekka Halonen")
        assert agents == [
            ParsedAgent(name="Liisa Louhela", role="unknown", confidence=0.5),
            ParsedAgent(name="Pekka Halonen", role="unknown", confidence=0.5),
        ]

    def test_english_and_separator_with_role_yields_two_authors(self) -> None:
        agents = parse_245c("by Margaret Atwood and Alice Munro")
        assert agents == [
            ParsedAgent(name="Margaret Atwood", role="author", confidence=0.7),
            ParsedAgent(name="Alice Munro", role="author", confidence=0.7),
        ]

    def test_german_und_separator_splits_multi_author(self) -> None:
        """Regression test for the 2026-06-02 P-41 B.8 5 k re-run
        bench: bib 1000072 had 245$c
        ``"herausgegeben von L. Richter, S. Marschner, F. Docci und U. Jürgens"``
        — four editors. The pre-fix parser stripped the German role
        marker correctly, split on commas, and produced three
        candidates — but the third candidate
        ``"F. Docci und U. Jürgens"`` was accepted as a single
        5-token name because ``"und"`` wasn't a separator. The fix
        adds German ``und`` (and French ``et``) to the separator
        regex."""
        agents = parse_245c("F. Docci und U. Jürgens")
        assert agents == [
            ParsedAgent(name="F. Docci", role="unknown", confidence=0.5),
            ParsedAgent(name="U. Jürgens", role="unknown", confidence=0.5),
        ]

    def test_french_et_separator_splits_multi_author(self) -> None:
        agents = parse_245c("Marie Curie et Pierre Curie")
        assert agents == [
            ParsedAgent(name="Marie Curie", role="unknown", confidence=0.5),
            ParsedAgent(name="Pierre Curie", role="unknown", confidence=0.5),
        ]


class TestCorporateRejection:
    """B1 is the personal-name tier; corporate authors fall through to
    B2 or B3."""

    def test_finnish_kaupunki_marker_rejects_the_parse(self) -> None:
        assert parse_245c("Helsingin kaupunki") == []

    def test_english_university_marker_rejects_the_parse(self) -> None:
        assert parse_245c("Harvard University") == []

    def test_oy_corporate_marker_rejects_the_parse(self) -> None:
        assert parse_245c("Nokia Oy") == []


class TestEdgeCases:
    """Conservative rejection paths — B1 returns [] rather than guessing."""

    def test_empty_string_yields_no_agents(self) -> None:
        assert parse_245c("") == []
        assert parse_245c("   ") == []

    def test_field_with_only_role_marker_yields_no_agents(self) -> None:
        assert parse_245c("kirjoittanut") == []

    def test_name_with_digits_is_rejected(self) -> None:
        # E.g. "Pseudo Author 123" — corpus shows this is usually a
        # cataloguing artefact (call number or identifier in 245$c).
        assert parse_245c("Pseudo Author 123") == []

    def test_single_word_name_is_rejected(self) -> None:
        # A bare "Madonna" wouldn't pass — B1 wants at least
        # First + Last to keep precision high. Edge cases like
        # single-word stage names land in B3.
        assert parse_245c("Madonna") == []

    def test_mixed_personal_and_corporate_falls_back_to_empty(self) -> None:
        # If any candidate fails the shape check, the whole parse fails.
        agents = parse_245c("Liisa Louhela, Helsingin kaupunki")
        assert agents == []

    def test_initials_preserve_through_parse(self) -> None:
        agents = parse_245c("J. R. R. Tolkien")
        assert agents == [ParsedAgent(name="J. R. R. Tolkien", role="unknown", confidence=0.6)]


class TestConfidenceBands:
    """Confidence per the synthesis-policy table — role-marked > bare;
    single > multi-author."""

    def test_role_marked_single_is_0_8(self) -> None:
        agents = parse_245c("by Margaret Atwood")
        assert agents[0].confidence == pytest.approx(0.8)

    def test_unmarked_single_is_0_6(self) -> None:
        agents = parse_245c("Margaret Atwood")
        assert agents[0].confidence == pytest.approx(0.6)

    def test_role_marked_multi_is_0_7(self) -> None:
        agents = parse_245c("by Margaret Atwood and Alice Munro")
        assert all(a.confidence == pytest.approx(0.7) for a in agents)

    def test_unmarked_multi_is_0_5(self) -> None:
        agents = parse_245c("Margaret Atwood and Alice Munro")
        assert all(a.confidence == pytest.approx(0.5) for a in agents)
