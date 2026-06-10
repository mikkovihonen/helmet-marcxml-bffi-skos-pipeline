"""Unit tests for the round-trip eval orchestrator."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bffi_pipeline.observability.events import StageEventEmitter, set_active_emitter
from bffi_pipeline.stages.bffi_to_marc.runner import (
    ConversionOptions as BffiToMarcOptions,
)
from bffi_pipeline.stages.bffi_to_marc.runner import (
    convert_one as bffi_to_marc_one,
)
from bffi_pipeline.stages.bibframe_to_bffi.mappings import load_rules
from bffi_pipeline.stages.bibframe_to_bffi.runner import (
    ConversionOptions as Bf2BffiOptions,
)
from bffi_pipeline.stages.bibframe_to_bffi.runner import (
    convert_one as bf2bffi_one,
)
from bffi_pipeline.stages.roundtrip_eval.runner import EvalOptions, run_eval

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SAMPLE_MARC = _REPO_ROOT / "third_party" / "marc2bibframe2" / "test" / "data" / "marc.xml"
_M2BF_XSL = _REPO_ROOT / "third_party" / "marc2bibframe2" / "xsl" / "marc2bibframe2.xsl"


def _run_full_pipeline(tmp_path: Path) -> tuple[Path, Path]:
    """Run MARC -> BIBFRAME -> BFFI -> MARC end-to-end against the vendored sample.

    Returns ``(source_dir, reconstructed_dir)`` for handoff to ``run_eval``.
    """
    source_dir = tmp_path / "source"
    bibframe_dir = tmp_path / "bibframe"
    bffi_dir = tmp_path / "bffi"
    recon_dir = tmp_path / "reconstructed"
    for d in (source_dir, bibframe_dir, bffi_dir, recon_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Stage 1: lay down the vendored MARCXML as the source.
    src = source_dir / "13600108.xml"
    src.write_bytes(_SAMPLE_MARC.read_bytes())

    # Stage 2: MARC -> BIBFRAME (direct xsltproc, avoids preprocess for speed).
    result = subprocess.run(
        ["xsltproc", str(_M2BF_XSL), str(src)],
        capture_output=True,
        text=True,
        check=True,
    )
    (bibframe_dir / "13600108.bibframe.xml").write_text(result.stdout, encoding="utf-8")

    # Stage 3: BIBFRAME -> BFFI.
    bf2bffi_one(
        bibframe_dir / "13600108.bibframe.xml",
        options=Bf2BffiOptions(input_dir=bibframe_dir, output_dir=bffi_dir),
        rules=load_rules(),
    )

    # Stage 4: BFFI -> MARC.
    bffi_to_marc_one(
        bffi_dir / "13600108.bffi.ttl",
        options=BffiToMarcOptions(input_dir=bffi_dir, output_dir=recon_dir),
    )

    return source_dir, recon_dir


def test_run_eval_full_pipeline_paired_record(tmp_path: Path) -> None:
    """End-to-end smoke: run all four stages, then eval. The vendored
    record should pair (same 001), the 001 line should be `identical`,
    and the per-record diff should contain both ``identical`` and ``lost``
    rows (lost because step-4 v0 only emits leader + 001 + 245)."""
    source_dir, recon_dir = _run_full_pipeline(tmp_path)
    summary = run_eval(
        options=EvalOptions(
            source_dir=source_dir,
            reconstructed_dir=recon_dir,
        )
    )
    assert summary.total_pairs == 1
    assert summary.diffed == 1
    assert summary.failed == 0
    # The 001 + 245 fields are reconstructed; everything else is lost.
    assert summary.distribution["identical"] >= 1
    assert summary.distribution["lost"] >= 1


def test_run_eval_emits_sidecar_events(tmp_path: Path) -> None:
    source_dir, recon_dir = _run_full_pipeline(tmp_path)
    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="test-run")
    set_active_emitter(emitter)
    try:
        run_eval(options=EvalOptions(source_dir=source_dir, reconstructed_dir=recon_dir))
    finally:
        set_active_emitter(None)

    events = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert all(e["stage"] == "roundtrip_eval" for e in events)
    start = next(e for e in events if e["event"] == "start")
    assert start["counters"]["entities_total"] == 1
    end = next(e for e in events if e["event"] == "end")
    assert end["counters"]["diffed"] == 1
    # The corpus distribution lands as additional counter labels on the end event.
    assert "identical" in end["counters"]
    assert "lost" in end["counters"]


def test_run_eval_renders_html_when_path_given(tmp_path: Path) -> None:
    source_dir, recon_dir = _run_full_pipeline(tmp_path)
    html_out = tmp_path / "report.html"
    run_eval(
        options=EvalOptions(
            source_dir=source_dir,
            reconstructed_dir=recon_dir,
            html_path=html_out,
        )
    )
    assert html_out.exists()
    body = html_out.read_text(encoding="utf-8")
    assert "<html" in body
    assert "13600108" in body  # bib ID surfaces in the per-record overview


def test_run_eval_reports_source_only_and_reconstructed_only_counts(tmp_path: Path) -> None:
    """Orphan-side records are counted separately from the diffed pairs."""
    source_dir = tmp_path / "source"
    recon_dir = tmp_path / "reconstructed"
    source_dir.mkdir()
    recon_dir.mkdir()

    # Source has b1, reconstructed has b2 — no pair.
    src_payload = b"""<?xml version='1.0' encoding='utf-8'?>
<record xmlns="http://www.loc.gov/MARC21/slim">
  <controlfield tag="001">b1</controlfield>
</record>"""
    recon_payload = b"""<?xml version='1.0' encoding='utf-8'?>
<record xmlns="http://www.loc.gov/MARC21/slim">
  <controlfield tag="001">b2</controlfield>
</record>"""
    (source_dir / "src.xml").write_bytes(src_payload)
    (recon_dir / "recon.marcxml").write_bytes(recon_payload)

    summary = run_eval(options=EvalOptions(source_dir=source_dir, reconstructed_dir=recon_dir))
    assert summary.total_pairs == 0
    assert summary.diffed == 0
    assert summary.source_only == 1
    assert summary.reconstructed_only == 1


@pytest.fixture(autouse=True)
def _clear_active_emitter() -> None:
    yield
    set_active_emitter(None)
