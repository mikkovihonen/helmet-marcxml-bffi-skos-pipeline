"""Unit tests for M9's per-fire picker-cascade audit log.

Covers the writer module in isolation (``reset_audit_log`` +
``append_audit_row`` + ``_classify_outcome``). Integration that
M9's ``_picker_phase_*`` actually invokes the writer lives in
the M9 pool tests; this file is the contract-level coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bffi_pipeline.stages.m9.picker import PickerDecision
from bffi_pipeline.stages.m9.picker_audit import (
    ADDED_BY,
    AUDIT_FILENAME,
    OUTCOME_FALLBACK,
    OUTCOME_LLM_PICK,
    OUTCOME_WATCHDOG,
    append_audit_row,
    audit_log_path,
    reset_audit_log,
)
from bffi_pipeline.stages.m9.schemas import (
    STAGE_FALLBACK,
    STAGE_LEXICAL,
    STAGE_LLM,
    AuthorityCandidate,
    EntityRequest,
    ReconciliationOutcome,
)


def _entity_request(*, literal: str = "Henrik Lönnrot", kind: str = "person") -> EntityRequest:
    return EntityRequest(
        work_uri="http://urn.fi/URN:NBN:fi:bib:work:abc123",
        literal=literal,
        kind=kind,  # type: ignore[arg-type]
        predicate_uri=None,
    )


def _candidates() -> list[AuthorityCandidate]:
    return [
        AuthorityCandidate(
            uri="http://urn.fi/URN:NBN:fi:au:finaf:000001",
            pref_label="Lönnrot, Henrik",
            source_vocabulary="finaf",
            lexical_similarity=0.92,
        ),
        AuthorityCandidate(
            uri="http://urn.fi/URN:NBN:fi:au:finaf:000002",
            pref_label="Lönnrot, Henric",
            source_vocabulary="finaf",
            lexical_similarity=0.85,
        ),
    ]


def _picker_decision(decision: str = "chose", uri: str | None = None) -> PickerDecision:
    return PickerDecision(
        decision=decision,  # type: ignore[arg-type]
        chosen_uri=uri,
        confidence=0.85 if decision == "chose" else 0.5,
        rationale="The first candidate matches the literal closely on name + dates.",
    )


def _outcome(
    *,
    stage: Any,
    pick: PickerDecision | None,
    chosen_uri: str | None,
    was_watchdog_aborted: bool = False,
) -> ReconciliationOutcome:
    return ReconciliationOutcome(
        request=_entity_request(),
        stage=stage,
        chosen_uri=chosen_uri,
        confidence=0.85,
        rationale="test rationale",
        candidates=_candidates(),
        needs_review=stage == STAGE_FALLBACK or was_watchdog_aborted,
        was_watchdog_aborted=was_watchdog_aborted,
        picker_decision=pick,
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


class TestAppendAuditRow:
    def test_llm_pick_outcome(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        outcome = _outcome(
            stage=STAGE_LLM,
            pick=_picker_decision("chose", "http://urn.fi/URN:NBN:fi:au:finaf:000001"),
            chosen_uri="http://urn.fi/URN:NBN:fi:au:finaf:000001",
        )
        append_audit_row(target, outcome=outcome, originating_bib_id="1000123")
        rows = _read_rows(target)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "cg-pending-0001"
        assert row["originating_bib_id"] == "1000123"
        assert row["entity_kind"] == "person"
        assert row["entity_label"] == "Henrik Lönnrot"
        assert row["outcome_stage"] == OUTCOME_LLM_PICK
        assert row["llm_decision"] == "chose"
        assert row["llm_chosen_uri"] == "http://urn.fi/URN:NBN:fi:au:finaf:000001"
        assert len(row["candidates"]) == 2
        assert row["candidates"][0]["source_vocabulary"] == "finaf"
        assert row["added_by"] == ADDED_BY

    def test_fallback_outcome(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        outcome = _outcome(
            stage=STAGE_FALLBACK,
            pick=_picker_decision("uncertain", None),
            chosen_uri="http://urn.fi/URN:NBN:fi:au:finaf:000001",  # fell back to top-lexical
        )
        append_audit_row(target, outcome=outcome, originating_bib_id="1000456")
        rows = _read_rows(target)
        assert len(rows) == 1
        assert rows[0]["outcome_stage"] == OUTCOME_FALLBACK
        assert rows[0]["llm_decision"] == "uncertain"
        assert rows[0]["llm_chosen_uri"] is None
        assert rows[0]["outcome_chosen_uri"] == "http://urn.fi/URN:NBN:fi:au:finaf:000001"

    def test_watchdog_outcome(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        outcome = _outcome(
            stage=STAGE_FALLBACK,
            pick=None,  # watchdog aborted before pick returned
            chosen_uri="http://urn.fi/URN:NBN:fi:au:finaf:000001",
            was_watchdog_aborted=True,
        )
        append_audit_row(target, outcome=outcome, originating_bib_id=None)
        rows = _read_rows(target)
        assert len(rows) == 1
        assert rows[0]["outcome_stage"] == OUTCOME_WATCHDOG
        assert rows[0]["llm_decision"] is None
        assert rows[0]["originating_bib_id"] is None

    def test_tier_0_skipped(self, tmp_path: Path) -> None:
        """Tier-0 outcomes (local exact-match) bypass the picker —
        the audit-writer must no-op on those rather than write a
        bogus row that confuses the sampler."""
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        outcome = _outcome(
            stage=STAGE_LEXICAL,
            pick=None,
            chosen_uri="http://urn.fi/URN:NBN:fi:au:finaf:000001",
        )
        append_audit_row(target, outcome=outcome, originating_bib_id="1000789")
        assert target.read_text(encoding="utf-8") == ""

    def test_sequential_ids(self, tmp_path: Path) -> None:
        target = tmp_path / AUDIT_FILENAME
        reset_audit_log(target)
        for _ in range(3):
            outcome = _outcome(
                stage=STAGE_LLM,
                pick=_picker_decision("chose", "http://example.org/uri"),
                chosen_uri="http://example.org/uri",
            )
            append_audit_row(target, outcome=outcome, originating_bib_id="b1")
        rows = _read_rows(target)
        assert [r["id"] for r in rows] == [
            "cg-pending-0001",
            "cg-pending-0002",
            "cg-pending-0003",
        ]


def test_audit_log_path_convention(tmp_path: Path) -> None:
    assert audit_log_path(tmp_path) == tmp_path / AUDIT_FILENAME
