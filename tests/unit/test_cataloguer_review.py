"""Unit tests for the unified cataloguer-review TSV helpers."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

import pytest

from bffi_pipeline.cataloguer_review import (
    _reset_for_tests,
    append_source_row,
    append_target_row,
)
from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.observability import (
    StageEventEmitter,
    set_active_emitter,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin BFFI_DATA_DIR to a fresh tmp_path per test, reset module state."""
    monkeypatch.setenv("BFFI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")
    monkeypatch.setenv("BFFI_RUN_UUID", "testrun-uuid")
    get_settings.cache_clear()
    _reset_for_tests()
    set_active_emitter(None)
    yield
    get_settings.cache_clear()
    _reset_for_tests()
    set_active_emitter(None)


@pytest.fixture
def active_emitter(tmp_path: Path) -> Iterator[StageEventEmitter]:
    emitter = StageEventEmitter(sidecar_path=None, run_uuid="testrun-uuid")
    set_active_emitter(emitter)
    yield emitter
    set_active_emitter(None)


def _read_tsv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh, delimiter="\t"))


# --- source-review --------------------------------------------------------


def test_append_source_row_writes_header_then_data(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    append_source_row(
        bib_id="b10001",
        stage="m2",
        category="bibframe-conversion",
        severity="blocking",
        details="MARCXML parse failure",
        marcxml_path="/in/b10001.xml",
    )
    path = tmp_path / "cataloguer-source-review-testrun-uuid.tsv"
    rows = _read_tsv(path)
    assert len(rows) == 2
    assert rows[0] == [
        "run_uuid",
        "bib_id",
        "stage",
        "category",
        "severity",
        "details",
        "marcxml_path",
        "flagged_at",
        "reviewed_by",
        "reviewed_at",
        "notes",
    ]
    # Data row carries the input; cataloguer-fill-in cols are empty.
    data = rows[1]
    assert data[0:7] == [
        "testrun-uuid",
        "b10001",
        "m2",
        "bibframe-conversion",
        "blocking",
        "MARCXML parse failure",
        "/in/b10001.xml",
    ]
    assert data[8:] == ["", "", ""]  # reviewed_by, reviewed_at, notes


def test_append_source_row_writes_header_once(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    for i in range(3):
        append_source_row(
            bib_id=f"b{i:05d}",
            stage="m2",
            category="bibframe-shape",
            severity="blocking",
            details=f"err {i}",
        )
    rows = _read_tsv(tmp_path / "cataloguer-source-review-testrun-uuid.tsv")
    # 1 header + 3 data rows.
    assert len(rows) == 4
    assert rows[0][0] == "run_uuid"


def test_append_source_row_dedupes_on_bib_stage_category(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    for _ in range(3):
        append_source_row(
            bib_id="b10001",
            stage="m2",
            category="bibframe-conversion",
            severity="blocking",
            details="dup",
        )
    rows = _read_tsv(tmp_path / "cataloguer-source-review-testrun-uuid.tsv")
    assert len(rows) == 2  # header + one data row


def test_append_source_row_escapes_tabs_quotes_newlines(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    weird = 'She said,\t"yes"\nthen left'
    append_source_row(
        bib_id="b99",
        stage="m3",
        category="boundary-3",
        severity="warning",
        details=weird,
    )
    rows = _read_tsv(tmp_path / "cataloguer-source-review-testrun-uuid.tsv")
    assert len(rows) == 2
    # csv.reader round-trip recovers the exact bytes.
    assert rows[1][5] == weird


def test_append_source_row_truncates_long_details(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    long_msg = "x" * 500
    append_source_row(
        bib_id="b99",
        stage="m3",
        category="boundary-3",
        severity="warning",
        details=long_msg,
    )
    rows = _read_tsv(tmp_path / "cataloguer-source-review-testrun-uuid.tsv")
    details = rows[1][5]
    # 240 chars + the ellipsis sentinel.
    assert len(details) == 241
    assert details.endswith("…")


def test_append_source_row_noop_without_emitter(tmp_path: Path) -> None:
    # No emitter set → helper silently returns; no file created.
    append_source_row(
        bib_id="b1",
        stage="m2",
        category="x",
        severity="blocking",
        details="dropped",
    )
    assert not (tmp_path / "cataloguer-source-review-testrun-uuid.tsv").exists()


# --- target-review --------------------------------------------------------


def test_append_target_row_writes_header_then_data(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    append_target_row(
        canonical_work_uri="http://urn.fi/work:abc",
        reason="m8-conflict",
        confidence=None,
        expression_uris=["http://urn.fi/expr:abc"],
        member_bib_ids=["b1", "b2"],
    )
    rows = _read_tsv(tmp_path / "cataloguer-target-review-testrun-uuid.tsv")
    assert rows[0] == [
        "run_uuid",
        "canonical_work_uri",
        "expression_uris",
        "reason",
        "confidence",
        "member_bib_ids",
        "skosmos_url",
        "flagged_at",
        "cataloguer_verdict",
        "severity",
        "expected_behavior",
        "reviewed_by",
        "reviewed_at",
        "notes",
    ]
    data = rows[1]
    assert data[1] == "http://urn.fi/work:abc"
    assert data[2] == "http://urn.fi/expr:abc"  # pipe-joined single-item list
    assert data[3] == "m8-conflict"
    assert data[4] == ""  # confidence None → ""
    assert data[5] == "b1|b2"
    assert data[6] == ""  # skosmos_url None → ""
    # cataloguer-fill-in columns all empty.
    assert data[8:] == ["", "", "", "", "", ""]


def test_append_target_row_serialises_confidence_float(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    append_target_row(
        canonical_work_uri="http://urn.fi/work:x",
        reason="m9-fallback",
        confidence=0.7234,
    )
    rows = _read_tsv(tmp_path / "cataloguer-target-review-testrun-uuid.tsv")
    assert rows[1][4] == "0.7234"


def test_append_target_row_dedupes_on_uri_plus_reason(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    for _ in range(3):
        append_target_row(
            canonical_work_uri="http://urn.fi/work:dup",
            reason="m9-fallback",
            confidence=0.5,
        )
    rows = _read_tsv(tmp_path / "cataloguer-target-review-testrun-uuid.tsv")
    assert len(rows) == 2


def test_append_target_row_different_reason_same_uri_writes_two_rows(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    """The same canonical Work can carry independent flags from
    different stages (M8 conflict and M9 fallback); both rows land."""
    append_target_row(
        canonical_work_uri="http://urn.fi/work:abc",
        reason="m8-conflict",
        confidence=None,
    )
    append_target_row(
        canonical_work_uri="http://urn.fi/work:abc",
        reason="m9-fallback",
        confidence=0.6,
    )
    rows = _read_tsv(tmp_path / "cataloguer-target-review-testrun-uuid.tsv")
    assert len(rows) == 3  # header + 2 data rows


def test_append_target_row_member_bib_ids_pipe_joined(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    """The bib_id list cataloguers use to inspect source records lands
    as pipe-separated values so the column is greppable from a one-
    liner without parsing each row."""
    append_target_row(
        canonical_work_uri="http://urn.fi/work:multi",
        reason="m8-conflict",
        confidence=None,
        member_bib_ids=["b10001", "b10002", "b10003"],
    )
    rows = _read_tsv(tmp_path / "cataloguer-target-review-testrun-uuid.tsv")
    assert rows[1][5] == "b10001|b10002|b10003"


def test_append_target_row_empty_list_columns_serialise_as_empty(
    active_emitter: StageEventEmitter, tmp_path: Path
) -> None:
    append_target_row(
        canonical_work_uri="http://urn.fi/work:empty",
        reason="m9-no-candidate",
        confidence=None,
        expression_uris=[],
        member_bib_ids=[],
    )
    rows = _read_tsv(tmp_path / "cataloguer-target-review-testrun-uuid.tsv")
    assert rows[1][2] == ""  # expression_uris
    assert rows[1][5] == ""  # member_bib_ids


def test_append_target_row_noop_without_emitter(tmp_path: Path) -> None:
    append_target_row(
        canonical_work_uri="http://urn.fi/x",
        reason="m9-fallback",
        confidence=0.5,
    )
    assert not (tmp_path / "cataloguer-target-review-testrun-uuid.tsv").exists()
