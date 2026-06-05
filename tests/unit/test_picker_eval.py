"""Unit tests for the M9 picker eval harness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bffi_pipeline.eval.picker_eval import (
    CONFIDENTLY_WRONG_CONFIDENCE_FLOOR,
    EvalCase,
    load_cases,
    render_table,
    score_one,
    score_set,
)
from bffi_pipeline.stages.m9.picker import PickerDecision
from bffi_pipeline.stages.m9.schemas import AuthorityCandidate, EntityRequest


@dataclass
class _ScriptedPicker:
    """Minimal stub: returns pre-baked picks per case id."""

    by_id: dict[str, PickerDecision]
    seen: list[tuple[EntityRequest, list[AuthorityCandidate]]]

    def __init__(self, by_id: dict[str, PickerDecision]) -> None:
        self.by_id = by_id
        self.seen = []

    def pick(
        self, *, request: EntityRequest, candidates: list[AuthorityCandidate]
    ) -> PickerDecision:
        self.seen.append((request, candidates))
        # Cases are looked up by a marker on the request literal — keep
        # the stub interface tight + readable.
        return self.by_id[request.literal]


def _make_case(
    case_id: str,
    *,
    literal: str,
    expected_decision: str = "chose",
    expected_uri: str | None = "http://example.org/right",
    candidates: list[AuthorityCandidate] | None = None,
) -> EvalCase:
    if candidates is None:
        candidates = [
            AuthorityCandidate(
                uri="http://example.org/right",
                pref_label="Right Match",
                source_vocabulary="finaf",
                lexical_similarity=0.95,
            ),
            AuthorityCandidate(
                uri="http://example.org/wrong",
                pref_label="Wrong Match",
                source_vocabulary="finaf",
                lexical_similarity=0.65,
            ),
        ]
    return EvalCase(
        id=case_id,
        description="test case",
        request=EntityRequest(
            work_uri="http://example.org/work/1",
            literal=literal,
            kind="person",
        ),
        candidates=candidates,
        expected_decision=expected_decision,
        expected_uri=expected_uri,
    )


# --- score_one ------------------------------------------------------


class TestScoreOne:
    def test_correct_pick_scores_match_both(self) -> None:
        case = _make_case("test-1", literal="alice")
        picker = _ScriptedPicker(
            {
                "alice": PickerDecision(
                    decision="chose",
                    chosen_uri="http://example.org/right",
                    confidence=0.91,
                    rationale="The candidate's label clearly aligns with the input.",
                )
            }
        )
        result = score_one(case, picker)
        assert result.decision_match is True
        assert result.uri_match is True
        assert result.confidently_wrong is False
        assert result.latency_seconds >= 0

    def test_wrong_pick_but_low_confidence_is_NOT_confidently_wrong(self) -> None:
        case = _make_case("test-2", literal="bob")
        picker = _ScriptedPicker(
            {
                "bob": PickerDecision(
                    decision="chose",
                    chosen_uri="http://example.org/wrong",
                    confidence=0.78,
                    rationale="Two candidates tied; picked the latter on a lexical edge.",
                )
            }
        )
        result = score_one(case, picker)
        assert result.decision_match is True  # decision was "chose"
        assert result.uri_match is False
        assert result.confidently_wrong is False  # < 0.85

    def test_wrong_pick_with_high_confidence_is_confidently_wrong(self) -> None:
        """Hakala-class failure: picker confidently picks the wrong URI."""
        case = _make_case("test-3", literal="hakala-shape")
        picker = _ScriptedPicker(
            {
                "hakala-shape": PickerDecision(
                    decision="chose",
                    chosen_uri="http://example.org/wrong",
                    confidence=0.92,
                    rationale=(
                        "Rationalised the wrong candidate into a confident pick — "
                        "the failure mode the eval is named after."
                    ),
                )
            }
        )
        result = score_one(case, picker)
        assert result.uri_match is False
        assert result.confidently_wrong is True

    def test_uncertain_expected_uncertain_actual_matches(self) -> None:
        case = _make_case(
            "test-4",
            literal="virtanen",
            expected_decision="uncertain",
            expected_uri=None,
        )
        picker = _ScriptedPicker(
            {
                "virtanen": PickerDecision(
                    decision="uncertain",
                    chosen_uri=None,
                    confidence=0.5,
                    rationale="Two indistinguishable candidates; returning uncertain.",
                )
            }
        )
        result = score_one(case, picker)
        assert result.decision_match is True
        assert result.uri_match is True  # both None
        assert result.confidently_wrong is False

    def test_threshold_boundary_exactly_at_floor_counts_as_confidently_wrong(self) -> None:
        """confidence == CONFIDENTLY_WRONG_CONFIDENCE_FLOOR is the cutoff (≥)."""
        case = _make_case("test-5", literal="boundary")
        picker = _ScriptedPicker(
            {
                "boundary": PickerDecision(
                    decision="chose",
                    chosen_uri="http://example.org/wrong",
                    confidence=CONFIDENTLY_WRONG_CONFIDENCE_FLOOR,
                    rationale="Edge of the confidently-wrong cliff.",
                )
            }
        )
        result = score_one(case, picker)
        assert result.confidently_wrong is True


# --- score_set / summary --------------------------------------------


class TestScoreSet:
    def test_aggregates_counts_correctly(self) -> None:
        cases = [
            _make_case("c1", literal="a"),
            _make_case("c2", literal="b"),
            _make_case("c3", literal="c"),
            _make_case(
                "c4",
                literal="d",
                expected_decision="uncertain",
                expected_uri=None,
            ),
        ]
        picker = _ScriptedPicker(
            {
                # c1: correct
                "a": PickerDecision(
                    decision="chose",
                    chosen_uri="http://example.org/right",
                    confidence=0.95,
                    rationale="Solid pick with all the right signals.",
                ),
                # c2: wrong + confidently
                "b": PickerDecision(
                    decision="chose",
                    chosen_uri="http://example.org/wrong",
                    confidence=0.92,
                    rationale="Rationalised wrong direction.",
                ),
                # c3: parse-fail fallthrough
                "c": PickerDecision(
                    decision="uncertain",
                    chosen_uri=None,
                    confidence=0.0,
                    rationale=(
                        "Picker fell through to uncertain after retries exhausted: parse-fail"
                    ),
                ),
                # c4: matches uncertain expectation
                "d": PickerDecision(
                    decision="uncertain",
                    chosen_uri=None,
                    confidence=0.5,
                    rationale="Genuinely uncertain — no disambiguator.",
                ),
            }
        )
        _, summary = score_set(cases, picker)
        assert summary.total == 4
        # Decision-match table:
        #   c1 chose / chose       ✓
        #   c2 chose / chose       ✓
        #   c3 chose / uncertain   ✗  (parse-fall-through)
        #   c4 uncertain / uncertain ✓
        # So decision_matches = 3 (c1 + c2 + c4).
        assert summary.decision_matches == 3
        assert summary.uri_matches == 2  # c1 + c4
        assert summary.confidently_wrong == 1  # c2
        assert summary.parse_failures == 1  # c3

    def test_render_summary_is_human_readable(self) -> None:
        cases = [_make_case("c1", literal="a")]
        picker = _ScriptedPicker(
            {
                "a": PickerDecision(
                    decision="chose",
                    chosen_uri="http://example.org/right",
                    confidence=0.95,
                    rationale="Solid pick across all the picker's signals.",
                )
            }
        )
        _, summary = score_set(cases, picker)
        out = summary.render()
        assert "N=1" in out
        assert "decision-match=1/1" in out


# --- load_cases -----------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class TestLoadCases:
    def test_parses_minimal_case(self, tmp_path: Path) -> None:
        cases_file = tmp_path / "cases.jsonl"
        _write_jsonl(
            cases_file,
            [
                {
                    "id": "test-1",
                    "description": "minimal case",
                    "request": {
                        "work_uri": "http://example.org/w/1",
                        "literal": "Tester, Ada",
                        "kind": "person",
                    },
                    "candidates": [
                        {
                            "uri": "http://example.org/c/1",
                            "pref_label": "Tester, Ada, 1900-",
                            "source_vocabulary": "finaf",
                            "lexical_similarity": 0.9,
                        }
                    ],
                    "expected_decision": "chose",
                    "expected_uri": "http://example.org/c/1",
                }
            ],
        )
        cases = load_cases(cases_file)
        assert len(cases) == 1
        assert cases[0].id == "test-1"
        assert cases[0].request.literal == "Tester, Ada"
        assert cases[0].candidates[0].lexical_similarity == pytest.approx(0.9)
        assert cases[0].candidates[0].context is None

    def test_skips_comment_lines_and_blanks(self, tmp_path: Path) -> None:
        cases_file = tmp_path / "cases.jsonl"
        cases_file.write_text(
            "# this is a comment\n"
            "\n"
            "# another comment\n"
            + json.dumps(
                {
                    "id": "c1",
                    "request": {
                        "work_uri": "w",
                        "literal": "L",
                        "kind": "person",
                    },
                    "candidates": [],
                    "expected_decision": "uncertain",
                    "expected_uri": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        cases = load_cases(cases_file)
        assert len(cases) == 1
        assert cases[0].id == "c1"

    def test_parses_full_case_with_contexts(self, tmp_path: Path) -> None:
        cases_file = tmp_path / "cases.jsonl"
        _write_jsonl(
            cases_file,
            [
                {
                    "id": "hakala-shape",
                    "request": {
                        "work_uri": "w",
                        "literal": "Hakala, Tommi",
                        "kind": "person",
                        "work_context": {
                            "title": "Oopperan helmet",
                            "language": "fi",
                            "sibling_subjects": ["oopperat", "baritoni"],
                        },
                    },
                    "candidates": [
                        {
                            "uri": "http://finaf/1",
                            "pref_label": "Hakala, Tommi, 1972-",
                            "source_vocabulary": "finaf",
                            "lexical_similarity": 0.79,
                            "context": {
                                "field_of_activity_labels": ["yleiskirurgia"],
                                "birth_date": "1972",
                            },
                        }
                    ],
                    "expected_decision": "uncertain",
                    "expected_uri": None,
                }
            ],
        )
        cases = load_cases(cases_file)
        c = cases[0]
        assert c.request.work_context is not None
        assert c.request.work_context.sibling_subjects == ("oopperat", "baritoni")
        assert c.candidates[0].context is not None
        assert c.candidates[0].context.field_of_activity_labels == ("yleiskirurgia",)


class TestRenderTable:
    def test_renders_one_row_per_case(self) -> None:
        case = _make_case("test-1", literal="alice")
        picker = _ScriptedPicker(
            {
                "alice": PickerDecision(
                    decision="chose",
                    chosen_uri="http://example.org/right",
                    confidence=0.91,
                    rationale="The candidate's label clearly aligns.",
                )
            }
        )
        results, _ = score_set([case], picker)
        out = render_table(results)
        assert "test-1" in out
        assert "0.91" in out
