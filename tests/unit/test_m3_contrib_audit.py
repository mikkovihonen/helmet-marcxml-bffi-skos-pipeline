"""Unit tests for M3's per-fire contrib-cascade audit log.

Covers the writer module in isolation (``reset_audit_log``,
``append_audit_rows``, ``suggest_category``,
``flatten_decision_contributions``). Integration coverage that M3's
``_emit_extracted_contributions`` actually calls the writer lives in
the M3-runner integration tests; this file is the contract-level
coverage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bffi_pipeline.stages.m3.contrib_audit import (
    ADDED_BY,
    AUDIT_FILENAME,
    CATEGORY_AMBIGUOUS,
    CATEGORY_PURE_NEW_AGENT,
    CATEGORY_ROLE_CLASSIFICATION,
    CATEGORY_TRANSLITERATION,
    append_audit_rows,
    audit_log_path,
    flatten_decision_contributions,
    reset_audit_log,
    suggest_category,
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


class TestResetAuditLog:
    def test_creates_empty_file_when_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / AUDIT_FILENAME
        assert not target.exists()
        reset_audit_log(target)
        assert target.exists()
        assert target.read_text(encoding="utf-8") == ""

    def test_truncates_existing_log(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        target.write_text('{"id":"existing"}\n', encoding="utf-8")
        reset_audit_log(target)
        assert target.read_text(encoding="utf-8") == ""


class TestAppendAuditRows:
    def test_first_append_writes_seq_1(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        append_audit_rows(
            target,
            helmet_bib_id="b001",
            c_subfield="by X Y",
            existing_agents=["Existing, Agent"],
            contributions=[{"name": "X Y", "relator_code": "aut"}],
            category="role-classification",
            now=datetime(2026, 6, 3, tzinfo=UTC),
        )
        rows = _read_jsonl(target)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "cg-pending-0001"
        assert row["category"] == "role-classification"
        assert row["helmet_bib_id"] == "b001"
        assert row["c_subfield"] == "by X Y"
        assert row["existing_agents"] == ["Existing, Agent"]
        assert row["expected_contributions"] == [{"name": "X Y", "relator_code": "aut"}]
        assert row["holdout"] is False
        assert row["added"] == "2026-06-03"
        assert row["added_by"] == ADDED_BY
        assert row["notes"] == ""

    def test_subsequent_appends_increment_seq(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        for i in range(3):
            append_audit_rows(
                target,
                helmet_bib_id=f"b{i:03d}",
                c_subfield="x",
                existing_agents=[],
                contributions=[{"name": "X", "relator_code": "aut"}],
                category="pure-new-agent",
            )
        rows = _read_jsonl(target)
        assert [r["id"] for r in rows] == ["cg-pending-0001", "cg-pending-0002", "cg-pending-0003"]

    def test_truncate_resets_seq(self, tmp_path: Path) -> None:
        """``reset_audit_log`` clears the seq counter via line-count
        (it derives seq from line count, so an empty file → seq 1)."""
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        append_audit_rows(
            target,
            helmet_bib_id="b001",
            c_subfield="x",
            existing_agents=[],
            contributions=[{"name": "X", "relator_code": "aut"}],
            category="pure-new-agent",
        )
        reset_audit_log(target)
        append_audit_rows(
            target,
            helmet_bib_id="b002",
            c_subfield="y",
            existing_agents=[],
            contributions=[{"name": "Y", "relator_code": "aut"}],
            category="pure-new-agent",
        )
        rows = _read_jsonl(target)
        assert len(rows) == 1
        assert rows[0]["id"] == "cg-pending-0001"
        assert rows[0]["helmet_bib_id"] == "b002"


class TestSuggestCategory:
    def test_all_transliteration(self) -> None:
        cs = [{"name": "X", "transliteration_of": "Y"}, {"name": "A", "transliteration_of": "B"}]
        assert suggest_category(cs) == CATEGORY_TRANSLITERATION

    def test_single_new_agent_is_role_classification(self) -> None:
        cs = [{"name": "X", "relator_code": "aut"}]
        assert suggest_category(cs) == CATEGORY_ROLE_CLASSIFICATION

    def test_multi_new_agent_is_pure_new_agent(self) -> None:
        cs = [{"name": "X", "relator_code": "aut"}, {"name": "Y", "relator_code": "trl"}]
        assert suggest_category(cs) == CATEGORY_PURE_NEW_AGENT

    def test_mixed_is_ambiguous(self) -> None:
        cs = [
            {"name": "X", "relator_code": "aut"},
            {"name": "Y", "transliteration_of": "Z"},
        ]
        assert suggest_category(cs) == CATEGORY_AMBIGUOUS


@dataclass
class _StubCandidate:
    name: str
    relator_code: str | None = None
    transliteration_of: str | None = None
    role_text: str | None = None


@dataclass
class _StubDecision:
    contributions: list[_StubCandidate]


class TestFlattenDecisionContributions:
    def test_preserves_all_set_fields(self) -> None:
        decision = _StubDecision(
            contributions=[
                _StubCandidate(name="X", relator_code="aut", role_text="author"),
                _StubCandidate(name="Y", transliteration_of="Y-canonical"),
            ]
        )
        out = flatten_decision_contributions(decision)
        assert out == [
            {"name": "X", "relator_code": "aut", "role_text": "author"},
            {"name": "Y", "transliteration_of": "Y-canonical"},
        ]

    def test_drops_none_fields(self) -> None:
        decision = _StubDecision(contributions=[_StubCandidate(name="X", relator_code="aut")])
        out = flatten_decision_contributions(decision)
        # No transliteration_of / role_text keys when those are None.
        assert out == [{"name": "X", "relator_code": "aut"}]


def test_audit_log_path_convention(tmp_path: Path) -> None:
    assert audit_log_path(tmp_path) == tmp_path / AUDIT_FILENAME
