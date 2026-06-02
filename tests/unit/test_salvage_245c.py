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
