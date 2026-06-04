"""Unit tests for M3's per-fire title-lang audit log."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bffi_pipeline.stages.m3.title_lang_audit import (
    ADDED_BY,
    AUDIT_FILENAME,
    TitleLangCallTelemetry,
    append_audit_row,
    audit_log_path,
    reset_audit_log,
    telemetry_from_decision,
)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestResetAuditLog:
    def test_creates_empty_file_when_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / AUDIT_FILENAME
        reset_audit_log(target)
        assert target.exists()
        assert target.read_text(encoding="utf-8") == ""

    def test_truncates_existing(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        target.write_text('{"id":"existing"}\n', encoding="utf-8")
        reset_audit_log(target)
        assert target.read_text(encoding="utf-8") == ""


class TestTelemetryFromDecision:
    def test_constructs_immutable_telemetry(self) -> None:
        t = telemetry_from_decision(
            title="Tšarka = venäläinen tšarkka = russkaja tšarka",
            candidates=frozenset({"fi", "ru"}),
            segments=[
                ("Tšarka", "ru"),
                ("venäläinen tšarkka", "fi"),
                ("russkaja tšarka", "ru"),
            ],
            rationale="Three-segment parallel title with Finnish + Russian transliterations.",
        )
        assert isinstance(t, TitleLangCallTelemetry)
        assert t.candidates == ("fi", "ru")
        assert len(t.segments) == 3
        assert t.segments[0] == ("Tšarka", "ru")


class TestAppendAuditRow:
    def test_writes_full_row(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        telemetry = telemetry_from_decision(
            title="Foo = bar",
            candidates=["fi", "en"],
            segments=[("Foo", "fi"), ("bar", "en")],
            rationale="Two-segment Finnish-English parallel title.",
        )
        append_audit_row(
            target,
            helmet_bib_id="1000123",
            work_uri="http://urn.fi/URN:NBN:fi:bib:work:abc",
            telemetry=telemetry,
            now=datetime(2026, 6, 4, tzinfo=UTC),
        )
        rows = _read_rows(target)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "cg-pending-0001"
        assert row["helmet_bib_id"] == "1000123"
        assert row["work_uri"] == "http://urn.fi/URN:NBN:fi:bib:work:abc"
        assert row["title"] == "Foo = bar"
        assert row["candidates"] == ["en", "fi"]
        assert row["llm_segments"] == [
            {"text": "Foo", "lang": "fi"},
            {"text": "bar", "lang": "en"},
        ]
        assert row["added"] == "2026-06-04"
        assert row["added_by"] == ADDED_BY

    def test_null_helmet_bib_id_ok(self, tmp_path: Path) -> None:
        """When the bib id lookup fails, the row still lands —
        the HTML viewer's MARC sidecar gracefully degrades to a
        'no MARC available' stub for that row."""
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        telemetry = telemetry_from_decision(
            title="X = Y",
            candidates=["fi", "en"],
            segments=[("X", "fi"), ("Y", "en")],
            rationale="Test rationale exceeding the twenty-char minimum easily.",
        )
        append_audit_row(
            target,
            helmet_bib_id=None,
            work_uri="http://example.org/work/missing",
            telemetry=telemetry,
        )
        rows = _read_rows(target)
        assert rows[0]["helmet_bib_id"] is None

    def test_sequential_ids(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        for i in range(3):
            telemetry = telemetry_from_decision(
                title=f"Title {i}",
                candidates=["fi", "en"],
                segments=[(f"Title {i}", "en")],
                rationale="Test rationale longer than twenty characters easily.",
            )
            append_audit_row(
                target,
                helmet_bib_id=f"100{i:04d}",
                work_uri=f"http://example.org/work/{i}",
                telemetry=telemetry,
            )
        rows = _read_rows(target)
        assert [r["id"] for r in rows] == [
            "cg-pending-0001",
            "cg-pending-0002",
            "cg-pending-0003",
        ]


def test_audit_log_path_convention(tmp_path: Path) -> None:
    assert audit_log_path(tmp_path) == tmp_path / AUDIT_FILENAME
