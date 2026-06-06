"""End-to-end test for stages/bf_to_bffi.

Runs M2 then M3 against the synthetic fixture set, then checks that the
BFFI Turtle outputs and the validation log are well-formed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m2 import run as run_m2
from bffi_pipeline.stages.m3 import BffiSummary
from bffi_pipeline.stages.m3 import run as run_m3
from bffi_pipeline.uris import (
    mint_raw_expression_uri,
    mint_raw_manifestation_uri,
    mint_raw_work_uri,
)

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "sample-marcxml"
#: ``10000007`` is the P-41 B1 salvage smoke fixture (245$c → MARC 100).
#: ``10000008`` is the P-41 B3 sentinel smoke fixture (no parseable
#: 245$c, falls through to the shared sentinel agent at MARC 710).
#: Both flow through M3 normally because the synthesised
#: 100/710 datafields look identical to cataloguer-typed ones once
#: M2 is done.
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


@pytest.fixture(scope="module")
def converted(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, BffiSummary]:
    out = tmp_path_factory.mktemp("m3-out")
    run_m2(FIXTURES, output_dir=out)
    summary = run_m3(output_dir=out)
    return out, summary


def test_summary_has_no_hard_errors(converted: tuple[Path, BffiSummary]) -> None:
    _, summary = converted
    assert not summary.errored
    assert set(summary.converted) == VALID_IDS
    assert not summary.failed_shape


def test_per_record_turtle_outputs_exist(converted: tuple[Path, BffiSummary]) -> None:
    out, _ = converted
    for bib_id in VALID_IDS:
        assert (out / "bffi" / f"{bib_id}.ttl").is_file()


def test_validation_log_only_lists_failures(converted: tuple[Path, BffiSummary]) -> None:
    out, summary = converted
    log = out / "bffi" / "_validation.jsonl"
    if not summary.failed_shape:
        assert not log.exists()
        return
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert {row["helmet_bib_id"] for row in rows} == set(summary.failed_shape)


def test_round_trip_uris_match_python_minters(
    converted: tuple[Path, BffiSummary],
) -> None:
    out, _ = converted
    g = Graph()
    g.parse(out / "bffi" / "10000001.ttl", format="turtle")
    bf_work = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work"
    expected_work = URIRef(mint_raw_work_uri(bf_work))
    expected_expr = URIRef(mint_raw_expression_uri(bf_work))
    assert (expected_work, RDF.type, V.BFFI.Work) in g
    assert (expected_expr, RDF.type, V.BFFI.Expression) in g
    assert (expected_expr, V.BFFI.expressionOf, expected_work) in g


def test_manifestation_chain_round_trips_end_to_end(
    converted: tuple[Path, BffiSummary],
) -> None:
    """P-45 commit 6: end-to-end Manifestation smoke. Every record's M3
    output must carry a minted ``bffi:Manifestation`` linked to its
    Expression via ``bffi:expressionManifested``, with the Helmet bib_id
    on the Manifestation (not on Work or Expression) and the BIBFRAME
    ``bf:media`` / ``bf:carrier`` URIs forwarded as ``bffi:media`` /
    ``bffi:carrier`` on the same node. Together these pin the full
    Work → Expression ← Manifestation chain that lets a cataloguer
    walk from a canonical Work back to every absorbed bib_id."""
    out, _ = converted
    bf_work = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work"
    bf_instance = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Instance"
    expected_expr = URIRef(mint_raw_expression_uri(bf_work))
    expected_manif = URIRef(mint_raw_manifestation_uri(bf_instance))

    g = Graph()
    g.parse(out / "bffi" / "10000001.ttl", format="turtle")

    assert (expected_manif, RDF.type, V.BFFI.Manifestation) in g
    assert (expected_manif, V.BFFI.expressionManifested, expected_expr) in g
    # bib_id (Sierra display form) lives on the Manifestation.
    idents = list(g.objects(expected_manif, V.BF.identifiedBy))
    assert len(idents) == 1
    # And the Sierra-flat dct:identifier denormalisation follows it.
    assert list(g.objects(expected_manif, DCTERMS.identifier)) == [Literal("10000001")]
    # bf:media + bf:carrier forwarded from the source bf:Instance.
    media_uris = list(g.objects(expected_manif, V.BFFI.media))
    carrier_uris = list(g.objects(expected_manif, V.BFFI.carrier))
    media_prefix = "http://id.loc.gov/vocabulary/mediaTypes/"
    carrier_prefix = "http://id.loc.gov/vocabulary/carriers/"
    assert media_uris and all(str(u).startswith(media_prefix) for u in media_uris)
    assert carrier_uris and all(str(u).startswith(carrier_prefix) for u in carrier_uris)
    # Work and Expression must NOT carry bf:identifiedBy after the
    # P-45 commit 2 migration.
    expected_work = URIRef(mint_raw_work_uri(bf_work))
    assert not list(g.objects(expected_work, V.BF.identifiedBy))
    assert not list(g.objects(expected_expr, V.BF.identifiedBy))


def test_idempotent_rerun(converted: tuple[Path, BffiSummary]) -> None:
    out, _ = converted
    s2 = run_m3(output_dir=out)
    assert not s2.converted
    assert set(s2.skipped_idempotent) == VALID_IDS
