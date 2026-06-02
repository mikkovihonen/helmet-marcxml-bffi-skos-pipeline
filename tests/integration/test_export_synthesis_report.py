"""Integration test for P-41 Phase C.3 — retrospective regen of the
export-synthesis TSV from per-record BIBFRAME graphs."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from bffi_pipeline.release.export_synthesis_report import regen_export_synthesis_report
from bffi_pipeline.stages.m2 import run as run_m2

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "sample-marcxml"


@pytest.fixture(scope="module")
def m2_out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("regen-m2")
    run_m2(FIXTURES, output_dir=out)
    return out


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_regen_reads_synthesis_activities_from_per_record_graphs(
    m2_out: Path, tmp_path: Path
) -> None:
    """The B1 salvage smoke fixture (10000007) emits a Synthesis
    Activity into its per-record BIBFRAME RDF. The retrospective CLI
    reads it back and emits a single TSV row with all eight columns
    populated."""
    target = tmp_path / "regen.tsv"
    rows_emitted = regen_export_synthesis_report(data_dir=m2_out, output_path=target)
    assert rows_emitted == 1

    rows = _read_rows(target)
    assert len(rows) == 1
    row = rows[0]
    assert row["bib_id"] == "10000007"
    assert row["field"] == "bf:contribution/bf:agent"
    assert row["marc_source"] == "245$c"
    assert row["synthesised_value"] == "Mika Waltari"
    assert row["tier"] == "B1"
    assert "creator-from-245c" in row["method"]
    assert row["confidence"] == "0.8000"
    assert row["activity_uri"].startswith("http://urn.fi/URN:NBN:fi:bib:synthesis/")


def test_regen_is_idempotent(m2_out: Path, tmp_path: Path) -> None:
    """Running the regen twice on the same provenance state produces
    byte-identical files — the rebuild path is the contract Phase C
    promises consumers."""
    target = tmp_path / "regen.tsv"
    regen_export_synthesis_report(data_dir=m2_out, output_path=target)
    first = target.read_bytes()
    regen_export_synthesis_report(data_dir=m2_out, output_path=target)
    second = target.read_bytes()
    assert first == second


def test_regen_writes_header_with_full_column_set(m2_out: Path, tmp_path: Path) -> None:
    target = tmp_path / "regen.tsv"
    regen_export_synthesis_report(data_dir=m2_out, output_path=target)
    with target.open(encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
    assert header == [
        "bib_id",
        "field",
        "marc_source",
        "synthesised_value",
        "tier",
        "method",
        "confidence",
        "activity_uri",
    ]
