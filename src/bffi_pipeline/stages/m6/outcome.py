"""M6 judge-decision outcomes — :class:`CascadeStep`, :class:`JudgeOutcome`,
the ``STAGE_*`` provenance tags, and the synthetic-decision builders.

Two kinds of synthetic outcomes live here:

- :func:`_uncertain_decision` — the canonical fall-through decision
  when ``judge_pair``'s retry budget is exhausted; the pair lands as
  ``decision="uncertain"`` with the upstream error text preserved in
  ``rationale``.
- :func:`synthesize_auto_merge_outcome` — the no-LLM ``same_work``
  decision M5's auto-merge band produces (similarity ≥ 0.90 per
  spec § 6); embedded similarity becomes the entire signal.

P-38 Phase D: extracted from m6/runner.py. No logic change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from bffi_pipeline.stages.m6.validation import (
    STUB_PHRASES,
    WorkMatchDecision,
)

#: ``bffi-prov:stage`` values per spec § 7. Both primary and second-opinion
#: decisions are logged with these tags so post-merge SPARQL queries can
#: distinguish 32 B-only decisions from cascade-resolved ones.
STAGE_PRIMARY: Final[str] = "llm-judge-primary"
STAGE_SECOND_OPINION: Final[str] = "llm-judge-second-opinion"
#: ``bffi-prov:stage`` for pairs above the M5 auto-merge band ceiling
#: (similarity ≥ 0.90 per spec § 6). These are merged deterministically
#: without an LLM call — the embedding similarity is the entire signal.
STAGE_AUTO_MERGE: Final[str] = "auto-merge-embedding"
#: ``bffi-prov:stage`` for pairs that fell through the M6 cascade
#: because every LLM call (primary + fallback) exceeded
#: ``LLM_CALL_TIMEOUT_SECONDS`` and exhausted the retry budget.
#: Plan: ``docs/plans/completed/p-03-m6-stall-watchdog.md``.
STAGE_WATCHDOG: Final[str] = "watchdog-aborted"


@dataclass(frozen=True)
class CascadeStep:
    """One LLM call's outcome inside :func:`cascade.cascade_judge`.

    Carries everything a provenance writer needs to mint a per-call
    ``prov:Activity`` later: which model, the stage tag, the cache-hit
    flag, and the resulting decision.
    """

    stage: str  # STAGE_PRIMARY or STAGE_SECOND_OPINION
    model_name: str
    decision: WorkMatchDecision
    cache_hit: bool
    latency_seconds: float


@dataclass
class JudgeOutcome:
    """Cascade result: final decision + per-step record for provenance."""

    final: WorkMatchDecision
    steps: list[CascadeStep] = field(default_factory=list)

    @property
    def used_cascade(self) -> bool:
        """True iff the second-opinion model was invoked.

        Second-opinion fires when the primary returned ``uncertain`` or
        a ``same_work`` decision below the cascade-confidence threshold.
        """
        return any(s.stage == STAGE_SECOND_OPINION for s in self.steps)


def _uncertain_decision(reason: str) -> WorkMatchDecision:
    """Build the canonical 'fall-through' decision for unrecoverable failures.

    Confidence is pinned to 0.0 to satisfy the ``_coherent_uncertain``
    validator (which requires confidence ≤ 0.7 when decision is
    ``uncertain``); the rationale carries the original error text so a
    later operator can grep for it. Stub phrases are stripped from
    ``reason`` because the rationale validator forbids them, and ``reason``
    is also padded to ≥ 20 characters with a stable prefix.
    """
    cleaned = reason.strip() or "no error message available"
    lowered = cleaned.lower()
    for phrase in STUB_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            cleaned = re.sub(
                rf"\b{re.escape(phrase)}\b",
                "[stub phrase elided]",
                cleaned,
                flags=re.IGNORECASE,
            )
    rationale = f"Judge fell through to uncertain after retries exhausted: {cleaned}"
    return WorkMatchDecision(
        decision="uncertain",
        confidence=0.0,
        rationale=rationale,
        matching_fields=[],
        diverging_fields=[],
    )


def synthesize_auto_merge_outcome(
    row: dict[str, Any],
    *,
    embedding_model: str = "BAAI/bge-m3",
) -> JudgeOutcome:
    """Build a :class:`JudgeOutcome` for an M5 auto-merge-band pair.

    Per spec § 6, similarity ≥ 0.90 pairs merge deterministically
    without an LLM call. The synthetic outcome carries one
    ``CascadeStep`` tagged with :data:`STAGE_AUTO_MERGE` and the M5
    embedding model as ``model_name`` so provenance still reflects
    which agent made the decision (the embedding model, not an LLM).

    ``confidence`` reuses the embedding similarity directly — it's
    already on a [0, 1] scale and exceeds the ≥ 0.90 floor.
    Boundary-4 validators require ``matching_fields`` non-empty for
    ``same_work``; ``"embedding_similarity"`` is the literal signal.
    """
    similarity = float(row.get("similarity", 0.0))
    # FAISS inner-product on L2-normalised vectors can drift a hair
    # above 1.0 (~1.0000001) from float32 accumulation noise. Clamp
    # to the Pydantic ``le=1.0`` constraint on ``WorkMatchDecision.
    # confidence`` rather than rejecting the pair.
    similarity_clamped = min(max(similarity, 0.0), 1.0)
    block_a = row.get("block_a", "")
    block_b = row.get("block_b", "")
    rationale = (
        f"M5 auto-merge band: embedding similarity {similarity:.3f} ≥ 0.90 "
        f"(spec § 6 ceiling). Same blocking key ({block_a}); LLM judge "
        "skipped — same_work signal is unambiguous at this similarity. "
        f"Block_a={block_a!r} block_b={block_b!r}."
    )
    decision = WorkMatchDecision(
        decision="same_work",
        confidence=similarity_clamped,
        matching_fields=["embedding_similarity"],
        diverging_fields=[],
        rationale=rationale,
    )
    step = CascadeStep(
        stage=STAGE_AUTO_MERGE,
        model_name=embedding_model,
        decision=decision,
        cache_hit=False,
        latency_seconds=0.0,
    )
    return JudgeOutcome(final=decision, steps=[step])
