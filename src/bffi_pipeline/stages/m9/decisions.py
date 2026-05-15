"""M9 four-tier decision logic — :func:`decide_reconciliation` and friends.

The spec § 6 ladder:

- :func:`_decide_before_picker` short-circuits at tier-1 (single
  candidate ≥ 0.95 lexical) or tier-0-style no-candidate /
  below-floor. Returns ``(outcome, sorted_candidates)``; the caller
  invokes :func:`_decide_with_pick` when ``outcome is None``.
- :func:`_decide_with_pick` applies the LLM verdict, including the
  three P-16 fallback knobs (per-vocab floor, global floor,
  hard-disable).
- :func:`_watchdog_aborted_outcome` builds the tier-3-shaped outcome
  for picker budget timeouts.
- :func:`_local_outcome` / :func:`_fictional_outcome` are tier-0 +
  fictional-character outcome factories used by both the
  ``reconcile_one`` legacy path and the concurrent Phase 1.
- :func:`decide_reconciliation` is the legacy single-threaded ladder
  the ``reconcile_one`` path uses.
- :func:`_format_m9_details` renders the cataloguer-facing free-text
  details column for the unified target-review TSV.

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from bffi_pipeline.stages.m9.picker import LLMPicker, PickerDecision
from bffi_pipeline.stages.m9.schemas import (
    LEXICAL_DIRECT_THRESHOLD,
    LEXICAL_FLOOR,
    LLM_CONFIDENCE_THRESHOLD,
    STAGE_FALLBACK,
    STAGE_FICTIONAL,
    STAGE_LEXICAL,
    STAGE_LLM,
    STAGE_LOCAL,
    STAGE_NO_CANDIDATE,
    AuthorityCandidate,
    EntityRequest,
    ReconciliationOutcome,
)

if TYPE_CHECKING:
    from bffi_pipeline.stages.m9.local_concept_resolver import LocalConceptHit

#: Max candidates rendered in the cataloguer-facing details column.
#: Caps row length so the TSV stays scannable in a spreadsheet; the
#: cataloguer can drill into the full candidate list via the
#: per-record provenance graph if more context is needed.
_DETAILS_CANDIDATE_LIMIT: Final[int] = 5


def _format_m9_details(outcome: ReconciliationOutcome) -> str:
    """Build the cataloguer-facing free-text context for one M9 outcome.

    Surfaces the literal we tried to reconcile, the top candidates that
    were considered (URI + prefLabel + source vocab + lexical sim), and
    the rationale that led to the no-candidate / fallback / fictional
    verdict. Cataloguers use this to judge whether the pipeline got
    the call right.
    """
    parts = [f"literal={outcome.request.literal!r} ({outcome.request.kind})"]
    if outcome.candidates:
        top = outcome.candidates[:_DETAILS_CANDIDATE_LIMIT]
        rendered = "; ".join(
            f"{c.uri} {c.pref_label!r} ({c.source_vocabulary}, sim={c.lexical_similarity:.2f})"
            for c in top
        )
        extra = len(outcome.candidates) - _DETAILS_CANDIDATE_LIMIT
        suffix = f" (+{extra} more)" if extra > 0 else ""
        parts.append(f"candidates: {rendered}{suffix}")
    else:
        parts.append("candidates: (none returned by the authority client)")
    rationale = (outcome.rationale or "").strip()
    if rationale:
        parts.append(f"rationale: {rationale}")
    return " | ".join(parts)


def _decide_before_picker(
    *,
    request: EntityRequest,
    candidates: list[AuthorityCandidate],
) -> tuple[ReconciliationOutcome | None, list[AuthorityCandidate]]:
    """Tier-0/1 short-circuits without touching the picker.

    Returns ``(outcome, sorted_candidates)``. When ``outcome`` is not
    ``None``, the decision was made by the deterministic tiers
    (no-candidate / lexical-direct) and the picker is unnecessary.
    When ``outcome`` is ``None``, the caller must call
    :func:`_decide_with_pick` with a ``PickerDecision`` (or build a
    watchdog-aborted fallback) to finish the decision.
    """
    if not candidates:
        return (
            ReconciliationOutcome(
                request=request,
                stage=STAGE_NO_CANDIDATE,
                chosen_uri=None,
                confidence=0.0,
                rationale="No candidates returned by the authority client.",
                candidates=[],
                needs_review=False,
            ),
            [],
        )

    sorted_candidates = sorted(candidates, key=lambda c: c.lexical_similarity, reverse=True)
    top = sorted_candidates[0]

    if top.lexical_similarity < LEXICAL_FLOOR:
        return (
            ReconciliationOutcome(
                request=request,
                stage=STAGE_NO_CANDIDATE,
                chosen_uri=None,
                confidence=top.lexical_similarity,
                rationale=(
                    f"Top lexical similarity {top.lexical_similarity:.3f} below "
                    f"the {LEXICAL_FLOOR:.2f} floor; left unreconciled."
                ),
                candidates=sorted_candidates,
                needs_review=False,
            ),
            sorted_candidates,
        )

    high_similarity = [
        c for c in sorted_candidates if c.lexical_similarity >= LEXICAL_DIRECT_THRESHOLD
    ]
    if len(high_similarity) == 1:
        winner = high_similarity[0]
        return (
            ReconciliationOutcome(
                request=request,
                stage=STAGE_LEXICAL,
                chosen_uri=winner.uri,
                confidence=winner.lexical_similarity,
                rationale=(
                    f"Single candidate cleared the {LEXICAL_DIRECT_THRESHOLD:.2f} "
                    f"lexical floor: {winner.pref_label!r} "
                    f"({winner.lexical_similarity:.3f})."
                ),
                candidates=sorted_candidates,
                needs_review=False,
            ),
            sorted_candidates,
        )

    return None, sorted_candidates


def _decide_with_pick(
    *,
    request: EntityRequest,
    sorted_candidates: list[AuthorityCandidate],
    pick: PickerDecision,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
) -> ReconciliationOutcome:
    """Apply tier-2 / tier-3 given an already-computed picker decision.

    P-10 Phase B.1: the original ``pick`` rides on the outcome's
    ``picker_decision`` field so the cache-write site can persist
    *every* picker call's verdict (not only the STAGE_LLM successes).
    Warm-cache replay then byte-stably reproduces the cold-run
    classification, including for low-confidence picks that map to
    STAGE_FALLBACK.

    P-16: ``lexical_fallback_floor``, ``lexical_fallback_floor_per_vocab``
    and ``disable_fallback`` gate the tier-3 fallback path. Defaults
    preserve pre-P-16 behaviour (floor = ``LEXICAL_FLOOR``, no per-vocab
    overrides, fallback enabled).
    """
    if (
        pick.decision == "chose"
        and pick.chosen_uri is not None
        and pick.confidence >= LLM_CONFIDENCE_THRESHOLD
    ):
        return ReconciliationOutcome(
            request=request,
            stage=STAGE_LLM,
            chosen_uri=pick.chosen_uri,
            confidence=pick.confidence,
            rationale=pick.rationale,
            candidates=sorted_candidates,
            needs_review=False,
            picker_decision=pick,
        )

    top = sorted_candidates[0]
    # P-16 Knob A + B: the fallback floor is per-vocabulary-overridable.
    # Vocabs not listed in ``lexical_fallback_floor_per_vocab`` fall
    # through to the global floor.
    per_vocab = lexical_fallback_floor_per_vocab or {}
    effective_floor = per_vocab.get(top.source_vocabulary, lexical_fallback_floor)
    # P-16 Knob C: hard-disable the tier-3 fallback path. Knob A/B are
    # subsumed when Knob C is on — we never bind. The order matters for
    # the rationale string.
    if disable_fallback:
        return ReconciliationOutcome(
            request=request,
            stage=STAGE_NO_CANDIDATE,
            chosen_uri=None,
            confidence=top.lexical_similarity,
            rationale=(
                f"LLM picker {pick.decision!r} (confidence "
                f"{pick.confidence:.2f}); tier-3 fallback hard-disabled via "
                f"BFFI_M9_DISABLE_FALLBACK. Top lexical was "
                f"{top.pref_label!r} ({top.lexical_similarity:.3f}). "
                f"Left unreconciled."
            ),
            candidates=sorted_candidates,
            needs_review=False,
            picker_decision=pick,
        )
    if top.lexical_similarity < effective_floor:
        return ReconciliationOutcome(
            request=request,
            stage=STAGE_NO_CANDIDATE,
            chosen_uri=None,
            confidence=top.lexical_similarity,
            rationale=(
                f"LLM picker {pick.decision!r} (confidence "
                f"{pick.confidence:.2f}); top lexical "
                f"{top.lexical_similarity:.3f} below the "
                f"{effective_floor:.2f} fallback floor for "
                f"{top.source_vocabulary!r}. Left unreconciled."
            ),
            candidates=sorted_candidates,
            needs_review=False,
            picker_decision=pick,
        )

    return ReconciliationOutcome(
        request=request,
        stage=STAGE_FALLBACK,
        chosen_uri=top.uri,
        confidence=top.lexical_similarity,
        rationale=(
            f"LLM picker {pick.decision!r} (confidence {pick.confidence:.2f}); "
            f"falling back to highest-lexical candidate {top.pref_label!r} "
            f"({top.lexical_similarity:.3f}). Flagged needs-review."
        ),
        candidates=sorted_candidates,
        needs_review=True,
        picker_decision=pick,
    )


def _watchdog_aborted_outcome(
    *,
    request: EntityRequest,
    sorted_candidates: list[AuthorityCandidate],
    elapsed_seconds: float,
    budget_seconds: int,
) -> ReconciliationOutcome:
    """Build a tier-3-shaped outcome for a picker call that exceeded its budget.

    The canonical-graph mutation is identical to a normal fallback
    (highest-lexical + needs-review), so cataloguers see the same
    Skosmos UX. The ``was_watchdog_aborted`` flag drives the
    ``bffi-prov:stage = "watchdog-aborted"`` literal in the provenance
    graph so the audit trail distinguishes "LLM said uncertain" from
    "LLM never answered in time".
    """
    top = sorted_candidates[0]
    return ReconciliationOutcome(
        request=request,
        stage=STAGE_FALLBACK,
        chosen_uri=top.uri,
        confidence=top.lexical_similarity,
        rationale=(
            f"Picker exceeded the {budget_seconds}s per-field budget "
            f"(elapsed {elapsed_seconds:.1f}s); falling back to highest-lexical "
            f"candidate {top.pref_label!r} ({top.lexical_similarity:.3f}). "
            f"Flagged needs-review and bffi-prov:stage=watchdog-aborted."
        ),
        candidates=sorted_candidates,
        needs_review=True,
        was_watchdog_aborted=True,
    )


def decide_reconciliation(
    *,
    request: EntityRequest,
    candidates: list[AuthorityCandidate],
    picker: LLMPicker,
) -> ReconciliationOutcome:
    """Apply the four-tier logic from spec § 6.

    Kept for backwards compatibility with the ``reconcile_one``
    single-threaded path and existing unit tests. The P-10 Phase A
    concurrent orchestrator uses :func:`_decide_before_picker` and
    :func:`_decide_with_pick` directly so the picker dispatch can be
    parallelised and budget-wrapped.
    """
    outcome, sorted_candidates = _decide_before_picker(request=request, candidates=candidates)
    if outcome is not None:
        return outcome
    pick = picker.pick(request=request, candidates=sorted_candidates)
    return _decide_with_pick(request=request, sorted_candidates=sorted_candidates, pick=pick)


def _local_outcome(request: EntityRequest, hit: LocalConceptHit) -> ReconciliationOutcome:
    """Build a tier-0 outcome for a local-graph match.

    Synthesises a single :class:`AuthorityCandidate` with similarity
    1.0 so downstream provenance + summary code can treat tier-0 hits
    uniformly with the other tiers.

    P-10 Phase C: when the bind required the diacritic-fold + strip
    (``hit.is_fuzzy_match == True``), the outcome sets
    ``needs_review`` so cataloguers see the imperfect match in
    Skosmos. Exact-string matches keep ``needs_review=False`` and the
    binding is treated as auto-merged.
    """
    candidate = AuthorityCandidate(
        uri=hit.uri,
        pref_label=hit.pref_label,
        source_vocabulary=hit.source_vocabulary,
        lexical_similarity=1.0,
    )
    if hit.is_fuzzy_match:
        rationale = (
            f"Folded-label match in local {hit.source_vocabulary} graph: "
            f"cataloguer literal {request.literal!r} aligned with "
            f"{hit.pref_label!r} after diacritic-fold + decoration strip "
            f"(no Finto API call). Flagged needs-review for cataloguer audit."
        )
    else:
        rationale = (
            f"Exact prefLabel match in local {hit.source_vocabulary} graph: "
            f"{hit.pref_label!r} (no Finto API call)."
        )
    return ReconciliationOutcome(
        request=request,
        stage=STAGE_LOCAL,
        chosen_uri=hit.uri,
        confidence=1.0,
        rationale=rationale,
        candidates=[candidate],
        needs_review=hit.is_fuzzy_match,
    )


def _fictional_outcome(request: EntityRequest) -> ReconciliationOutcome:
    """Build a by-design no-bind outcome for a fictional-character label.

    No candidates, no chosen URI, ``needs_review=False`` — cataloguer
    already classified this entity as fictional with the parenthetical
    qualifier; downstream review queues should NOT show these. The
    distinct ``STAGE_FICTIONAL`` stage tag separates them from genuine
    ``reconciliation-no-candidate`` failures in summary + provenance.
    """
    return ReconciliationOutcome(
        request=request,
        stage=STAGE_FICTIONAL,
        chosen_uri=None,
        confidence=0.0,
        rationale=(
            f"Fictional-character label (cataloguer-tagged): {request.literal!r}. "
            "No general authority carries fictional persons; skipped by design."
        ),
        candidates=[],
        needs_review=False,
    )
