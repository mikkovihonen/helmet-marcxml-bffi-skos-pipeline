"""Unit tests for the M6 judge stage of the review bundle.

Covers:
- Sample stratification on (decision x confidence_band)
- Reproducibility (same seed → same rows)
- URI → bib id resolution via helmet-map + mint_raw_work_uri
- GoldRecord hydration from per-bib BFFI Turtle files
- Import round-trip: AGREE / FLIP-TO-SAME / FLIP-TO-DIFFERENT / STILL-UNCERTAIN /
  DISCARD / SKIP-FOR-NOW
- Validation errors: bad category, missing record_a/record_b, bad expected
- bib_ids_for_marc returns both pair sides (deduped at caller)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest

from bffi_pipeline.eval.gold_set import GoldCategory
from bffi_pipeline.eval.review_bundle.stages.judge import (
    DECISION_AGREE,
    DECISION_DISCARD,
    DECISION_FLIP_SAME,
    DECISION_SKIP,
    DECISION_UNCERTAIN,
    GOLD_CATEGORIES,
    JudgeImportError,
    bib_ids_for_marc,
    import_results,
    sample_stratified,
)
from bffi_pipeline.uris import mint_raw_work_uri

# --- Helpers --------------------------------------------------------------


def _judge_row(
    *,
    work_a: str,
    work_b: str,
    decision: str = "different_work",
    confidence: float = 0.7,
    similarity: float = 0.78,
    matching_fields: list[str] | None = None,
    diverging_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "work_a": work_a,
        "work_b": work_b,
        "similarity": similarity,
        "decision": decision,
        "confidence": confidence,
        "rationale": "test rationale exceeds twenty chars for validator.",
        "matching_fields": matching_fields or [],
        "diverging_fields": diverging_fields or [],
        "used_cascade": False,
        "cascade": [],
    }


def _helmet_map_row(*, bib_id: str, raw_work_uri: str) -> dict[str, Any]:
    return {
        "helmet_bib_id": bib_id,
        "source_file": f"{bib_id}.xml",
        "raw_work_uri": raw_work_uri,
        "raw_instance_uri": raw_work_uri.replace("#Work", "#Instance"),
        "converted_at": "2026-06-03T00:00:00Z",
        "marc2bibframe2_version": "v3.1.0",
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


def _build_run_dir(tmp_path: Path, pairs: list[dict[str, Any]]) -> tuple[Path, Path]:
    """Build a synthetic run dir with helmet-map.jsonl + judge-decisions.jsonl
    + per-bib BFFI Turtle files for each pair side.

    Returns ``(run_dir, judge_decisions_path)``.
    """
    run_dir = tmp_path / "run-uuid"
    run_dir.mkdir()
    bffi_dir = run_dir / "bffi"
    bffi_dir.mkdir()

    bibs_seen: dict[str, str] = {}  # bib_id → minted work uri
    helmet_rows: list[dict[str, Any]] = []
    judge_rows: list[dict[str, Any]] = []

    for i, p in enumerate(pairs):
        bib_a = p.get("bib_a", f"b{i:04d}a")
        bib_b = p.get("bib_b", f"b{i:04d}b")
        for bib in (bib_a, bib_b):
            if bib in bibs_seen:
                continue
            raw_uri = f"http://urn.fi/URN:NBN:fi:bib:raw/{bib}#Work"
            minted = mint_raw_work_uri(raw_uri)
            bibs_seen[bib] = minted
            helmet_rows.append(_helmet_map_row(bib_id=bib, raw_work_uri=raw_uri))
            # Minimal BFFI Turtle: a Work with prefLabel + Expression with language.
            ttl = f"""@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
@prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

<http://urn.fi/URN:NBN:fi:bib:expression:expr{bib}> a bffi:Expression ;
    bffi:language <http://id.loc.gov/vocabulary/languages/fin> ;
    bffi:content <http://id.loc.gov/vocabulary/contentTypes/txt> .

<http://example.org/agent-{bib}> rdfs:label "Author of {bib}" .

<{minted}> a bffi:Work ;
    skos:prefLabel "Title of {bib}"@fi ;
    bffi:hasExpression <http://urn.fi/URN:NBN:fi:bib:expression:expr{bib}> ;
    bffi:contribution [ a bffi:PrimaryContribution ;
        bffi:agent <http://example.org/agent-{bib}> ] .
"""
            (bffi_dir / f"{bib}.ttl").write_text(ttl, encoding="utf-8")
        judge_rows.append(
            _judge_row(
                work_a=bibs_seen[bib_a],
                work_b=bibs_seen[bib_b],
                decision=p.get("decision", "different_work"),
                confidence=p.get("confidence", 0.7),
                similarity=p.get("similarity", 0.78),
                matching_fields=p.get("matching_fields"),
                diverging_fields=p.get("diverging_fields"),
            )
        )

    _write_jsonl(run_dir / "helmet-map.jsonl", helmet_rows)
    judge_path = run_dir / "judge-decisions.jsonl"
    _write_jsonl(judge_path, judge_rows)
    return run_dir, judge_path


# --- Sampler --------------------------------------------------------------


class TestSampleStratified:
    def test_hydrates_record_fields_from_bffi(self, tmp_path: Path) -> None:
        run_dir, judge_path = _build_run_dir(
            tmp_path,
            [{"bib_a": "b100", "bib_b": "b200", "decision": "same_work", "confidence": 0.9}],
        )
        out = run_dir / "candidates.jsonl"
        sample_stratified(input_path=judge_path, output_path=out, per_category=5, seed="x")
        rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines() if ln]
        assert len(rows) == 1
        row = rows[0]
        assert row["record_a"]["helmet_bib_id"] == "b100"
        assert row["record_a"]["title"] == "Title of b100"
        assert row["record_a"]["creator"] == "Author of b100"
        assert row["record_a"]["language"] == "fin"
        assert row["record_a"]["content_type"] == "txt"
        assert row["record_b"]["helmet_bib_id"] == "b200"
        assert row["llm_decision"] == "same_work"
        assert row["llm_confidence"] == 0.9
        assert row["embedding_sim"] == 0.78

    def test_stratifies_on_decision_x_confidence_band(self, tmp_path: Path) -> None:
        pairs = []
        # 5 of each (decision, band) — should sample at most per_category from each.
        for i in range(5):
            pairs.append(
                {
                    "bib_a": f"sw-low-{i}-a",
                    "bib_b": f"sw-low-{i}-b",
                    "decision": "same_work",
                    "confidence": 0.3,
                }
            )
            pairs.append(
                {
                    "bib_a": f"dw-mid-{i}-a",
                    "bib_b": f"dw-mid-{i}-b",
                    "decision": "different_work",
                    "confidence": 0.7,
                }
            )
            pairs.append(
                {
                    "bib_a": f"dw-high-{i}-a",
                    "bib_b": f"dw-high-{i}-b",
                    "decision": "different_work",
                    "confidence": 0.95,
                }
            )
        run_dir, judge_path = _build_run_dir(tmp_path, pairs)
        out = run_dir / "sample.jsonl"
        hist = sample_stratified(
            input_path=judge_path, output_path=out, per_category=3, seed="seed-a"
        )
        # Each stratum capped at per_category=3.
        for v in hist.values():
            assert v <= 3
        # All 3 strata present (5 rows each, capped to 3 → 9 total).
        assert hist == {"same_work/low": 3, "different_work/mid": 3, "different_work/high": 3}

    def test_seed_reproducibility(self, tmp_path: Path) -> None:
        pairs = [
            {
                "bib_a": f"b{i:03d}a",
                "bib_b": f"b{i:03d}b",
                "decision": "different_work",
                "confidence": 0.65,
            }
            for i in range(20)
        ]
        run_dir, judge_path = _build_run_dir(tmp_path, pairs)
        out_a = run_dir / "a.jsonl"
        out_b = run_dir / "b.jsonl"
        sample_stratified(input_path=judge_path, output_path=out_a, per_category=5, seed="seed-x")
        sample_stratified(input_path=judge_path, output_path=out_b, per_category=5, seed="seed-x")
        assert out_a.read_text(encoding="utf-8") == out_b.read_text(encoding="utf-8")

    def test_unresolved_work_uri_keeps_stub(self, tmp_path: Path) -> None:
        """If a judge row references a work URI that isn't in the
        helmet map (e.g. M2 dropped that record), the row still
        lands in the bundle with a stub record (just work_uri,
        no bib id). MARC sidecar will be skipped for that side."""
        run_dir, judge_path = _build_run_dir(
            tmp_path,
            [{"bib_a": "b001", "bib_b": "b002", "decision": "same_work", "confidence": 0.9}],
        )
        # Append a row pointing at unknown work URIs.
        rogue = _judge_row(
            work_a="http://urn.fi/URN:NBN:fi:bib:work:00000000000000000000000000000000",
            work_b="http://urn.fi/URN:NBN:fi:bib:work:11111111111111111111111111111111",
            decision="different_work",
            confidence=0.6,
        )
        with judge_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rogue) + "\n")
        out = run_dir / "sample.jsonl"
        sample_stratified(input_path=judge_path, output_path=out, per_category=5, seed="x")
        rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines() if ln]
        assert len(rows) == 2
        # The rogue row has stub records (no helmet_bib_id, just work_uri).
        rogue_row = next(r for r in rows if r["record_a"].get("work_uri"))
        assert "helmet_bib_id" not in rogue_row["record_a"]
        assert rogue_row["record_a"]["work_uri"].endswith("0000")


# --- bib_ids_for_marc ----------------------------------------------------


class TestBibIdsForMarc:
    def test_returns_both_sides(self, tmp_path: Path) -> None:
        run_dir, judge_path = _build_run_dir(
            tmp_path,
            [
                {"bib_a": "b001", "bib_b": "b002"},
                {"bib_a": "b003", "bib_b": "b001"},  # b001 shared
            ],
        )
        out = run_dir / "sample.jsonl"
        sample_stratified(input_path=judge_path, output_path=out, per_category=5, seed="x")
        bibs = bib_ids_for_marc(out.read_bytes())
        # Duplicates preserved (bundle builder dedups downstream).
        assert sorted(bibs) == ["b001", "b001", "b002", "b003"]


# --- Importer ------------------------------------------------------------


def _result_row(
    *,
    row_id: str = "cg-pending-0001",
    decision: str = DECISION_AGREE,
    expected: str = "different_work",
    category: str = "cross-genre-different-work",
    llm_decision: str = "different_work",
    bib_a: str = "b001",
    bib_b: str = "b002",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "decision": decision,
        "expected": expected,
        "category": category,
        "holdout": False,
        "notes": "smoke",
        "reviewed_by": "Maija M",
        "reviewed_at": "2026-06-04T10:00:00+00:00",
        "record_a": {"helmet_bib_id": bib_a, "title": "T-A", "creator": "C-A", "language": "fin"},
        "record_b": {"helmet_bib_id": bib_b, "title": "T-B", "creator": "C-B", "language": "fin"},
        "embedding_sim": 0.78,
        "llm_decision": llm_decision,
        "llm_confidence": 0.6,
        "llm_rationale": "rationale text exceeding twenty characters.",
        "matching_fields": [],
        "diverging_fields": ["preferred_title"],
        "used_cascade": False,
    }


class TestImportResults:
    def test_agree_lands_with_llm_decision_as_expected(self, tmp_path: Path) -> None:
        gold = tmp_path / "gold.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(
                    decision=DECISION_AGREE,
                    expected="different_work",
                    llm_decision="different_work",
                ),
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        assert summary.flipped_ids == []
        rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert len(rows) == 1
        gold_row = rows[0]
        assert gold_row["id"] == "gs-0001"
        assert gold_row["expected"] == "different_work"
        assert gold_row["category"] == "cross-genre-different-work"
        assert "Maija M" in gold_row["added_by"]
        # Cataloguer-only fields stripped.
        assert "decision" not in gold_row
        assert "llm_decision" not in gold_row
        assert "matching_fields" not in gold_row

    def test_flip_records_override_in_summary(self, tmp_path: Path) -> None:
        gold = tmp_path / "gold.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [
                _result_row(
                    row_id="cg-pending-0001",
                    decision=DECISION_FLIP_SAME,
                    expected="same_work",
                    llm_decision="different_work",
                    category="translation",
                ),
            ],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        assert summary.flipped_ids == [("gs-0001", "different_work", "same_work")]
        assert "Cataloguer overrode" in summary.render()

    def test_uncertain_does_not_land(self, tmp_path: Path) -> None:
        gold = tmp_path / "gold.jsonl"
        gold.write_text("", encoding="utf-8")
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(decision=DECISION_UNCERTAIN, expected="different_work")],
        )
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 0
        assert summary.still_uncertain == 1
        assert gold.read_text(encoding="utf-8") == ""

    def test_discard_and_skip_dont_land(self, tmp_path: Path) -> None:
        gold = tmp_path / "gold.jsonl"
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
        assert summary.skipped_ids == ["cg-pending-0002"]
        assert gold.read_text(encoding="utf-8") == ""

    def test_mints_sequential_ids_continuing_from_existing_gold(self, tmp_path: Path) -> None:
        gold = tmp_path / "gold.jsonl"
        # Pre-existing gold with gs-0007 as the tail.
        _write_jsonl(
            gold,
            [
                {
                    "id": "gs-0001",
                    "category": "translation",
                    "expected": "same_work",
                    "holdout": False,
                    "added": "2026-01-01",
                    "added_by": "bootstrap",
                    "notes": "",
                    "record_a": {"helmet_bib_id": "b1"},
                    "record_b": {"helmet_bib_id": "b2"},
                },
                {
                    "id": "gs-0007",
                    "category": "edition-revision",
                    "expected": "different_work",
                    "holdout": False,
                    "added": "2026-01-02",
                    "added_by": "bootstrap",
                    "notes": "",
                    "record_a": {"helmet_bib_id": "b3"},
                    "record_b": {"helmet_bib_id": "b4"},
                },
            ],
        )
        results = tmp_path / "results.jsonl"
        _write_jsonl(results, [_result_row()])
        summary = import_results(input_path=results, output_path=gold)
        assert summary.landed == 1
        all_rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        # Next id is gs-0008 (continues from max gs-0007).
        assert all_rows[-1]["id"] == "gs-0008"

    def test_validates_category(self, tmp_path: Path) -> None:
        gold = tmp_path / "gold.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(category="not-a-real-category")],
        )
        with pytest.raises(JudgeImportError, match=r"category"):
            import_results(input_path=results, output_path=gold)

    def test_validates_expected(self, tmp_path: Path) -> None:
        gold = tmp_path / "gold.jsonl"
        results = tmp_path / "results.jsonl"
        _write_jsonl(
            results,
            [_result_row(expected="uncertain")],  # not in {same_work, different_work}
        )
        with pytest.raises(JudgeImportError, match=r"expected"):
            import_results(input_path=results, output_path=gold)

    def test_gold_categories_match_gold_set_enum(self) -> None:
        """The judge handler's GOLD_CATEGORIES tuple must stay in
        sync with bffi_pipeline.eval.gold_set.GoldCategory — keeping
        them aligned prevents an import succeeding here but failing
        when the eval harness loads the gold file."""
        assert set(GOLD_CATEGORIES) == set(get_args(GoldCategory))
