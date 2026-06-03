"""Unit tests for the review-bundle infrastructure.

Covers the stage-agnostic pieces — manifest schema, zip build/read,
MARC sidecar attachment, missing-MARC fallback, unknown-stage
tolerance, and the composite-name soft warning surfaced by the
contrib handler at import time.

Per-stage round-trip tests (KEEP/DISCARD/SKIP for contrib) live in
``test_review_bundle_stages_contrib.py``.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from bffi_pipeline.eval.review_bundle import (
    BundleBuildError,
    BundleReadError,
    Manifest,
    StageEntry,
    build_bundle,
    import_results,
    read_bundle,
)
from bffi_pipeline.eval.review_bundle.manifest import MANIFEST_SCHEMA
from bffi_pipeline.eval.review_bundle.stages.contrib import (
    DECISION_DISCARD,
    DECISION_KEEP,
    DECISION_SKIP,
)

# --- Helpers --------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


def _candidate(*, row_id: str, bib: str, category: str = "role-classification") -> dict[str, Any]:
    return {
        "id": row_id,
        "category": category,
        "helmet_bib_id": bib,
        "c_subfield": "by X Y",
        "existing_agents": ["Existing, Agent"],
        "expected_contributions": [{"name": "X Y", "relator_code": "aut"}],
        "holdout": False,
        "added": "2026-06-03",
        "added_by": "grow-gold-contrib",
        "notes": "",
    }


def _result_row(
    *,
    row_id: str,
    bib: str,
    decision: str = DECISION_KEEP,
    name: str = "X Y",
    category: str = "role-classification",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "helmet_bib_id": bib,
        "c_subfield": "by X Y",
        "existing_agents": ["Existing, Agent"],
        "decision": decision,
        "category": category,
        "holdout": False,
        "vetted_contributions": [
            {"name": name, "relator_code": "aut", "transliteration_of": None},
        ],
        "notes": "",
        "reviewed_by": "Smoke Test",
        "reviewed_at": "2026-06-03T10:00:00+00:00",
    }


# --- Manifest schema -----------------------------------------------------


class TestManifest:
    def test_round_trip_preserves_fields(self) -> None:
        m = Manifest(
            schema=MANIFEST_SCHEMA,
            created="2026-06-03T12:00:00+00:00",
            operator="Test Operator",
            stages=[
                StageEntry(
                    id="contrib",
                    label="M3 contrib extraction",
                    candidates="contrib/candidates.jsonl",
                    marc_dir="contrib/marc/",
                    schema="contrib-candidate/1",
                    marc_missing=["b999"],
                )
            ],
        )
        as_json = json.dumps(m.model_dump(by_alias=True))
        m2 = Manifest.model_validate_json(as_json)
        assert m2.operator == "Test Operator"
        assert m2.schema_ == MANIFEST_SCHEMA
        assert m2.stages[0].id == "contrib"
        assert m2.stages[0].schema_ == "contrib-candidate/1"
        assert m2.stages[0].marc_missing == ["b999"]

    def test_unknown_schema_is_not_a_parse_error(self) -> None:
        """The manifest itself doesn't validate the stage schema id —
        unknown ids flow through so the HTML can render a placeholder
        tab for stages a newer pipeline added."""
        as_json = json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "created": "2026-06-03T12:00:00+00:00",
                "operator": "Test",
                "stages": [
                    {
                        "id": "judge",
                        "label": "M6 judge (future)",
                        "candidates": "judge/candidates.jsonl",
                        "schema": "judge-pair/1",
                    }
                ],
            }
        )
        m = Manifest.model_validate_json(as_json)
        assert m.stages[0].schema_ == "judge-pair/1"


# --- build_bundle ---------------------------------------------------------


class TestBuildBundle:
    def test_attaches_marc_sidecars_when_present(self, tmp_path: Path) -> None:
        """The bundle builder copies <bib>.xml from the source MARC
        directory into <stage>/marc/<bib>.xml inside the zip for each
        candidate row whose MARC is available."""
        pool = tmp_path / "pool.jsonl"
        _write_jsonl(pool, [_candidate(row_id="r1", bib="b1"), _candidate(row_id="r2", bib="b2")])
        marc_dir = tmp_path / "marc"
        marc_dir.mkdir()
        (marc_dir / "b1.xml").write_text("<record>b1</record>", encoding="utf-8")
        (marc_dir / "b2.xml").write_text("<record>b2</record>", encoding="utf-8")

        out = tmp_path / "bundle.zip"
        result = build_bundle(
            output_path=out,
            operator="Tester",
            per_category=10,
            seed="seed-a",
            marc_dir=marc_dir,
            stage_schemas=["contrib-candidate/1"],
            pool_overrides={"contrib-candidate/1": pool},
        )

        assert out.exists()
        assert result.per_stage_marc_attached["contrib"] == 2
        assert result.per_stage_marc_missing["contrib"] == []
        with zipfile.ZipFile(out) as zf:
            names = sorted(zf.namelist())
        assert "manifest.json" in names
        assert "contrib/candidates.jsonl" in names
        assert "contrib/marc/b1.xml" in names
        assert "contrib/marc/b2.xml" in names

    def test_records_marc_missing_in_manifest(self, tmp_path: Path) -> None:
        """Bibs whose MARCXML isn't in the source dir land in
        manifest.json's per-stage ``marc_missing`` so the HTML can
        render a 'no MARC available' stub inline."""
        pool = tmp_path / "pool.jsonl"
        _write_jsonl(
            pool, [_candidate(row_id="r1", bib="b-present"), _candidate(row_id="r2", bib="b-gone")]
        )
        marc_dir = tmp_path / "marc"
        marc_dir.mkdir()
        (marc_dir / "b-present.xml").write_text("<record>p</record>", encoding="utf-8")

        out = tmp_path / "bundle.zip"
        result = build_bundle(
            output_path=out,
            operator="Tester",
            seed="seed-a",
            marc_dir=marc_dir,
            stage_schemas=["contrib-candidate/1"],
            pool_overrides={"contrib-candidate/1": pool},
        )

        assert result.per_stage_marc_attached["contrib"] == 1
        assert result.per_stage_marc_missing["contrib"] == ["b-gone"]
        with zipfile.ZipFile(out) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["stages"][0]["marc_missing"] == ["b-gone"]

    def test_unknown_stage_schema_raises(self, tmp_path: Path) -> None:
        """Asking the builder for a schema with no registered handler
        is an error — the operator should know up front, not after a
        cataloguer tries to import an unhandleable zip."""
        with pytest.raises(BundleBuildError, match=r"No stage handler"):
            build_bundle(
                output_path=tmp_path / "x.zip",
                operator="Tester",
                stage_schemas=["nonexistent/1"],
            )

    def test_missing_pool_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BundleBuildError, match=r"pool file not found"):
            build_bundle(
                output_path=tmp_path / "x.zip",
                operator="Tester",
                pool_overrides={"contrib-candidate/1": tmp_path / "does-not-exist.jsonl"},
            )


# --- read_bundle / import_results ----------------------------------------


def _make_results_zip(
    *,
    out: Path,
    operator: str,
    result_rows: list[dict[str, Any]],
) -> None:
    """Build a results-side zip mirroring the build-side bundle
    layout but carrying ``contrib/results.jsonl`` instead of
    ``contrib/candidates.jsonl``. This is what the HTML exports."""
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "created": "2026-06-03T12:00:00+00:00",
        "operator": operator,
        "stages": [
            {
                "id": "contrib",
                "label": "M3 contrib extraction",
                "candidates": "contrib/candidates.jsonl",  # path symmetry; results sits next to it
                "marc_dir": "contrib/marc/",
                "schema": "contrib-candidate/1",
                "marc_missing": [],
            }
        ],
    }
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(
            "contrib/results.jsonl",
            "\n".join(json.dumps(r, ensure_ascii=False) for r in result_rows) + "\n",
        )


class TestImportResults:
    def test_dispatches_to_contrib_handler(self, tmp_path: Path) -> None:
        """A results zip dispatches each stage's results.jsonl to the
        registered handler. For contrib, KEEP rows land in
        gold/contrib.jsonl."""
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
        zip_path = tmp_path / "results.zip"
        _make_results_zip(
            out=zip_path,
            operator="Tester",
            result_rows=[
                _result_row(row_id="cg-pending-0001", bib="b001", decision=DECISION_KEEP),
                _result_row(row_id="cg-pending-0002", bib="b002", decision=DECISION_DISCARD),
                _result_row(row_id="cg-pending-0003", bib="b003", decision=DECISION_SKIP),
            ],
        )

        # Run import with gold_overrides so the test doesn't dirty the real gold file.
        dispatch = import_results(
            input_path=zip_path,
            gold_overrides={"contrib-candidate/1": gold},
        )
        assert "contrib" in dispatch.per_stage_summaries
        summary = dispatch.per_stage_summaries["contrib"]
        assert summary.kept == 1
        assert summary.discarded == 1
        assert summary.skipped == 1
        gold_rows = [json.loads(ln) for ln in gold.read_text(encoding="utf-8").splitlines() if ln]
        assert len(gold_rows) == 2
        assert gold_rows[-1]["id"] == "cg-0002"

    def test_unknown_stage_recorded_without_aborting_known(self, tmp_path: Path) -> None:
        """Future-stage results that no handler knows about end up in
        ``unknown_stages``, but the known stages still get imported."""
        gold = tmp_path / "contrib.jsonl"
        gold.write_text("", encoding="utf-8")

        zip_path = tmp_path / "results.zip"
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "created": "2026-06-03T12:00:00+00:00",
            "operator": "Tester",
            "stages": [
                {
                    "id": "contrib",
                    "label": "M3 contrib extraction",
                    "candidates": "contrib/candidates.jsonl",
                    "marc_dir": "contrib/marc/",
                    "schema": "contrib-candidate/1",
                    "marc_missing": [],
                },
                {
                    "id": "picker",
                    "label": "M9 picker (future)",
                    "candidates": "picker/candidates.jsonl",
                    "schema": "picker-choice/1",
                    "marc_missing": [],
                },
            ],
        }
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr(
                "contrib/results.jsonl",
                json.dumps(_result_row(row_id="cg-pending-0001", bib="b001")) + "\n",
            )
            zf.writestr("picker/results.jsonl", "{}\n")

        dispatch = import_results(
            input_path=zip_path,
            gold_overrides={"contrib-candidate/1": gold},
        )
        assert "contrib" in dispatch.per_stage_summaries
        assert dispatch.unknown_stages == ["picker-choice/1"]
        assert dispatch.per_stage_summaries["contrib"].kept == 1

    def test_missing_results_jsonl_raises(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "broken.zip"
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "created": "2026-06-03T12:00:00+00:00",
            "operator": "Tester",
            "stages": [
                {
                    "id": "contrib",
                    "label": "M3 contrib extraction",
                    "candidates": "contrib/candidates.jsonl",
                    "marc_dir": "contrib/marc/",
                    "schema": "contrib-candidate/1",
                    "marc_missing": [],
                }
            ],
        }
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            # No contrib/results.jsonl entry.

        with pytest.raises(BundleReadError, match=r"contrib/results\.jsonl"):
            import_results(
                input_path=zip_path,
                gold_overrides={"contrib-candidate/1": tmp_path / "g.jsonl"},
            )

    def test_composite_name_soft_warning(self, tmp_path: Path) -> None:
        """A KEEP row whose name still carries a multi-author
        separator at import time surfaces in the summary's
        ``composite_name_kept`` list, but the import still succeeds."""
        gold = tmp_path / "contrib.jsonl"
        gold.write_text("", encoding="utf-8")
        zip_path = tmp_path / "results.zip"
        _make_results_zip(
            out=zip_path,
            operator="Tester",
            result_rows=[
                _result_row(
                    row_id="cg-pending-0001", bib="b001", name="Bill Hailey and The Comets"
                ),
                _result_row(row_id="cg-pending-0002", bib="b002", name="Eija Aarnio .. et al."),
                _result_row(row_id="cg-pending-0003", bib="b003", name="Andersson, Lars"),
            ],
        )

        dispatch = import_results(
            input_path=zip_path,
            gold_overrides={"contrib-candidate/1": gold},
        )
        summary = dispatch.per_stage_summaries["contrib"]
        assert summary.kept == 3
        composite_names = [name for (_id, name) in summary.composite_name_kept]
        assert "Bill Hailey and The Comets" in composite_names
        assert "Eija Aarnio .. et al." in composite_names
        # "Andersson, Lars" must NOT trip the regex (word-boundary on "and").
        assert "Andersson, Lars" not in composite_names
        text = summary.render()
        assert "Composite names retained on 2 row(s)" in text


# --- read_bundle ---------------------------------------------------------


class TestReadBundle:
    def test_round_trip_with_build_bundle(self, tmp_path: Path) -> None:
        pool = tmp_path / "pool.jsonl"
        _write_jsonl(pool, [_candidate(row_id="r1", bib="b1")])
        marc_dir = tmp_path / "marc"
        marc_dir.mkdir()
        (marc_dir / "b1.xml").write_text("<record>b1</record>", encoding="utf-8")

        out = tmp_path / "bundle.zip"
        build_bundle(
            output_path=out,
            operator="Tester",
            seed="seed-a",
            marc_dir=marc_dir,
            stage_schemas=["contrib-candidate/1"],
            pool_overrides={"contrib-candidate/1": pool},
        )

        manifest, files = read_bundle(out)
        assert manifest.operator == "Tester"
        assert "contrib/candidates.jsonl" in files
        assert files["contrib/marc/b1.xml"] == b"<record>b1</record>"

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "no-manifest.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("contrib/candidates.jsonl", "{}\n")
        with pytest.raises(BundleReadError, match=r"manifest\.json"):
            read_bundle(zip_path)
