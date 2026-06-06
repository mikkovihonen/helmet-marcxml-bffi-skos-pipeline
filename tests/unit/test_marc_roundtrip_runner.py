"""Smoke tests for the round-trip stage runner."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, SKOS

from bffi_pipeline.marc_roundtrip import run as roundtrip_run
from bffi_pipeline.marc_roundtrip.converter import MARC_NAMESPACE
from bffi_pipeline.provenance import vocab as V


def _write_canonical(tmp_path: Path) -> Path:
    """Build a tiny canonical.ttl with one Manifestation chain."""
    g = Graph()
    work = URIRef("urn:work/A")
    expr = URIRef("urn:expr/A")
    manif = URIRef("urn:manif/A")
    agent = URIRef("urn:agent/krag")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, SKOS.prefLabel, Literal("AADA WILDE", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((manif, RDF.type, V.BFFI.Manifestation))
    g.add((manif, V.BFFI.expressionManifested, expr))
    g.add((manif, DCTERMS.identifier, Literal("b13511105")))
    # PrimaryContribution
    contrib = URIRef("urn:contrib/pri")
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("KRAG, THOMAS PETER")))

    path = tmp_path / "canonical-skosified.ttl"
    g.serialize(destination=str(path), format="turtle")
    return path


def _write_original_marc(tmp_path: Path, bib_id: str) -> Path:
    """Write a minimal MARCXML record."""
    marc_dir = tmp_path / "marcxml"
    marc_dir.mkdir()
    body = textwrap.dedent(
        f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <record xmlns="{MARC_NAMESPACE}">
          <leader>00000nam  2200000   4500</leader>
          <controlfield tag="001">{bib_id}</controlfield>
          <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">KRAG, THOMAS PETER</subfield>
          </datafield>
          <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">AADA WILDE</subfield>
          </datafield>
          <datafield tag="084" ind1=" " ind2=" ">
            <subfield code="a">84.2</subfield>
            <subfield code="2">ykl</subfield>
          </datafield>
        </record>
        """
    )
    (marc_dir / f"{bib_id}.xml").write_text(body, encoding="utf-8")
    return marc_dir


def test_runner_writes_reconstructed_marc_and_diff_per_bib(tmp_path: Path) -> None:
    canonical = _write_canonical(tmp_path)
    marc_dir = _write_original_marc(tmp_path, "b13511105")
    output = tmp_path / "marc-roundtrip"

    summary = roundtrip_run(
        canonical_path=canonical,
        marcxml_dir=marc_dir,
        output_dir=output,
    )

    assert summary.total_manifestations == 1
    assert summary.reconstructed == 1
    assert summary.diffed == 1
    assert summary.missing_original == 0

    # Files on disk
    assert (output / "reconstructed" / "b13511105.xml").is_file()
    diff_path = output / "diffs" / "b13511105.json"
    assert diff_path.is_file()
    summary_path = output / "summary.json"
    assert summary_path.is_file()

    # Diff payload shape
    payload = json.loads(diff_path.read_text())
    assert payload["bib_id"] == "b13511105"
    statuses = {f["status"] for f in payload["fields"]}
    # Original has 100 + 245 + 084. Reconstructed has 100 + 245 + 040(synth)
    # + 008 + 907 + LDR + 001/003. We expect a mix of identical/lost/added.
    assert "identical" in statuses or "changed" in statuses
    assert "lost" in statuses  # 084 ykl classification


def test_runner_records_missing_original_softly(tmp_path: Path) -> None:
    """When a bib has no source MARCXML, the runner emits a stub
    diff JSON and counts it in the summary's ``missing_original``
    instead of crashing."""
    canonical = _write_canonical(tmp_path)
    marc_dir = tmp_path / "marcxml-empty"
    marc_dir.mkdir()
    output = tmp_path / "marc-roundtrip"

    summary = roundtrip_run(
        canonical_path=canonical,
        marcxml_dir=marc_dir,
        output_dir=output,
    )
    assert summary.reconstructed == 1
    assert summary.diffed == 0
    assert summary.missing_original == 1
    assert summary.missing_bib_ids == ["b13511105"]

    stub = json.loads((output / "diffs" / "b13511105.json").read_text())
    assert stub["error"] == "original_marcxml_not_found"


def test_summary_json_aggregates_per_status_counts(tmp_path: Path) -> None:
    canonical = _write_canonical(tmp_path)
    marc_dir = _write_original_marc(tmp_path, "b13511105")
    output = tmp_path / "marc-roundtrip"
    roundtrip_run(canonical_path=canonical, marcxml_dir=marc_dir, output_dir=output)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["per_status"]
    assert isinstance(summary["per_status"], dict)
