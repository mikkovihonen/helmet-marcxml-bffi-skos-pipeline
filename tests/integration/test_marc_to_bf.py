"""End-to-end test for stages/marc_to_bf.

Exercises XSLT + post-processor + Boundary 1/2 validation against the
synthetic fixture set. No Docker, no LLM — only the marc2bibframe2
submodule (which CI checks out via `submodules: recursive`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.provenance.vocab import is_synthetic_sentinel
from bffi_pipeline.stages.m2 import (
    ConversionErrorRow,
    ConversionSummary,
    HelmetMapRow,
    run,
)

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "sample-marcxml"
#: ``10000007`` is the P-41 Phase B salvage smoke fixture — record
#: with no 1XX/7XX + a parseable ``245$c "kirjoittanut Mika Waltari"``.
#: M2's creator-salvage layer rescues it via B1; the resulting BIBFRAME
#: carries a synthesised personal-name agent (Mika Waltari) lifted
#: from the statement of responsibility.
#: ``10000008`` is the P-41 Phase B.3 sentinel smoke fixture — record
#: with no 1XX/7XX and no parseable 245$c. M2 falls through to B3
#: and links the record to the shared sentinel agent at
#: ``http://urn.fi/URN:NBN:fi:bib:agent:unknown`` with the
#: ``bffi:syntheticSentinel "true"`` flag attached.
VALID_IDS = {
    "10000001",
    "10000002",
    "10000003",
    "10000004",
    "10000005",
    "10000006",
    "10000007",
    "10000008",
}
EXPECTED_FAILURES: dict[str, str] = {
    "99999900.xml": "marcxml-encoding",
    "99999901.xml": "marcxml-xsd-validation",
    "99999902.xml": "marcxml-content-minimum",
}


@pytest.fixture(scope="module")
def conversion(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, ConversionSummary]:
    out = tmp_path_factory.mktemp("m2-out")
    summary = run(FIXTURES, output_dir=out)
    return out, summary


def test_summary_counts(conversion: tuple[Path, ConversionSummary]) -> None:
    _, summary = conversion
    assert {p.split(".")[0] for p in summary.succeeded} == VALID_IDS
    assert len(summary.failed) == len(EXPECTED_FAILURES)
    assert not summary.skipped_idempotent


def test_per_record_outputs_exist(conversion: tuple[Path, ConversionSummary]) -> None:
    out, _ = conversion
    for bib_id in VALID_IDS:
        assert (out / "bibframe" / f"{bib_id}.rdf").is_file()


def test_helmet_map_rows(conversion: tuple[Path, ConversionSummary]) -> None:
    out, _ = conversion
    rows = [
        json.loads(line)
        for line in (out / "helmet-map.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows_by_id = {row["helmet_bib_id"]: row for row in rows}
    assert set(rows_by_id) == VALID_IDS
    for bib_id, row in rows_by_id.items():
        assert HelmetMapRow(**row).helmet_bib_id == bib_id
        assert row["raw_work_uri"].startswith("http://urn.fi/URN:NBN:fi:bib:raw/")
        assert row["raw_work_uri"].endswith("#Work")
        assert row["raw_instance_uri"].endswith("#Instance")
        assert row["marc2bibframe2_version"]


def test_errors_jsonl_classifies_each_broken_file(
    conversion: tuple[Path, ConversionSummary],
) -> None:
    out, _ = conversion
    rows = [
        ConversionErrorRow(**json.loads(line))
        for line in (out / "bibframe" / "_errors.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_filename = {row.filename: row for row in rows}
    for filename, error_type in EXPECTED_FAILURES.items():
        assert by_filename[filename].error_type == error_type


def test_helmet_identifier_emitted_on_work_and_instance(
    conversion: tuple[Path, ConversionSummary],
) -> None:
    out, _ = conversion
    g = Graph()
    g.parse(out / "bibframe" / "10000001.rdf", format="xml")

    work = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work")
    helmet_ids = []
    for ident in g.objects(work, V.BF.identifiedBy):
        if (ident, V.BF.source, V.HELMET_SOURCE_URI) in g:
            for value in g.objects(ident, RDF.value):
                helmet_ids.append(str(value))
    assert helmet_ids == ["10000001"]

    instance = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/10000001#Instance")
    inst_ids = []
    for ident in g.objects(instance, V.BF.identifiedBy):
        if (ident, V.BF.source, V.HELMET_SOURCE_URI) in g:
            for value in g.objects(ident, RDF.value):
                inst_ids.append(str(value))
    assert inst_ids == ["10000001"]


def test_marc_conversion_activity_present(
    conversion: tuple[Path, ConversionSummary],
) -> None:
    out, _ = conversion
    g = Graph()
    g.parse(out / "bibframe" / "10000001.rdf", format="xml")

    activities = list(g.subjects(RDF.type, V.MarcConversion))
    assert len(activities) == 1
    activity = activities[0]
    assert (activity, V.helmetBibId, Literal("10000001")) in g
    assert any(isinstance(o, Literal) for o in g.objects(activity, V.converterVersion))
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work")
    instance = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/10000001#Instance")
    assert (work, V.PROV.wasGeneratedBy, activity) in g
    assert (instance, V.PROV.wasGeneratedBy, activity) in g


def test_synthesis_activity_present_on_salvaged_record(
    conversion: tuple[Path, ConversionSummary],
) -> None:
    """P-41 Phase B.5 + C.2 — the salvage smoke fixture (10000007)
    produces a bffi-prov:Synthesis Activity in its BIBFRAME RDF with
    all six synthetic* predicates populated (field, method, tier,
    confidence, value, marc_source) + prov:used → MarcConversion chain.
    The non-salvaged fixture (10000001) emits NO Synthesis Activity."""
    out, _ = conversion

    # Salvaged record carries the Synthesis Activity.
    g_salvaged = Graph()
    g_salvaged.parse(out / "bibframe" / "10000007.rdf", format="xml")
    synth_activities = list(g_salvaged.subjects(RDF.type, V.Synthesis))
    assert len(synth_activities) == 1, "expected one Synthesis Activity on salvaged record"
    synth = synth_activities[0]

    # All six synthetic* predicates populated.
    assert (synth, V.syntheticField, Literal("bf:contribution/bf:agent")) in g_salvaged
    tier_value = next(g_salvaged.objects(synth, V.syntheticTier))
    assert str(tier_value) == "B1"
    method_value = next(g_salvaged.objects(synth, V.syntheticMethod))
    assert "creator-from-245c" in str(method_value)
    confidence_value = next(g_salvaged.objects(synth, V.syntheticConfidence))
    assert float(str(confidence_value)) >= 0.5
    value_value = next(g_salvaged.objects(synth, V.syntheticValue))
    # Post-2026-06-02 surname-first reformat: B1 emits "Waltari, Mika,"
    # in the synthesised 100$a (cataloguer convention) so the agent
    # blocks correctly with cataloguer-typed records of the same author.
    assert str(value_value) == "Waltari, Mika,"
    marc_source_value = next(g_salvaged.objects(synth, V.syntheticMarcSource))
    assert str(marc_source_value) == "245$c"

    # prov:used chains to a MarcConversion Activity.
    used = next(g_salvaged.objects(synth, V.PROV.used))
    assert (used, RDF.type, V.MarcConversion) in g_salvaged

    # Non-salvaged record has NO Synthesis Activity.
    g_clean = Graph()
    g_clean.parse(out / "bibframe" / "10000001.rdf", format="xml")
    assert list(g_clean.subjects(RDF.type, V.Synthesis)) == []


def test_b3_sentinel_carries_synthetic_sentinel_flag(
    conversion: tuple[Path, ConversionSummary],
) -> None:
    """P-41 Phase B.6 — the B3 salvage fixture (10000008) routes
    through to the shared sentinel agent. The BFFI graph must carry
    ``bffi:syntheticSentinel "true"^^xsd:boolean`` on the sentinel
    agent URI so :func:`is_synthetic_sentinel` returns True (and
    P-39's M9 walker can short-circuit on it without wasting a
    KANTO call). The Synthesis Activity carries tier=B3 and
    ``method="anonymous-by-convention"``."""
    out, _ = conversion
    g = Graph()
    g.parse(out / "bibframe" / "10000008.rdf", format="xml")

    sentinel = URIRef("http://urn.fi/URN:NBN:fi:bib:agent:unknown")
    assert is_synthetic_sentinel(g, sentinel), "sentinel agent missing syntheticSentinel flag"

    synth_activities = list(g.subjects(RDF.type, V.Synthesis))
    assert len(synth_activities) == 1
    synth = synth_activities[0]
    assert (synth, V.syntheticTier, Literal("B3")) in g
    assert (synth, V.syntheticMethod, Literal("anonymous-by-convention")) in g
    assert (synth, V.syntheticMarcSource, Literal("(none)")) in g


def test_admin_metadata_block_present(conversion: tuple[Path, ConversionSummary]) -> None:
    out, _ = conversion
    g = Graph()
    g.parse(out / "bibframe" / "10000001.rdf", format="xml")

    work = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work")
    am_targets = list(g.objects(work, V.adminMetadata))
    assert len(am_targets) == 1
    am = am_targets[0]
    # The ten M2 mandatory predicates plus the spine link.
    expected_predicates = [
        V.adminMetadataFor,
        V.descriptionCreationDate,
        V.dateGenerated,
        V.descriptionModifier,
        V.generationProcess,
        V.descriptionConventions,
        V.descriptionLevel,
        V.encodingLevel,
        V.descriptionAuthentication,
        V.metadataLicensor,
        V.recordingSource,
        V.sourceMetadata,
        V.PROV.wasGeneratedBy,
    ]
    for p in expected_predicates:
        assert any(g.triples((am, p, None))), f"AdminMetadata missing predicate {p}"


def test_idempotent_rerun_skips_existing_outputs(
    tmp_path: Path,
) -> None:
    summary1 = run(FIXTURES, output_dir=tmp_path)
    assert len(summary1.succeeded) == len(VALID_IDS)

    summary2 = run(FIXTURES, output_dir=tmp_path)
    assert not summary2.succeeded
    assert {p.split(".")[0] for p in summary2.skipped_idempotent} == VALID_IDS
    # Failures still re-emit because the broken inputs never produced output.
    assert {row.filename for row in summary2.failed} == set(EXPECTED_FAILURES)


def test_force_flag_reconverts_existing_outputs(tmp_path: Path) -> None:
    run(FIXTURES, output_dir=tmp_path)
    summary = run(FIXTURES, output_dir=tmp_path, force=True)
    assert {p.split(".")[0] for p in summary.succeeded} == VALID_IDS
