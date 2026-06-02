"""Unit tests for P-41 Phase C — per-run export-synthesis TSV writer."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bffi_pipeline import export_synthesis as ES
from bffi_pipeline.export_synthesis import _reset_for_tests, append_synthesis_row

if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeEmitter:
    """Stand-in for the observability emitter, carrying just the
    ``run_uuid`` field that the TSV writer reads."""

    def __init__(self, run_uuid: str) -> None:
        self.run_uuid = run_uuid


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    """Per-test isolation — clear module-level dedup state + header flag."""
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture
def emit_to(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Wire the writer to ``tmp_path`` by stubbing the emitter +
    Settings."""
    emitter = _FakeEmitter(run_uuid="run-1234")
    monkeypatch.setattr(ES, "get_active_emitter", lambda: emitter)

    class _StubSettings:
        data_dir = tmp_path

    stub = _StubSettings()
    monkeypatch.setattr(ES, "get_settings", lambda: stub)
    return tmp_path / "export-synthesis-run-1234.tsv"


def _read_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestHeader:
    def test_header_written_on_first_append(self, emit_to: Path) -> None:
        append_synthesis_row(
            bib_id="b001",
            field="bf:contribution/bf:agent",
            marc_source="245$c",
            synthesised_value="Mika Waltari",
            tier="B1",
            method="creator-from-245c (regex, role=author, marc=100)",
            confidence=0.8,
            activity_uri="http://urn.fi/URN:NBN:fi:bib:synthesis/abc",
        )
        lines = _read_lines(emit_to)
        assert len(lines) == 2
        header = lines[0].split("\t")
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

    def test_header_not_re_written_on_subsequent_appends(self, emit_to: Path) -> None:
        for bib in ("b001", "b002"):
            append_synthesis_row(
                bib_id=bib,
                field="bf:contribution/bf:agent",
                marc_source="245$c",
                synthesised_value="Tove Jansson",
                tier="B1",
                method="creator-from-245c (regex, role=unknown, marc=100)",
                confidence=0.6,
                activity_uri="http://urn.fi/URN:NBN:fi:bib:synthesis/" + bib,
            )
        lines = _read_lines(emit_to)
        assert len(lines) == 3  # 1 header + 2 data rows.


class TestDedup:
    def test_same_key_writes_one_row(self, emit_to: Path) -> None:
        for _ in range(3):
            append_synthesis_row(
                bib_id="b001",
                field="bf:contribution/bf:agent",
                marc_source="245$c",
                synthesised_value="Margaret Atwood",
                tier="B1",
                method="creator-from-245c (regex, role=author, marc=100)",
                confidence=0.8,
                activity_uri="http://urn.fi/URN:NBN:fi:bib:synthesis/dedup",
            )
        # 1 header + 1 data row (other two were deduped).
        assert len(_read_lines(emit_to)) == 2

    def test_distinct_tier_with_same_bib_field_writes_two_rows(self, emit_to: Path) -> None:
        # Hypothetical: a record where B1 contributes a primary creator
        # AND a future tier contributes a different field synthesis.
        # Today's salvage layer doesn't produce this case, but the
        # dedup key includes tier so it would.
        for tier in ("B1", "B3"):
            append_synthesis_row(
                bib_id="b001",
                field="bf:contribution/bf:agent",
                marc_source="245$c",
                synthesised_value="value",
                tier=tier,
                method=f"method-{tier}",
                confidence=0.5,
                activity_uri=f"http://urn.fi/URN:NBN:fi:bib:synthesis/{tier}",
            )
        assert len(_read_lines(emit_to)) == 3  # header + 2 distinct tiers.


class TestConfidenceFormatting:
    def test_confidence_is_4_decimal_places(self, emit_to: Path) -> None:
        append_synthesis_row(
            bib_id="b001",
            field="bf:contribution/bf:agent",
            marc_source="245$c",
            synthesised_value="value",
            tier="B1",
            method="m",
            confidence=0.7,
            activity_uri="http://urn.fi/URN:NBN:fi:bib:synthesis/c",
        )
        data_row = _read_lines(emit_to)[1].split("\t")
        confidence_column = data_row[6]
        assert confidence_column == "0.7000"

    def test_confidence_rounds_correctly(self, emit_to: Path) -> None:
        append_synthesis_row(
            bib_id="b001",
            field="bf:contribution/bf:agent",
            marc_source="245$c",
            synthesised_value="value",
            tier="B1",
            method="m",
            confidence=0.12345678,
            activity_uri="http://urn.fi/URN:NBN:fi:bib:synthesis/r",
        )
        data_row = _read_lines(emit_to)[1].split("\t")
        assert data_row[6] == "0.1235"


class TestNoEmitter:
    def test_no_op_when_emitter_inactive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Tests + direct-CLI invocations that don't bootstrap the
        observability emitter fall through silently."""
        monkeypatch.setattr(ES, "get_active_emitter", lambda: None)
        # No file should be created.
        append_synthesis_row(
            bib_id="b001",
            field="bf:contribution/bf:agent",
            marc_source="245$c",
            synthesised_value="value",
            tier="B1",
            method="m",
            confidence=0.5,
            activity_uri="http://urn.fi/URN:NBN:fi:bib:synthesis/x",
        )
        # tmp_path was never touched.
        assert not any(tmp_path.iterdir())
