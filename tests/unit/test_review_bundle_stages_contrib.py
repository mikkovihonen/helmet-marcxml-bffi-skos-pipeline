"""Unit tests for cataloguer-review pipeline of gold/contrib.jsonl.

Covers:

- stratified-sampling reproducibility (same seed → same rows)
- import round-trip (KEEP rows land; DISCARD drop; SKIP-FOR-NOW
  doesn't error)
- schema validation (unknown category, missing reviewed_by, bad
  relator code, missing vetted_contributions, malformed decision)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bffi_pipeline.eval.review_bundle.stages.contrib import (
    CANONICAL_CATEGORIES,
    DECISION_DISCARD,
    DECISION_KEEP,
    DECISION_SKIP,
    GoldReviewImportError,
    import_results,
    sample_stratified,
)

# --- Sample helpers -------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


def _candidate_row(
    *,
    id: str,
    category: str,
    helmet_bib_id: str = "b123",
) -> dict[str, Any]:
    """Build a minimal candidate-row dict mirroring the
    ``grow-candidates-contrib.jsonl`` shape."""
    return {
        "id": id,
        "category": category,
        "helmet_bib_id": helmet_bib_id,
        "c_subfield": "by X Y",
        "existing_agents": ["Existing, Agent"],
        "expected_contributions": [{"name": "X Y", "relator_code": "aut"}],
        "holdout": False,
        "added": "2026-06-03",
        "added_by": "grow-gold-contrib",
        "notes": "",
    }


_DEFAULT_VETTED: list[dict[str, Any]] = [
    {"name": "X Y", "relator_code": "aut", "transliteration_of": None}
]


def _keep_result_row(
    *,
    id: str = "cg-pending-0001",
    helmet_bib_id: str = "b001",
    category: str = "role-classification",
    vetted: list[dict[str, Any]] | None = None,
    holdout: bool = False,
    reviewed_by: str = "Maija Mäkinen",
) -> dict[str, Any]:
    """Build a KEEP-decision result row (the shape the HTML tool
    exports). ``vetted=None`` uses the default single-contribution
    list; ``vetted=[]`` is preserved verbatim so tests can exercise
    the empty-list validation path."""
    return {
        "id": id,
        "helmet_bib_id": helmet_bib_id,
        "c_subfield": "by X Y",
        "existing_agents": ["Existing, Agent"],
        "decision": DECISION_KEEP,
        "category": category,
        "holdout": holdout,
        "vetted_contributions": _DEFAULT_VETTED if vetted is None else vetted,
        "notes": "",
        "reviewed_by": reviewed_by,
        "reviewed_at": "2026-06-04T10:23:00+03:00",
    }


# --- sample_stratified ---------------------------------------------------


class TestSampleStratified:
    def test_same_seed_produces_same_rows(self, tmp_path: Path) -> None:
        """Pinned-seed reproducibility — two operators on the same
        day generate the same batch."""
        rows = [_candidate_row(id=f"r{i:04d}", category="transliteration") for i in range(50)] + [
            _candidate_row(id=f"r{i:04d}", category="role-classification") for i in range(50, 100)
        ]
        src = tmp_path / "pool.jsonl"
        _write_jsonl(src, rows)
        out_a = tmp_path / "a.jsonl"
        out_b = tmp_path / "b.jsonl"
        sample_stratified(input_path=src, output_path=out_a, per_category=5, seed="2026-06-03")
        sample_stratified(input_path=src, output_path=out_b, per_category=5, seed="2026-06-03")
        assert out_a.read_text(encoding="utf-8") == out_b.read_text(encoding="utf-8")

    def test_different_seed_produces_different_rows(self, tmp_path: Path) -> None:
        rows = [_candidate_row(id=f"r{i:04d}", category="transliteration") for i in range(50)]
        src = tmp_path / "pool.jsonl"
        _write_jsonl(src, rows)
        out_a = tmp_path / "a.jsonl"
        out_b = tmp_path / "b.jsonl"
        sample_stratified(input_path=src, output_path=out_a, per_category=5, seed="seed-a")
        sample_stratified(input_path=src, output_path=out_b, per_category=5, seed="seed-b")
        assert out_a.read_text(encoding="utf-8") != out_b.read_text(encoding="utf-8")

    def test_per_category_count_matches_target(self, tmp_path: Path) -> None:
        rows = []
        for cat in CANONICAL_CATEGORIES:
            rows.extend(_candidate_row(id=f"{cat}-{i}", category=cat) for i in range(30))
        src = tmp_path / "pool.jsonl"
        _write_jsonl(src, rows)
        out = tmp_path / "sample.jsonl"
        hist = sample_stratified(input_path=src, output_path=out, per_category=10, seed="x")
        assert all(v == 10 for v in hist.values())
        sampled = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines() if ln]
        assert len(sampled) == 10 * len(CANONICAL_CATEGORIES)

    def test_small_pool_doesnt_oversample(self, tmp_path: Path) -> None:
        """A category with fewer than ``per_category`` rows contributes
        all its rows — sample_stratified doesn't fabricate."""
        rows = [
            _candidate_row(id="only-1", category="pure-new-agent"),
            _candidate_row(id="only-2", category="pure-new-agent"),
        ]
        src = tmp_path / "pool.jsonl"
        _write_jsonl(src, rows)
        out = tmp_path / "sample.jsonl"
        hist = sample_stratified(input_path=src, output_path=out, per_category=25, seed="x")
        assert hist == {"pure-new-agent": 2}


# --- import_results -----------------------------------------------------


class TestImportRoundTrip:
    def test_keep_row_appends_to_gold(self, tmp_path: Path) -> None:
        """The KEEP path: a result row lands in gold/contrib.jsonl in
        the canonical bootstrap schema with a sequential cg-NNNN id."""
        gold = tmp_path / "contrib.jsonl"
        _write_jsonl(
            gold,
            [
                {
                    "id": "cg-0001",
                    "category": "pure-new-agent",
                    "helmet_bib_id": "b1",
                    "c_subfield": "x",
                    "existing_agents": [],
                    "expected_contributions": [{"name": "X", "relator_code": "aut"}],
                    "holdout": False,
                    "added": "2026-01-01",
                    "added_by": "bootstrap",
                    "notes": "",
                }
            ],
        )
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_keep_result_row(id="cg-pending-0042")])

        summary = import_results(input_path=results, output_path=gold)
        assert summary.kept == 1
        assert summary.appended_to_gold == 1
        assert summary.gold_total_before == 1
        assert summary.gold_total_after == 2

        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        appended = rows[-1]
        assert appended["id"] == "cg-0002"
        assert appended["category"] == "role-classification"
        assert appended["helmet_bib_id"] == "b001"
        assert appended["expected_contributions"] == [
            {"name": "X Y", "relator_code": "aut", "transliteration_of": None}
        ]
        # Cataloguer-only fields are dropped from the gold row.
        assert "decision" not in appended
        assert "reviewed_by" not in appended
        assert "vetted_contributions" not in appended
        # Reviewer credit is preserved in added_by.
        assert "Maija Mäkinen" in appended["added_by"]

    def test_discard_row_is_dropped_silently(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                {
                    "id": "cg-pending-0001",
                    "helmet_bib_id": "b1",
                    "c_subfield": "x",
                    "existing_agents": [],
                    "decision": DECISION_DISCARD,
                    "category": "pure-new-agent",
                    "holdout": False,
                    "vetted_contributions": [],
                    "notes": "obvious LLM hallucination",
                    "reviewed_by": "Maija",
                    "reviewed_at": "2026-06-04T10:00:00Z",
                }
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.kept == 0
        assert summary.discarded == 1
        assert summary.appended_to_gold == 0
        assert gold.read_text(encoding="utf-8") == ""

    def test_skip_row_is_reported_but_not_appended(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                {
                    "id": "cg-pending-9999",
                    "helmet_bib_id": "b1",
                    "c_subfield": "x",
                    "existing_agents": [],
                    "decision": DECISION_SKIP,
                    "category": None,
                    "holdout": False,
                    "vetted_contributions": [],
                    "notes": "need to ask colleague",
                    "reviewed_by": "Maija",
                    "reviewed_at": "2026-06-04T10:00:00Z",
                }
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.skipped == 1
        assert summary.kept == 0
        assert summary.skipped_ids == ["cg-pending-9999"]

    def test_id_minting_handles_mixed_existing_ids(self, tmp_path: Path) -> None:
        """If gold has cg-0001 and cg-0017, the next minted id is
        cg-0018 (max+1, not count+1)."""
        gold = tmp_path / "contrib.jsonl"
        _write_jsonl(
            gold,
            [
                {"id": "cg-0001", "helmet_bib_id": "b1"},
                {"id": "cg-0017", "helmet_bib_id": "b17"},
            ],
        )
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_keep_result_row()])
        import_results(input_path=results, output_path=gold)
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert rows[-1]["id"] == "cg-0018"


# --- Validation ---------------------------------------------------------


class TestValidation:
    """KEEP-row validation surfaces errors with row-id + field context
    so the operator can ask the cataloguer to fix one specific row."""

    def test_unknown_category_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results, [_keep_result_row(id="cg-pending-bad", category="not-a-real-category")]
        )
        with pytest.raises(GoldReviewImportError, match=r"cg-pending-bad.*category"):
            import_results(input_path=results, output_path=gold)
        # Gold file unchanged because validation runs before append.
        assert gold.read_text(encoding="utf-8") == ""

    def test_missing_reviewed_by_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_keep_result_row(reviewed_by="")])
        with pytest.raises(GoldReviewImportError, match="reviewed_by"):
            import_results(input_path=results, output_path=gold)

    def test_invalid_relator_code_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _keep_result_row(
                    vetted=[{"name": "X Y", "relator_code": "not-a-real-code"}],
                )
            ],
        )
        with pytest.raises(GoldReviewImportError, match=r"relator_code.*not-a-real-code"):
            import_results(input_path=results, output_path=gold)

    def test_pipe_separated_relator_codes_accepted(self, tmp_path: Path) -> None:
        """gold/contrib.jsonl's bootstrap cg-0002 uses
        ``"aft|aui|wpr|ctb"`` as a valid relator code value meaning
        'any of these is defensible'. The validator must accept it as
        long as each piped code is in the canonical set."""
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        # 'wpr' isn't in VALID_RELATOR_CODES; use codes that all are.
        _write_jsonl(
            results,
            [_keep_result_row(vetted=[{"name": "X Y", "relator_code": "aft|aui|ctb"}])],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.kept == 1

    def test_empty_vetted_contributions_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_keep_result_row(vetted=[])])
        with pytest.raises(GoldReviewImportError, match="vetted_contributions"):
            import_results(input_path=results, output_path=gold)

    def test_contribution_without_relator_or_translit_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_keep_result_row(vetted=[{"name": "X Y"}])],  # neither relator nor translit
        )
        with pytest.raises(GoldReviewImportError, match="neither relator_code nor"):
            import_results(input_path=results, output_path=gold)

    def test_transliteration_only_contribution_accepted(self, tmp_path: Path) -> None:
        """The within-record-typo / cyrillic-latin case: cataloguer
        keeps ``transliteration_of`` (no ``relator_code``), which the
        bootstrap row cg-0003 uses."""
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _keep_result_row(
                    category="within-record-typo",
                    vetted=[{"name": "Anssi Karttunen", "transliteration_of": "Karttunen, Assi"}],
                )
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.kept == 1

    def test_malformed_decision_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                {
                    "id": "cg-pending-x",
                    "helmet_bib_id": "b1",
                    "c_subfield": "x",
                    "existing_agents": [],
                    "decision": "MAYBE",
                    "category": "pure-new-agent",
                    "holdout": False,
                    "vetted_contributions": [{"name": "X", "relator_code": "aut"}],
                    "notes": "",
                    "reviewed_by": "Maija",
                    "reviewed_at": "2026-06-04T10:00:00Z",
                }
            ],
        )
        with pytest.raises(GoldReviewImportError, match="decision must be"):
            import_results(input_path=results, output_path=gold)

    def test_two_pass_atomicity(self, tmp_path: Path) -> None:
        """If row 2 fails validation, row 1 must NOT have been appended.
        Validation runs over all rows before any append."""
        gold = tmp_path / "contrib.jsonl"
        gold.touch()
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _keep_result_row(id="cg-pending-good"),
                _keep_result_row(id="cg-pending-bad", category="not-a-real-category"),
            ],
        )
        with pytest.raises(GoldReviewImportError):
            import_results(input_path=results, output_path=gold)
        # Empty gold file: the first (valid) row didn't sneak through.
        assert gold.read_text(encoding="utf-8") == ""


# --- ImportSummary rendering -------------------------------------------


class TestImportSummary:
    def test_summary_includes_p39_gate_status(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        _write_jsonl(
            gold,
            [{"id": f"cg-{i:04d}", "helmet_bib_id": f"b{i}"} for i in range(1, 29)],
        )
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_keep_result_row()])
        summary = import_results(input_path=results, output_path=gold)
        rendered = summary.render()
        assert "P-39 gate" in rendered
        # 28 + 1 = 29; 1 short of the 30 default threshold.
        assert "still 1" in rendered

    def test_summary_reports_gate_cleared(self, tmp_path: Path) -> None:
        gold = tmp_path / "contrib.jsonl"
        _write_jsonl(
            gold,
            [{"id": f"cg-{i:04d}", "helmet_bib_id": f"b{i}"} for i in range(1, 30)],
        )
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_keep_result_row()])
        summary = import_results(input_path=results, output_path=gold)
        rendered = summary.render()
        assert "P-39 gate cleared" in rendered
