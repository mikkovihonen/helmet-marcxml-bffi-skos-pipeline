"""Unit tests for the M9 picker stage of the review bundle.

Covers sampler + importer behaviour. Stratifies on
entity_kind x outcome_stage x confidence_band. Importer validates
6 decision values; AGREE / PICK-CANDIDATE / NO-MATCH land in
gold/picker.jsonl; STILL-UNCERTAIN / DISCARD / SKIP-FOR-NOW
don't.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bffi_pipeline.eval.review_bundle.stages.picker import (
    DECISION_AGREE,
    DECISION_DISCARD,
    DECISION_NO_MATCH,
    DECISION_PICK_CANDIDATE,
    DECISION_SKIP,
    DECISION_UNCERTAIN,
    PickerImportError,
    bib_ids_for_marc,
    import_results,
    sample_stratified,
)


def _audit_row(
    *,
    row_id: str,
    entity_kind: str = "person",
    outcome_stage: str = "llm_pick",
    llm_confidence: float = 0.85,
    bib: str = "b001",
    llm_chosen_uri: str | None = "http://example.org/uri-A",
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "originating_bib_id": bib,
        "entity_kind": entity_kind,
        "entity_label": "Test Entity",
        "originating_work_uri": "http://urn.fi/URN:NBN:fi:bib:work:test",
        "predicate_uri": None,
        "candidates": candidates
        if candidates is not None
        else [
            {
                "uri": "http://example.org/uri-A",
                "pref_label": "Test Entity A",
                "source_vocabulary": "finaf",
                "lexical_similarity": 0.9,
            },
            {
                "uri": "http://example.org/uri-B",
                "pref_label": "Test Entity B",
                "source_vocabulary": "finaf",
                "lexical_similarity": 0.7,
            },
        ],
        "llm_decision": "chose" if llm_chosen_uri is not None else "uncertain",
        "llm_chosen_uri": llm_chosen_uri,
        "llm_confidence": llm_confidence,
        "llm_rationale": "rationale exceeding twenty characters easily.",
        "outcome_stage": outcome_stage,
        "outcome_chosen_uri": llm_chosen_uri or "http://example.org/uri-A",
        "outcome_confidence": llm_confidence,
        "outcome_rationale": "outcome rationale text",
        "needs_review": outcome_stage != "llm_pick",
        "added": "2026-06-04",
        "added_by": "m9-picker",
        "notes": "",
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


# --- Sampler --------------------------------------------------------------


class TestSampleStratified:
    def test_stratifies_on_kind_outcome_band(self, tmp_path: Path) -> None:
        rows = []
        for i in range(5):
            rows.append(
                _audit_row(
                    row_id=f"p-{i:04d}",
                    entity_kind="person",
                    outcome_stage="llm_pick",
                    llm_confidence=0.9,
                )
            )
            rows.append(
                _audit_row(
                    row_id=f"s-{i:04d}",
                    entity_kind="subject",
                    outcome_stage="fallback",
                    llm_confidence=0.5,
                )
            )
        audit = tmp_path / "picker-decisions.jsonl"
        _write_jsonl(audit, rows)
        out = tmp_path / "candidates.jsonl"
        hist = sample_stratified(input_path=audit, output_path=out, per_category=3, seed="x")
        # Two strata expected: person/llm_pick/high (0.9) + subject/fallback/low (0.5).
        assert hist == {"person/llm_pick/high": 3, "subject/fallback/low": 3}

    def test_seed_reproducibility(self, tmp_path: Path) -> None:
        rows = [_audit_row(row_id=f"r-{i:04d}", llm_confidence=0.7) for i in range(20)]
        audit = tmp_path / "picker-decisions.jsonl"
        _write_jsonl(audit, rows)
        out_a = tmp_path / "a.jsonl"
        out_b = tmp_path / "b.jsonl"
        sample_stratified(input_path=audit, output_path=out_a, per_category=5, seed="seed-x")
        sample_stratified(input_path=audit, output_path=out_b, per_category=5, seed="seed-x")
        assert out_a.read_text(encoding="utf-8") == out_b.read_text(encoding="utf-8")

    def test_per_stage_id_placeholder(self, tmp_path: Path) -> None:
        rows = [_audit_row(row_id=f"audit-{i}", llm_confidence=0.9) for i in range(3)]
        audit = tmp_path / "picker-decisions.jsonl"
        _write_jsonl(audit, rows)
        out = tmp_path / "sample.jsonl"
        sample_stratified(input_path=audit, output_path=out, per_category=10, seed="x")
        sampled = [
            json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        assert [r["id"] for r in sampled] == [
            "cg-pending-0001",
            "cg-pending-0002",
            "cg-pending-0003",
        ]


# --- bib_ids_for_marc ----------------------------------------------------


class TestBibIdsForMarc:
    def test_returns_one_per_row(self, tmp_path: Path) -> None:
        rows = [_audit_row(row_id="r1", bib="b1"), _audit_row(row_id="r2", bib="b2")]
        audit = tmp_path / "audit.jsonl"
        _write_jsonl(audit, rows)
        out = tmp_path / "sample.jsonl"
        sample_stratified(input_path=audit, output_path=out, per_category=10, seed="x")
        assert sorted(bib_ids_for_marc(out.read_bytes())) == ["b1", "b2"]

    def test_skips_rows_without_bib(self, tmp_path: Path) -> None:
        rows = [_audit_row(row_id="r1", bib="b1"), _audit_row(row_id="r2", bib="")]
        # Second row has empty bib (corner case — bib stripped at audit-write).
        rows[1]["originating_bib_id"] = None
        audit = tmp_path / "audit.jsonl"
        _write_jsonl(audit, rows)
        out = tmp_path / "sample.jsonl"
        sample_stratified(input_path=audit, output_path=out, per_category=10, seed="x")
        assert bib_ids_for_marc(out.read_bytes()) == ["b1"]


# --- Importer ------------------------------------------------------------


def _result_row(
    *,
    row_id: str = "cg-pending-0001",
    decision: str = DECISION_AGREE,
    expected_uri: str | None = "http://example.org/uri-A",
    entity_kind: str = "person",
    candidates: list[dict[str, Any]] | None = None,
    llm_chosen_uri: str | None = "http://example.org/uri-A",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "decision": decision,
        "expected_uri": expected_uri,
        "entity_kind": entity_kind,
        "entity_label": "Test Entity",
        "originating_bib_id": "1001234",
        "originating_work_uri": "http://urn.fi/URN:NBN:fi:bib:work:test",
        "predicate_uri": None,
        "candidates": candidates
        if candidates is not None
        else [
            {
                "uri": "http://example.org/uri-A",
                "pref_label": "A",
                "source_vocabulary": "finaf",
                "lexical_similarity": 0.9,
            },
            {
                "uri": "http://example.org/uri-B",
                "pref_label": "B",
                "source_vocabulary": "finaf",
                "lexical_similarity": 0.7,
            },
        ],
        "llm_decision": "chose",
        "llm_chosen_uri": llm_chosen_uri,
        "llm_confidence": 0.85,
        "llm_rationale": "rationale exceeding twenty characters easily.",
        "outcome_stage": "llm_pick",
        "outcome_chosen_uri": llm_chosen_uri,
        "outcome_confidence": 0.85,
        "outcome_rationale": "outcome rationale",
        "needs_review": False,
        "holdout": False,
        "notes": "",
        "reviewed_by": "Maija M",
        "reviewed_at": "2026-06-04T10:00:00+00:00",
    }


class TestImportResults:
    def test_agree_lands_with_llm_uri(self, tmp_path: Path) -> None:
        gold = tmp_path / "picker.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(decision=DECISION_AGREE)])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        assert summary.flipped_ids == []
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert rows[0]["id"] == "gp-0001"
        assert rows[0]["expected_uri"] == "http://example.org/uri-A"
        assert "Maija M" in rows[0]["added_by"]

    def test_pick_candidate_lands_with_picked_uri(self, tmp_path: Path) -> None:
        gold = tmp_path / "picker.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(
                    decision=DECISION_PICK_CANDIDATE,
                    expected_uri="http://example.org/uri-B",  # candidate B
                )
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        assert summary.flipped_ids == [
            ("gp-0001", "http://example.org/uri-A", "http://example.org/uri-B")
        ]

    def test_no_match_lands_with_null_uri(self, tmp_path: Path) -> None:
        gold = tmp_path / "picker.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(decision=DECISION_NO_MATCH, expected_uri=None)],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        assert summary.no_match_ids == ["gp-0001"]
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert rows[0]["expected_uri"] is None
        # Render summary surfaces NO-MATCH section.
        assert "NO-MATCH rows" in summary.render()

    def test_uncertain_does_not_land(self, tmp_path: Path) -> None:
        gold = tmp_path / "picker.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(decision=DECISION_UNCERTAIN)])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 0
        assert summary.still_uncertain == 1
        assert gold.read_text(encoding="utf-8") == ""

    def test_discard_and_skip_dont_land(self, tmp_path: Path) -> None:
        gold = tmp_path / "picker.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(row_id="cg-pending-0001", decision=DECISION_DISCARD),
                _result_row(row_id="cg-pending-0002", decision=DECISION_SKIP),
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.discarded == 1
        assert summary.skipped == 1
        assert gold.read_text(encoding="utf-8") == ""

    def test_pick_candidate_uri_not_in_candidates_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "picker.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(
                    decision=DECISION_PICK_CANDIDATE,
                    expected_uri="http://example.org/not-a-candidate",
                )
            ],
        )
        with pytest.raises(PickerImportError, match=r"not in the candidate list"):
            import_results(input_path=results, output_path=gold)

    def test_no_match_with_non_null_uri_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "picker.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(
                    decision=DECISION_NO_MATCH,
                    expected_uri="http://example.org/uri-A",  # invalid for NO-MATCH
                )
            ],
        )
        with pytest.raises(PickerImportError, match=r"NO-MATCH"):
            import_results(input_path=results, output_path=gold)

    def test_invalid_entity_kind_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "picker.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(entity_kind="not-a-kind")])
        with pytest.raises(PickerImportError, match=r"entity_kind"):
            import_results(input_path=results, output_path=gold)

    def test_mints_sequential_ids_continuing_existing_gold(self, tmp_path: Path) -> None:
        gold = tmp_path / "picker.jsonl"
        _write_jsonl(
            gold,
            [
                {
                    "id": "gp-0007",
                    "entity_kind": "person",
                    "entity_label": "Existing",
                    "expected_uri": "http://example.org/x",
                    "originating_bib_id": "b1",
                    "holdout": False,
                    "added": "2026-01-01",
                    "added_by": "bootstrap",
                    "notes": "",
                    "candidates_seen": [],
                }
            ],
        )
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row()])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert rows[-1]["id"] == "gp-0008"
