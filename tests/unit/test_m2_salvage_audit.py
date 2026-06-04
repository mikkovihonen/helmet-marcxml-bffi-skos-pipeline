"""Unit tests for M2's per-fire salvage-cascade audit log.

Covers the writer module in isolation (``reset_audit_log`` +
``append_audit_row`` + telemetry plumbing). Integration coverage —
that M2's ``_try_b1_llm`` actually invokes the writer — lives in
``test_salvage.py``; this file is the contract-level coverage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bffi_pipeline.stages.m2.salvage_audit import (
    ADDED_BY,
    AUDIT_FILENAME,
    SalvageCallTelemetry,
    append_audit_row,
    audit_log_path,
    reset_audit_log,
)


@dataclass
class _StubAgent:
    name: str
    role: str = "author"


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


class TestAppendAuditRow:
    def test_fresh_call_carries_rationale_and_cache_hit_false(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        agents = [_StubAgent(name="John Smith", role="author")]
        telemetry = SalvageCallTelemetry(
            rationale="John Smith appears verbatim in the statement of responsibility.",
            cache_hit=False,
        )
        append_audit_row(
            target,
            helmet_bib_id="1000123",
            c_subfield="by John Smith",
            salvaged_agents=agents,
            telemetry=telemetry,
            now=datetime(2026, 6, 4, tzinfo=UTC),
        )
        rows = _read_rows(target)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "cg-pending-0001"
        assert row["helmet_bib_id"] == "1000123"
        assert row["c_subfield"] == "by John Smith"
        assert row["salvaged_agents"] == [{"name": "John Smith", "role": "author"}]
        assert row["rationale"].startswith("John Smith appears")
        assert row["cache_hit"] is False
        assert row["added"] == "2026-06-04"
        assert row["added_by"] == ADDED_BY

    def test_cache_hit_flagged(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        telemetry = SalvageCallTelemetry(
            rationale="cached rationale text that's substantive.",
            cache_hit=True,
        )
        append_audit_row(
            target,
            helmet_bib_id="1000456",
            c_subfield="by Jane Doe",
            salvaged_agents=[_StubAgent(name="Jane Doe")],
            telemetry=telemetry,
        )
        rows = _read_rows(target)
        assert rows[0]["cache_hit"] is True

    def test_empty_agents_still_writes_row(self, tmp_path: Path) -> None:
        """The LLM is allowed to return ``agents=[]`` (couldn't extract).
        The audit row still lands — that's a meaningful decision the
        cataloguer might want to inspect."""
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        telemetry = SalvageCallTelemetry(
            rationale="No personal name could be extracted from this 245$c.",
            cache_hit=False,
        )
        append_audit_row(
            target,
            helmet_bib_id="1000789",
            c_subfield="[material type]",
            salvaged_agents=[],
            telemetry=telemetry,
        )
        rows = _read_rows(target)
        assert len(rows) == 1
        assert rows[0]["salvaged_agents"] == []

    def test_missing_telemetry_is_ok(self, tmp_path: Path) -> None:
        """Test stubs / impls that don't expose ``_last_call``
        get nulls in the rationale + cache_hit fields rather than
        crashing the writer."""
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        append_audit_row(
            target,
            helmet_bib_id="1000999",
            c_subfield="by Anon",
            salvaged_agents=[_StubAgent(name="Anon")],
            telemetry=None,
        )
        rows = _read_rows(target)
        assert rows[0]["rationale"] is None
        assert rows[0]["cache_hit"] is None

    def test_sequential_ids(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        for i in range(3):
            append_audit_row(
                target,
                helmet_bib_id=f"100{i:04d}",
                c_subfield=f"by Author {i}",
                salvaged_agents=[_StubAgent(name=f"Author {i}")],
                telemetry=None,
            )
        rows = _read_rows(target)
        assert [r["id"] for r in rows] == [
            "cg-pending-0001",
            "cg-pending-0002",
            "cg-pending-0003",
        ]


def test_audit_log_path_convention(tmp_path: Path) -> None:
    assert audit_log_path(tmp_path) == tmp_path / AUDIT_FILENAME
