"""Tests for the ``cataloguer-bundle`` pipeline stage dispatcher.

The stage reads ``runs/<uuid>/contrib-candidates.jsonl`` (M3's per-fire
audit log), builds a review bundle into
``runs/<uuid>/cataloguer-review/bundle.zip``, and copies
``gold/cataloguer-review.html`` alongside. When the audit log is empty
or absent (heuristic-only M3 run, M3 skipped, etc.) the stage no-ops
without failing.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from bffi_pipeline.runner import _dispatch_cataloguer_bundle
from bffi_pipeline.stages.m3.contrib_audit import AUDIT_FILENAME


def _write_audit(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


def _audit_row(*, row_id: str, bib: str) -> dict[str, Any]:
    return {
        "id": row_id,
        "category": "role-classification",
        "helmet_bib_id": bib,
        "c_subfield": "by X Y",
        "existing_agents": [],
        "expected_contributions": [{"name": "X Y", "relator_code": "aut"}],
        "holdout": False,
        "added": "2026-06-03",
        "added_by": "m3-cascade",
        "notes": "",
    }


def _settings_with_run_dir(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``get_settings()`` at a temp run dir without touching the
    real runs-root. The dispatcher reads ``settings.data_dir`` for the
    run dir and ``settings.run_uuid`` for the manifest operator field
    + the sampling seed.
    """

    class _Stub:
        data_dir = run_dir
        run_uuid = "test-run-uuid"

    def _get_stub() -> _Stub:
        return _Stub()

    monkeypatch.setattr("bffi_pipeline.runner.get_settings", _get_stub)


class TestCataloguerBundleDispatcher:
    def test_skips_when_audit_log_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No audit log → stage no-ops with a clear message, doesn't
        create the bundle dir, doesn't fail."""
        run_dir = tmp_path / "run-x"
        run_dir.mkdir()
        _settings_with_run_dir(run_dir, monkeypatch)

        _dispatch_cataloguer_bundle()

        out = capsys.readouterr().out
        assert "No populated audit logs found" in out
        assert not (run_dir / "cataloguer-review").exists()

    def test_skips_when_audit_log_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Empty audit log (M3 ran but heuristic never fired) → no-op."""
        run_dir = tmp_path / "run-x"
        run_dir.mkdir()
        (run_dir / AUDIT_FILENAME).write_text("", encoding="utf-8")
        _settings_with_run_dir(run_dir, monkeypatch)

        _dispatch_cataloguer_bundle()

        out = capsys.readouterr().out
        assert "No populated audit logs found" in out
        assert not (run_dir / "cataloguer-review").exists()

    def test_builds_bundle_when_audit_log_populated(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Audit log has rows → stage builds the bundle, copies the
        HTML reviewer alongside. MARC sidecars are best-effort (the
        Helmet MARC dir won't exist in tests; the stage handles that
        via the manifest's ``marc_missing`` list and doesn't fail)."""
        run_dir = tmp_path / "run-x"
        run_dir.mkdir()
        _write_audit(
            run_dir / AUDIT_FILENAME,
            [_audit_row(row_id="cg-pending-0001", bib="b001")],
        )
        # Re-route the MARC dir to an empty tmp dir so missing-MARC
        # behaviour is exercised but the stage still completes.
        empty_marc = tmp_path / "marc-empty"
        empty_marc.mkdir()
        monkeypatch.setattr(
            "bffi_pipeline.eval.review_bundle.bundle.DEFAULT_HELMET_MARC_DIR",
            empty_marc,
        )
        _settings_with_run_dir(run_dir, monkeypatch)

        _dispatch_cataloguer_bundle()

        bundle_dir = run_dir / "cataloguer-review"
        assert bundle_dir.is_dir()
        bundle = bundle_dir / "bundle.zip"
        assert bundle.is_file()
        html = bundle_dir / "cataloguer-review.html"
        assert html.is_file()
        # Confirm the bundle's manifest names the run as operator.
        with zipfile.ZipFile(bundle) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["operator"] == "pipeline-run:test-run-uuid"
        assert manifest["stages"][0]["id"] == "contrib"
        assert manifest["stages"][0]["marc_missing"] == ["b001"]
        # Stage prints both the build summary and the HTML location.
        out = capsys.readouterr().out
        assert "HTML reviewer at" in out
