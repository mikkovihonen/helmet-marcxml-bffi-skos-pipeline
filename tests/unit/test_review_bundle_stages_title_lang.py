"""Unit tests for the M3 title-language stage of the review bundle.

Sampler stratifies on ``segment_count_band x len(candidates)``.
Importer validates 3 decision values (KEEP / DISCARD / SKIP-FOR-NOW),
mints ``tl-NNNN`` gold ids, and refuses empty
``expected_segments`` on KEEP (cataloguer should use DISCARD
instead).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bffi_pipeline.eval.review_bundle.stages.title_lang import (
    DECISION_DISCARD,
    DECISION_KEEP,
    DECISION_SKIP,
    TitleLangImportError,
    bib_ids_for_marc,
    import_results,
    sample_stratified,
)


def _audit_row(
    *,
    row_id: str,
    bib: str = "1000001",
    title: str = "Foo = bar",
    candidates: list[str] | None = None,
    llm_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "helmet_bib_id": bib,
        "work_uri": f"http://urn.fi/URN:NBN:fi:bib:work:{bib}",
        "title": title,
        "candidates": candidates if candidates is not None else ["en", "fi"],
        "llm_segments": llm_segments
        if llm_segments is not None
        else [{"text": "Foo", "lang": "fi"}, {"text": "bar", "lang": "en"}],
        "rationale": "Test rationale exceeding twenty characters easily.",
        "added": "2026-06-04",
        "added_by": "m3-title-lang",
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
    def test_stratifies_on_segment_count_x_candidate_count(self, tmp_path: Path) -> None:
        rows: list[dict[str, Any]] = []
        # 3 x (2seg / 2cand)
        for i in range(3):
            rows.append(_audit_row(row_id=f"2-2-{i}", bib=f"a{i:03d}"))
        # 2 x (3seg / 3cand)
        for i in range(2):
            rows.append(
                _audit_row(
                    row_id=f"3-3-{i}",
                    bib=f"b{i:03d}",
                    candidates=["en", "fi", "sv"],
                    llm_segments=[
                        {"text": "A", "lang": "en"},
                        {"text": "B", "lang": "fi"},
                        {"text": "C", "lang": "sv"},
                    ],
                )
            )
        # 1 x (multi / 2cand)
        rows.append(
            _audit_row(
                row_id="m-2-0",
                bib="c000",
                llm_segments=[
                    {"text": "A", "lang": "en"},
                    {"text": "B", "lang": "fi"},
                    {"text": "C", "lang": "en"},
                    {"text": "D", "lang": "fi"},
                ],
            )
        )
        audit = tmp_path / "title-lang-candidates.jsonl"
        _write_jsonl(audit, rows)
        out = tmp_path / "candidates.jsonl"
        hist = sample_stratified(input_path=audit, output_path=out, per_category=10, seed="x")
        assert hist == {
            "2seg/2cand": 3,
            "3seg/3cand": 2,
            "multi/2cand": 1,
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

    def test_mints_sequential_pending_ids(self, tmp_path: Path) -> None:
        rows = [_audit_row(row_id=f"r{i:03d}", bib=f"b{i:03d}") for i in range(3)]
        audit = tmp_path / "audit.jsonl"
        _write_jsonl(audit, rows)
        out = tmp_path / "candidates.jsonl"
        sample_stratified(input_path=audit, output_path=out, per_category=10, seed="x")
        ids = [
            json.loads(ln)["id"]
            for ln in out.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert ids == ["cg-pending-0001", "cg-pending-0002", "cg-pending-0003"]


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

    def test_skips_null_bib(self, tmp_path: Path) -> None:
        rows = [_audit_row(row_id="r1", bib="b1")]
        rows[0]["helmet_bib_id"] = None
        rows.append(_audit_row(row_id="r2", bib="b2"))
        audit = tmp_path / "audit.jsonl"
        _write_jsonl(audit, rows)
        out = tmp_path / "sample.jsonl"
        sample_stratified(input_path=audit, output_path=out, per_category=10, seed="x")
        assert bib_ids_for_marc(out.read_bytes()) == ["b2"]


# --- Importer ------------------------------------------------------------


def _result_row(
    *,
    row_id: str = "cg-pending-0001",
    decision: str = DECISION_KEEP,
    bib: str = "1000123",
    title: str = "Foo = bar",
    candidates: list[str] | None = None,
    llm_segments: list[dict[str, Any]] | None = None,
    expected_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "decision": decision,
        "helmet_bib_id": bib,
        "work_uri": f"http://urn.fi/URN:NBN:fi:bib:work:{bib}",
        "title": title,
        "candidates": candidates if candidates is not None else ["en", "fi"],
        "llm_segments": llm_segments
        if llm_segments is not None
        else [{"text": "Foo", "lang": "fi"}, {"text": "bar", "lang": "en"}],
        "expected_segments": expected_segments
        if expected_segments is not None
        else [{"text": "Foo", "lang": "fi"}, {"text": "bar", "lang": "en"}],
        "rationale": "rationale that's substantive enough.",
        "holdout": False,
        "notes": "",
        "reviewed_by": "Maija M",
        "reviewed_at": "2026-06-04T10:00:00+00:00",
    }


class TestImportResults:
    def test_keep_appends_to_gold(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(decision=DECISION_KEEP)])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert rows[0]["id"] == "tl-0001"
        assert rows[0]["helmet_bib_id"] == "1000123"
        assert rows[0]["title"] == "Foo = bar"
        assert rows[0]["expected_segments"] == [
            {"text": "Foo", "lang": "fi"},
            {"text": "bar", "lang": "en"},
        ]
        assert "Maija M" in rows[0]["added_by"]
        # Cataloguer-only fields dropped from the gold row.
        assert "decision" not in rows[0]
        assert "reviewed_by" not in rows[0]
        assert "llm_segments" not in rows[0]
        assert "rationale" not in rows[0]

    def test_keep_with_empty_expected_segments_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(expected_segments=[])])
        with pytest.raises(TitleLangImportError, match=r"expected_segments"):
            import_results(input_path=results, output_path=gold)

    def test_keep_with_segment_missing_text_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(expected_segments=[{"text": "", "lang": "fi"}])],
        )
        with pytest.raises(TitleLangImportError, match=r"text"):
            import_results(input_path=results, output_path=gold)

    def test_keep_with_lang_outside_candidates_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(
                    candidates=["en", "fi"],
                    expected_segments=[{"text": "Foo", "lang": "ru"}],
                )
            ],
        )
        with pytest.raises(TitleLangImportError, match=r"lang"):
            import_results(input_path=results, output_path=gold)

    def test_keep_with_null_lang_accepted(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(
                    expected_segments=[
                        {"text": "Foo", "lang": "fi"},
                        {"text": "bar", "lang": None},
                    ]
                )
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert rows[0]["expected_segments"][1]["lang"] is None

    def test_discard_drops_silently(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(decision=DECISION_DISCARD)])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.discarded == 1
        assert summary.landed == 0
        assert gold.read_text(encoding="utf-8") == ""

    def test_skip_surfaces_in_summary(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(row_id="cg-pending-0042", decision=DECISION_SKIP)],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.skipped == 1
        assert summary.skipped_ids == ["cg-pending-0042"]

    def test_flipped_ids_tracked_when_cataloguer_overrides_segments(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(
                    row_id="cg-pending-0001",
                    title="Foo = bar",
                    llm_segments=[
                        {"text": "Foo", "lang": "fi"},
                        {"text": "bar", "lang": "en"},
                    ],
                    expected_segments=[
                        # Cataloguer flipped fi/en
                        {"text": "Foo", "lang": "en"},
                        {"text": "bar", "lang": "fi"},
                    ],
                )
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        assert len(summary.flipped_ids) == 1
        assert summary.flipped_ids[0][0] == "tl-0001"
        assert summary.flipped_ids[0][1] == "Foo = bar"
        assert "overrode per-segment language" in summary.render()

    def test_unchanged_segments_not_flipped(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row()])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        assert summary.flipped_ids == []

    def test_mints_sequential_ids_continuing_existing_gold(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        _write_jsonl(
            gold,
            [
                {
                    "id": "tl-0007",
                    "helmet_bib_id": "1000001",
                    "title": "Existing",
                    "candidates": ["fi"],
                    "expected_segments": [{"text": "Existing", "lang": "fi"}],
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
        assert rows[-1]["id"] == "tl-0008"

    def test_invalid_decision_raises(self, tmp_path: Path) -> None:
        gold = tmp_path / "title-lang.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row(decision="MAYBE")])
        with pytest.raises(TitleLangImportError, match=r"decision"):
            import_results(input_path=results, output_path=gold)
