"""Unit tests for the MARC -> BIBFRAME corpus runner."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from bffi_pipeline.observability.events import StageEventEmitter, set_active_emitter
from bffi_pipeline.stages.marc_to_bibframe.runner import (
    ConversionOptions,
    convert_corpus,
    convert_one,
)
from bffi_pipeline.stages.marc_to_bibframe.xslt import XsltPaths

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SAMPLE_MARC = _REPO_ROOT / "third_party" / "marc2bibframe2" / "test" / "data" / "marc.xml"


def _options(input_dir: Path, output_dir: Path, *, preprocess: bool = True) -> ConversionOptions:
    return ConversionOptions(
        input_dir=input_dir,
        output_dir=output_dir,
        xslt_paths=XsltPaths.from_repo_root(_REPO_ROOT),
        baseuri="http://urn.fi/URN:NBN:fi:bib:",
        preprocess=preprocess,
    )


def test_convert_one_writes_rdf_xml_to_output_dir(tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    out_dir = tmp_path / "out"
    sample = input_dir / "test.xml"
    shutil.copy(_SAMPLE_MARC, sample)

    output_path = convert_one(sample, options=_options(input_dir, out_dir))

    assert output_path == out_dir / "test.bibframe.xml"
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "<rdf:RDF" in content
    assert "bf:Work" in content


def test_convert_one_without_preprocess_still_produces_rdf_xml(tmp_path: Path) -> None:
    """The preprocess flag is opt-out; the no-preprocess path still works."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    out_dir = tmp_path / "out"
    sample = input_dir / "test.xml"
    shutil.copy(_SAMPLE_MARC, sample)

    output_path = convert_one(sample, options=_options(input_dir, out_dir, preprocess=False))
    assert output_path.exists()
    assert "bf:Work" in output_path.read_text(encoding="utf-8")


def test_convert_corpus_summary_and_sidecar_events(tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    out_dir = tmp_path / "out"
    # Three identical input files — enough to verify the loop emits per-record.
    for name in ("a.xml", "b.xml", "c.xml"):
        shutil.copy(_SAMPLE_MARC, input_dir / name)

    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="test-run")
    set_active_emitter(emitter)
    try:
        summary = convert_corpus(options=_options(input_dir, out_dir))
    finally:
        set_active_emitter(None)

    assert summary.total == 3
    assert summary.converted == 3
    assert summary.failed == 0
    assert not summary.failures

    assert (out_dir / "a.bibframe.xml").exists()
    assert (out_dir / "b.bibframe.xml").exists()
    assert (out_dir / "c.bibframe.xml").exists()

    # Sidecar should carry a start, at least one progress (the final one),
    # and an end event — all labeled `marc2bibframe`.
    lines = sidecar.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    assert any(e["event"] == "start" for e in events)
    assert any(e["event"] == "end" for e in events)
    assert all(e["stage"] == "marc2bibframe" for e in events)
    start = next(e for e in events if e["event"] == "start")
    assert start["counters"]["entities_total"] == 3
    end = next(e for e in events if e["event"] == "end")
    assert end["counters"] == {"success": 3, "failed": 0}


def test_convert_corpus_emits_failed_event_on_bad_input(tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    out_dir = tmp_path / "out"
    # Write a syntactically-invalid XML file to provoke a failure.
    (input_dir / "broken.xml").write_text("not actually xml", encoding="utf-8")
    shutil.copy(_SAMPLE_MARC, input_dir / "good.xml")

    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="test-run")
    set_active_emitter(emitter)
    try:
        summary = convert_corpus(options=_options(input_dir, out_dir))
    finally:
        set_active_emitter(None)

    assert summary.total == 2
    assert summary.converted == 1
    assert summary.failed == 1
    assert len(summary.failures) == 1
    failed_path, message = summary.failures[0]
    assert failed_path.name == "broken.xml"
    assert message  # non-empty error text

    events = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    failed_events = [e for e in events if e["event"] == "failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["extra"]["path"].endswith("broken.xml")
