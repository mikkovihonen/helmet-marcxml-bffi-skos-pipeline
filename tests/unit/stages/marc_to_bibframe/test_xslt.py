"""Unit tests for the xsltproc wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from bffi_pipeline.stages.marc_to_bibframe.xslt import (
    XsltPaths,
    XsltprocError,
    run_xsltproc,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SAMPLE_MARC = _REPO_ROOT / "third_party" / "marc2bibframe2" / "test" / "data" / "marc.xml"


def test_xslt_paths_from_repo_root_points_at_vendored_stylesheets() -> None:
    paths = XsltPaths.from_repo_root(_REPO_ROOT)
    assert paths.convert.exists(), f"missing: {paths.convert}"
    assert paths.preprocess.exists(), f"missing: {paths.preprocess}"
    assert paths.convert.name == "marc2bibframe2.xsl"
    assert paths.preprocess.name == "ConvSpec-Preprocess0-Splitting.xsl"


def test_run_xsltproc_smoke_against_vendored_test_record() -> None:
    """The vendored marc.xml + main XSLT must produce a bf:Work RDF/XML doc."""
    paths = XsltPaths.from_repo_root(_REPO_ROOT)
    result = run_xsltproc(stylesheet=paths.convert, input_path=_SAMPLE_MARC)
    assert result.ok, result.stderr
    assert "<rdf:RDF" in result.stdout
    assert "bf:Work" in result.stdout


def test_run_xsltproc_passes_params_through_to_baseuri() -> None:
    paths = XsltPaths.from_repo_root(_REPO_ROOT)
    custom_base = "http://example.test/custom/"
    result = run_xsltproc(
        stylesheet=paths.convert,
        input_path=_SAMPLE_MARC,
        params={"baseuri": custom_base},
    )
    assert result.ok, result.stderr
    assert custom_base in result.stdout


def test_run_xsltproc_nonzero_exit_returns_not_ok(tmp_path: Path) -> None:
    """A missing input file makes xsltproc exit nonzero; we surface that as not-ok."""
    paths = XsltPaths.from_repo_root(_REPO_ROOT)
    missing = tmp_path / "does-not-exist.xml"
    result = run_xsltproc(stylesheet=paths.convert, input_path=missing)
    assert not result.ok
    assert result.returncode != 0


def test_run_xsltproc_missing_binary_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If xsltproc isn't on PATH the wrapper raises XsltprocError.

    Surfacing :exc:`FileNotFoundError` from the underlying ``subprocess.run``
    would be confusing — the wrapper translates it.
    """
    monkeypatch.setattr(
        "bffi_pipeline.stages.marc_to_bibframe.xslt.XSLTPROC",
        "xsltproc-does-not-exist-anywhere",
    )
    paths = XsltPaths.from_repo_root(_REPO_ROOT)
    with pytest.raises(XsltprocError, match="binary not found"):
        run_xsltproc(stylesheet=paths.convert, input_path=_SAMPLE_MARC)
