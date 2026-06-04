"""M9 picker-cascade audit log.

Per-run sidecar that records every LLM picker fire as a candidate-
shaped JSONL row, so downstream tooling (the ``cataloguer-bundle``
pipeline stage, the future ``review-bundle-build --from-run``) can
build a review queue without re-running the LLM cascade against
the canonical graph.

Each row carries the full picker call context: the entity literal
+ kind + originating work URI, the candidate list the LLM saw,
the raw :class:`PickerDecision`, and the final
:class:`ReconciliationOutcome` (so the cataloguer sees whether
the outcome was an LLM pick, a fallback, or a watchdog-aborted
fallback). The bundle handler in
:mod:`bffi_pipeline.eval.review_bundle.stages.picker` consumes
this file.

The log is truncated at M9 stage start (:func:`reset_audit_log`)
and appended to per fire (:func:`append_audit_row`). Re-runs of
M9 produce a fresh audit; partial runs leave a partial log, which
is what we want — the bundle reflects exactly what was processed.

Mirrors :mod:`bffi_pipeline.stages.m3.contrib_audit` in shape, so
the cataloguer-bundle dispatcher's auto-discovery loop works
identically for picker and contrib.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from bffi_pipeline.stages.m9.schemas import (
    STAGE_FALLBACK,
    STAGE_LLM,
    ReconciliationOutcome,
)

#: Filename M9 writes the audit log under in the run directory. Sits
#: next to ``contrib-candidates.jsonl`` + ``judge-decisions.jsonl``
#: so the cataloguer-bundle stage can locate it by convention.
AUDIT_FILENAME: Final[str] = "picker-decisions.jsonl"

#: Value stamped on every audit row's ``added_by`` field. Lets the
#: cataloguer + the import path tell picker-mined candidates apart
#: from contrib/judge ones.
ADDED_BY: Final[str] = "m9-picker"

#: Outcome category labels — surface what the cataloguer sees.
#: Mirror the ``ReconciliationSummary`` end-counter names so the
#: stratified sampler keys can be diffed against the run-event log.
OUTCOME_LLM_PICK: Final[str] = "llm_pick"
OUTCOME_FALLBACK: Final[str] = "fallback"
OUTCOME_WATCHDOG: Final[str] = "watchdog_aborted"


def audit_log_path(run_dir: Path) -> Path:
    """Convention for where M9 writes the picker audit log."""
    return run_dir / AUDIT_FILENAME


def reset_audit_log(path: Path) -> None:
    """Truncate the audit log (or create the empty file).

    Called at M9 stage start so re-runs produce a coherent log
    instead of appending duplicates. The parent directory is
    created if needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _classify_outcome(outcome: ReconciliationOutcome) -> str | None:
    """Map a ReconciliationOutcome to one of the three picker-fired
    outcome categories, or ``None`` for tier-0/1 outcomes that
    bypassed the picker entirely (we don't audit those)."""
    if outcome.was_watchdog_aborted:
        return OUTCOME_WATCHDOG
    if outcome.stage == STAGE_LLM:
        return OUTCOME_LLM_PICK
    if outcome.stage == STAGE_FALLBACK:
        return OUTCOME_FALLBACK
    return None


def append_audit_row(
    path: Path,
    *,
    outcome: ReconciliationOutcome,
    originating_bib_id: str | None,
    now: datetime | None = None,
) -> None:
    """Append one audit row for a single picker fire.

    ``outcome`` is the :class:`ReconciliationOutcome` produced by
    ``_decide_with_pick`` (or ``_watchdog_aborted_outcome``). The
    function inspects ``outcome.stage`` + ``outcome.was_watchdog_aborted``
    to classify the row's ``outcome_stage`` field — and skips
    silently when the outcome resolved at tier-0 / tier-1 (no
    picker call, nothing to audit).

    ``originating_bib_id`` is looked up by the caller from the
    apply-level ``canonical_bib_ids`` map; pass ``None`` when the
    work URI isn't in the map (rare — M8 dropped it). The row
    still lands; the bundle's MARC viewer renders a "no MARC
    available" stub for that side.
    """
    category = _classify_outcome(outcome)
    if category is None:
        return  # tier-0 / tier-1 / no-candidate / fictional — picker didn't fire.

    seq = _next_seq(path)
    request = outcome.request
    candidates = [
        {
            "uri": c.uri,
            "pref_label": c.pref_label,
            "source_vocabulary": c.source_vocabulary,
            "lexical_similarity": c.lexical_similarity,
        }
        for c in outcome.candidates
    ]
    pick = outcome.picker_decision
    row: dict[str, Any] = {
        "id": f"cg-pending-{seq:04d}",
        "originating_bib_id": originating_bib_id,
        "entity_kind": request.kind,
        "entity_label": request.literal,
        "originating_work_uri": request.work_uri,
        "predicate_uri": request.predicate_uri,
        "candidates": candidates,
        "llm_decision": pick.decision if pick is not None else None,
        "llm_chosen_uri": pick.chosen_uri if pick is not None else None,
        "llm_confidence": pick.confidence if pick is not None else None,
        "llm_rationale": pick.rationale if pick is not None else None,
        "outcome_stage": category,
        "outcome_chosen_uri": outcome.chosen_uri,
        "outcome_confidence": outcome.confidence,
        "outcome_rationale": outcome.rationale,
        "needs_review": outcome.needs_review,
        "added": (now or datetime.now(UTC)).date().isoformat(),
        "added_by": ADDED_BY,
        "notes": "",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False))
        fh.write("\n")


def _next_seq(path: Path) -> int:
    """Count of non-blank lines already in ``path``, +1."""
    if not path.exists():
        return 1
    n = 0
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            if raw.strip():
                n += 1
    return n + 1


__all__ = [
    "ADDED_BY",
    "AUDIT_FILENAME",
    "OUTCOME_FALLBACK",
    "OUTCOME_LLM_PICK",
    "OUTCOME_WATCHDOG",
    "append_audit_row",
    "audit_log_path",
    "reset_audit_log",
]
