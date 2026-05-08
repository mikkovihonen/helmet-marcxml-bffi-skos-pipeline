"""End-to-end test for stages/bf_to_bffi.

Runs M2 then M3 against the synthetic fixture set, then checks that the
BFFI Turtle outputs and the validation log are well-formed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.bf_to_bffi import BffiSummary
from bffi_pipeline.stages.bf_to_bffi import run as run_m3
from bffi_pipeline.stages.marc_to_bf import run as run_m2
from bffi_pipeline.uris import mint_raw_expression_uri, mint_raw_work_uri

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "sample-marcxml"
VALID_IDS = {"10000001", "10000002", "10000003", "10000004", "10000005", "10000006"}


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


def test_idempotent_rerun(converted: tuple[Path, BffiSummary]) -> None:
    out, _ = converted
    s2 = run_m3(output_dir=out)
    assert not s2.converted
    assert set(s2.skipped_idempotent) == VALID_IDS
