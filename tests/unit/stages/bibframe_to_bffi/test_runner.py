"""Unit tests for the BIBFRAME -> BFFI corpus runner."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from rdflib import RDF, Graph, URIRef

from bffi_pipeline.observability.events import StageEventEmitter, set_active_emitter
from bffi_pipeline.stages.bibframe_to_bffi.mappings import (
    BF_NAMESPACE,
    BFFI_NAMESPACE,
    load_rules,
)
from bffi_pipeline.stages.bibframe_to_bffi.runner import (
    ConversionOptions,
    convert_corpus,
    convert_one,
)


def _parse_turtle(path: Path) -> Graph:
    g = Graph()
    g.parse(path, format="turtle")
    return g


_REPO_ROOT = Path(__file__).resolve().parents[4]
_SAMPLE_MARC = _REPO_ROOT / "third_party" / "marc2bibframe2" / "test" / "data" / "marc.xml"
_MARC2BFRAME_XSL = _REPO_ROOT / "third_party" / "marc2bibframe2" / "xsl" / "marc2bibframe2.xsl"


def _produce_bibframe_fixture(out_dir: Path, *, stem: str = "test") -> Path:
    """Run the vendored MARCXML through marc2bibframe2 to get a real BIBFRAME RDF/XML.

    Used as the fixture for the BIBFRAME -> BFFI runner; avoids hand-rolling
    BIBFRAME by hand or vendoring a fixture file. Fast enough at <1s.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    bibframe_path = out_dir / f"{stem}.bibframe.xml"
    result = subprocess.run(
        ["xsltproc", str(_MARC2BFRAME_XSL), str(_SAMPLE_MARC)],
        capture_output=True,
        text=True,
        check=True,
    )
    bibframe_path.write_text(result.stdout, encoding="utf-8")
    return bibframe_path


def test_convert_one_emits_bffi_person_for_bf_person(tmp_path: Path) -> None:
    """Sanity check the rename actually happens: at least one entity in
    the vendored fixture is typed ``bffi:Person`` after rename, and no
    entity carries the original ``bf:Person`` type."""
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    _produce_bibframe_fixture(in_dir, stem="test")
    options = ConversionOptions(input_dir=in_dir, output_dir=out_dir)

    output_path, _residual = convert_one(
        in_dir / "test.bibframe.xml", options=options, rules=load_rules()
    )
    assert output_path == out_dir / "test.bffi.ttl"

    g = _parse_turtle(output_path)
    bffi_person = URIRef(BFFI_NAMESPACE + "Person")
    bf_person = URIRef(BF_NAMESPACE + "Person")
    assert any(g.triples((None, RDF.type, bffi_person)))
    assert not any(g.triples((None, RDF.type, bf_person)))


def test_convert_one_renames_bf_work_to_bibframework(tmp_path: Path) -> None:
    """Memory-note canonical case: ``bf:Work`` -> ``bffi:BibframeWork`` (not
    ``bffi:Work``, which is a separate RDA-aligned concept)."""
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    _produce_bibframe_fixture(in_dir, stem="test")

    output_path, _ = convert_one(
        in_dir / "test.bibframe.xml",
        options=ConversionOptions(input_dir=in_dir, output_dir=out_dir),
        rules=load_rules(),
    )
    g = _parse_turtle(output_path)
    bf_work = URIRef(BF_NAMESPACE + "Work")
    bffi_bibframework = URIRef(BFFI_NAMESPACE + "BibframeWork")
    assert any(g.triples((None, RDF.type, bffi_bibframework)))
    assert not any(g.triples((None, RDF.type, bf_work)))


def test_convert_one_residual_count_surfaces_unhandled_bf_terms(tmp_path: Path) -> None:
    """Records that carry a discriminator-routed term (`bf:Hub`,
    `bf:VariantTitle`, …) carry a non-zero residual until step 6 lands.

    Use the vendored sample — it has at least one ``bf:Identifier``
    subclass without a clean rename rule (e.g. ``bf:Isbn``), so the
    residual should be positive.
    """
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    _produce_bibframe_fixture(in_dir, stem="test")

    _, residual = convert_one(
        in_dir / "test.bibframe.xml",
        options=ConversionOptions(input_dir=in_dir, output_dir=out_dir),
        rules=load_rules(),
    )
    # The vendored test record has at least one term that's discriminator
    # routed (e.g. bf:Isbn — step 6) so residual must be > 0.
    assert residual > 0


def test_convert_corpus_summary_and_sidecar_events(tmp_path: Path) -> None:
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    src = _produce_bibframe_fixture(in_dir, stem="test")
    # Duplicate the fixture so we exercise more than one iteration.
    for stem in ("a", "b"):
        shutil.copy(src, in_dir / f"{stem}.bibframe.xml")
    src.unlink()  # leave just a.bibframe.xml + b.bibframe.xml

    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="test-run")
    set_active_emitter(emitter)
    try:
        summary = convert_corpus(options=ConversionOptions(input_dir=in_dir, output_dir=out_dir))
    finally:
        set_active_emitter(None)

    assert summary.total == 2
    assert summary.converted == 2
    assert summary.failed == 0
    assert (out_dir / "a.bffi.ttl").exists()
    assert (out_dir / "b.bffi.ttl").exists()

    events = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert all(e["stage"] == "bibframe2bffi" for e in events)
    start = next(e for e in events if e["event"] == "start")
    assert start["counters"]["entities_total"] == 2
    end = next(e for e in events if e["event"] == "end")
    assert end["counters"]["success"] == 2
    assert end["counters"]["failed"] == 0
    # Residue is per-record, not corpus-summed; both copies of the same
    # fixture share the same residue value, so the count should be 2.
    assert end["counters"]["closed_namespace_residue"] == 2


def test_convert_corpus_emits_failed_event_on_bad_input(tmp_path: Path) -> None:
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    # rdflib parses "not xml" as turtle-ish whitespace and emits an empty
    # graph rather than raising — use actual invalid RDF/XML to provoke a
    # parse error.
    (in_dir / "broken.bibframe.xml").write_text("<rdf:RDF><not-closed-tag", encoding="utf-8")

    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="test-run")
    set_active_emitter(emitter)
    try:
        summary = convert_corpus(options=ConversionOptions(input_dir=in_dir, output_dir=out_dir))
    finally:
        set_active_emitter(None)

    assert summary.total == 1
    assert summary.converted == 0
    assert summary.failed == 1
    failed_path, _msg = summary.failures[0]
    assert failed_path.name == "broken.bibframe.xml"


@pytest.fixture(autouse=True)
def _clear_active_emitter() -> None:
    """Defensive cleanup in case a test leaves a stale emitter set."""
    yield
    set_active_emitter(None)
