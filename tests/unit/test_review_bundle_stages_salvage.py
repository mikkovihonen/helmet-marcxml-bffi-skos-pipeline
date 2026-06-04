"""Unit tests for the M2 salvage stage of the review bundle.

Sampler stratifies on ``cache_hit x agent_count_band``. Importer
validates 3 decision values (KEEP / DISCARD / SKIP-FOR-NOW),
mints ``sv-NNNN`` gold ids, and refuses empty ``expected_agents``
on KEEP (cataloguer should use DISCARD instead).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bffi_pipeline.eval.review_bundle.stages.salvage import (
    DECISION_DISCARD,
    DECISION_KEEP,
    DECISION_SKIP,
    SalvageImportError,
    bib_ids_for_marc,
    import_results,
    sample_stratified,
)


def _audit_row(
    *,
    row_id: str,
    bib: str = "1000001",
    cache_hit: bool = False,
    agents: list[dict[str, Any]] | None = None,
    c_subfield: str = "by Test Author",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "helmet_bib_id": bib,
        "c_subfield": c_subfield,
        "salvaged_agents": agents
        if agents is not None
        else [{"name": "Test Author", "role": "author"}],
        "rationale": "Test rationale exceeding the twenty char minimum.",
        "cache_hit": cache_hit,
        "added": "2026-06-04",
        "added_by": "m2-salvage",
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
    def test_stratifies_on_cache_hit_x_agent_count(self, tmp_path: Path) -> None:
        rows = []
        # 3 fresh x single, 3 cache-hit x single, 2 fresh x multi, 1 fresh x empty
        for i in range(3):
            rows.append(_audit_row(row_id=f"f-s-{i}", cache_hit=False, bib=f"b{i:03d}"))
        for i in range(3):
            rows.append(_audit_row(row_id=f"c-s-{i}", cache_hit=True, bib=f"c{i:03d}"))
        for i in range(2):
            rows.append(
                _audit_row(
                    row_id=f"f-m-{i}",
                    cache_hit=False,
                    bib=f"m{i:03d}",
                    agents=[
                        {"name": "A", "role": "author"},
                        {"name": "B", "role": "author"},
                    ],
                )
            )
        rows.append(_audit_row(row_id="f-e-0", cache_hit=False, bib="e001", agents=[]))

        audit = tmp_path / "salvage-candidates.jsonl"
        _write_jsonl(audit, rows)
        out = tmp_path / "candidates.jsonl"
        hist = sample_stratified(input_path=audit, output_path=out, per_category=10, seed="x")
        assert hist == {
            "cache-hit/single": 3,
            "fresh/empty": 1,
            "fresh/multi": 2,
            "fresh/single": 3,
        }

    def test_seed_reproducibility(self, tmp_path: Path) -> None:
        rows = [_audit_row(row_id=f"r{i:03d}", bib=f"b{i:03d}") for i in range(15)]
        audit = tmp_path / "audit.jsonl"
        _write_jsonl(audit, rows)
        out_a = tmp_path / "a.jsonl"
        out_b = tmp_path / "b.jsonl"
        sample_stratified(input_path=audit, output_path=out_a, per_category=5, seed="seed-x")
        sample_stratified(input_path=audit, output_path=out_b, per_category=5, seed="seed-x")
        assert out_a.read_text(encoding="utf-8") == out_b.read_text(encoding="utf-8")


class TestBibIdsForMarc:
    def test_returns_one_per_row(self, tmp_path: Path) -> None:
        rows = [
            _audit_row(row_id="r1", bib="b1"),
            _audit_row(row_id="r2", bib="b2"),
        ]
        audit = tmp_path / "audit.jsonl"
        _write_jsonl(audit, rows)
        out = tmp_path / "sample.jsonl"
        sample_stratified(input_path=audit, output_path=out, per_category=10, seed="x")
        assert sorted(bib_ids_for_marc(out.read_bytes())) == ["b1", "b2"]


# --- Importer ------------------------------------------------------------


def _result_row(
    *,
    row_id: str = "cg-pending-0001",
    decision: str = DECISION_KEEP,
    bib: str = "1000123",
    expected_agents: list[dict[str, Any]] | None = None,
    cache_hit: bool = False,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "decision": decision,
        "helmet_bib_id": bib,
        "c_subfield": "by Test Author",
        "expected_agents": expected_agents
        if expected_agents is not None
        else [{"name": "Test Author", "relator_code": "aut", "role_text": "author"}],
        "salvaged_agents": [{"name": "Test Author", "role": "author"}],
        "rationale": "rationale text that's substantive.",
        "cache_hit": cache_hit,
        "holdout": False,
        "notes": "",
        "reviewed_by": "Maija M",
        "reviewed_at": "2026-06-04T10:00:00+00:00",
    }


class TestImportResults:
    def test_keep_appends_to_gold(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(decision=DECISION_KEEP)])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert rows[0]["id"] == "sv-0001"
        assert rows[0]["helmet_bib_id"] == "1000123"
        assert rows[0]["expected_agents"] == [
            {"name": "Test Author", "relator_code": "aut", "role_text": "author"}
        ]
        assert "Maija M" in rows[0]["added_by"]
        # Cataloguer-only fields dropped from the gold row.
        assert "decision" not in rows[0]
        assert "reviewed_by" not in rows[0]
        assert "salvaged_agents" not in rows[0]

    def test_keep_with_empty_expected_agents_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(expected_agents=[])])
        with pytest.raises(SalvageImportError, match=r"expected_agents"):
            import_results(input_path=results, output_path=gold)

    def test_keep_with_agent_missing_name_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(expected_agents=[{"name": "", "relator_code": "aut"}])],
        )
        with pytest.raises(SalvageImportError, match=r"name"):
            import_results(input_path=results, output_path=gold)

    def test_discard_drops_silently(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(decision=DECISION_DISCARD)])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.discarded == 1
        assert summary.landed == 0
        assert gold.read_text(encoding="utf-8") == ""

    def test_skip_surfaces_in_summary(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(row_id="cg-pending-0042", decision=DECISION_SKIP)],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.skipped == 1
        assert summary.skipped_ids == ["cg-pending-0042"]

    def test_cache_hit_kept_count_tracked(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(row_id="cg-pending-0001", cache_hit=False),
                _result_row(row_id="cg-pending-0002", cache_hit=True),
                _result_row(row_id="cg-pending-0003", cache_hit=True),
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 3
        assert summary.cache_hit_kept == 2
        assert "KEEP rows from cache hits" in summary.render()

    def test_mints_sequential_ids_continuing_existing_gold(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        _write_jsonl(
            gold,
            [
                {
                    "id": "sv-0007",
                    "helmet_bib_id": "1000001",
                    "expected_agents": [{"name": "Existing", "relator_code": "aut"}],
                    "holdout": False,
                    "added": "2026-01-01",
                    "added_by": "bootstrap",
                    "notes": "",
                }
            ],
        )
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row()])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert rows[-1]["id"] == "sv-0008"

    def test_invalid_decision_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(decision="MAYBE")])
        with pytest.raises(SalvageImportError, match=r"decision"):
            import_results(input_path=results, output_path=gold)

    def test_missing_relator_code_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(expected_agents=[{"name": "John Smith"}])],
        )
        with pytest.raises(SalvageImportError, match=r"relator_code"):
            import_results(input_path=results, output_path=gold)

    def test_unknown_relator_code_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(expected_agents=[{"name": "John Smith", "relator_code": "xyz"}])],
        )
        with pytest.raises(SalvageImportError, match=r"VALID_RELATOR_CODES"):
            import_results(input_path=results, output_path=gold)

    def test_pipe_separated_relator_code_accepted(self, tmp_path: Path) -> None:
        gold = tmp_path / "salvage.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(
                    expected_agents=[
                        {"name": "Foreword Author", "relator_code": "aft|aui|ctb"},
                    ]
                )
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert rows[0]["expected_agents"][0]["relator_code"] == "aft|aui|ctb"
