"""M9 public dataclasses + Literal types + reconciliation constants.

The value-object surface that the rest of M9 (decisions, requests,
graph_mutate, pool, apply) and external callers / tests consume:

- ``AuthorityKind`` / ``ReconciliationStage`` Literal types.
- ``EntityRequest`` / ``AuthorityCandidate`` / ``ReconciliationOutcome``
  / ``ReconciliationSummary`` dataclasses.
- Spec § 6 thresholds (``LEXICAL_*``, ``LLM_CONFIDENCE_THRESHOLD``,
  ``DEFAULT_TOP_K``) and stage tag constants (``STAGE_*``).
- Vocabulary IDs (``VOCAB_KANTO`` etc.) + ``FINTO_BASE_URL``.
- ``PickerOrdering`` Literal + constants for the P-10 Phase E
  picker-queue reorder.
- Progress + concurrency cadences (``_M9_PROGRESS_CADENCE``,
  ``_M9_HEALTH_PROBE_CADENCE``, ``_MIN_CONCURRENCY_FOR_FACTORY``).

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from typing import Literal as LiteralType

if TYPE_CHECKING:
    from bffi_pipeline.stages.m9.picker import PickerDecision

#: P-11 Phase A progress cadence for M9. Phase 2 is LLM-picker-bound
#: at ~2-5s per entity, so a too-sparse cadence makes the dashboard
#: look frozen for 5-15 minutes between updates. 10 keeps the sidecar
#: bounded (~500 events on a 5k-entity Phase 1 walk) and gives the
#: dashboard a tick every ~30-60s of Phase 2 picker work.
_M9_PROGRESS_CADENCE: Final[int] = 10

#: P-11 Phase C re-probe cadence for M9 Phase 1. One health probe per
#: N entities surfaces mid-stage degradation in the 12-hour overnight
#: run (a single entry probe doesn't catch a Fuseki crash at hour 4).
#: Picked larger than the progress cadence so probes don't pile up
#: every status-rendering pass.
_M9_HEALTH_PROBE_CADENCE: Final[int] = 1000

#: Spec § 6 thresholds. Tightening these requires a corresponding
#: plan amendment so policy changes stay visible in review.
LEXICAL_DIRECT_THRESHOLD: Final[float] = 0.95
LEXICAL_FLOOR: Final[float] = 0.70
LLM_CONFIDENCE_THRESHOLD: Final[float] = 0.80

#: Spec-committed authority kinds. KANTO and VIAF cover persons +
#: corporate bodies; YSO/KAUNO/MUSO cover subjects + genre/form. Phase 1
#: wires creators (KANTO+VIAF). Subjects land in phase 2.
#: ``fictional_character`` is a marker kind: cataloguer-tagged
#: ``(fiktiivinen hahmo)`` / ``(fiktiv gestalt)`` qualifiers on MARC
#: 6XX person labels mean the subject is a fictional entity that
#: doesn't exist in any general authority. Reconcile short-circuits
#: with a by-design ``"reconciliation-fictional-character"`` outcome
#: — saves the Finto/VIAF call AND reframes the metric (these aren't
#: pipeline failures, they're cataloguer-marked-unbindable).
AuthorityKind = LiteralType[
    "person",  # → KANTO, VIAF as fallback
    "corporate_body",  # → KANTO, VIAF as fallback
    "subject",  # → YSO
    "genre_form",  # → KAUNO
    "music_form",  # → MUSO
    "fictional_character",  # → skip both tiers; no authority carries fictional persons
]

#: Source-vocabulary keys logged onto the provenance Activity. Also
#: consumed as Finto's ``vocab`` query parameter for the four Finto
#: clients. KANTO is identified as ``finaf`` ("Finnish Authority File")
#: in Finto's API even though the human-facing name is still "KANTO";
#: using ``vocab=kanto`` returns HTTP 500.
VOCAB_KANTO: Final[str] = "finaf"
VOCAB_YSO: Final[str] = "yso"
VOCAB_KAUNO: Final[str] = "kauno"
VOCAB_MUSO: Final[str] = "muso"
VOCAB_VIAF: Final[str] = "viaf"

ReconciliationStage = LiteralType[
    "reconciliation-local",
    "reconciliation-lexical",
    "reconciliation-llm",
    "reconciliation-fallback",
    "reconciliation-no-candidate",
    "reconciliation-fictional-character",
]

#: Stage tags, kept aligned with spec § 8. These are the
#: same Literal type as :data:`ReconciliationStage`; declared via
#: forward strings so mypy treats the constants as the narrowed Literal,
#: not just ``str``.
STAGE_LOCAL: Final[ReconciliationStage] = "reconciliation-local"
STAGE_LEXICAL: Final[ReconciliationStage] = "reconciliation-lexical"
STAGE_LLM: Final[ReconciliationStage] = "reconciliation-llm"
STAGE_FALLBACK: Final[ReconciliationStage] = "reconciliation-fallback"
STAGE_NO_CANDIDATE: Final[ReconciliationStage] = "reconciliation-no-candidate"
STAGE_FICTIONAL: Final[ReconciliationStage] = "reconciliation-fictional-character"

#: Default top-k pulled from the authority for each input literal.
DEFAULT_TOP_K: Final[int] = 10

#: Finto Skosmos REST API endpoint. Free public service; no API key.
FINTO_BASE_URL: Final[str] = "https://api.finto.fi/rest/v1"

#: Provenance ``bffi-prov:stage`` literal for outcomes where the picker
#: exceeded its per-field budget. Mirrors M6's ``STAGE_WATCHDOG`` literal
#: in ``stages/judge.py`` so cataloguers see one consistent marker
#: across stages.
STAGE_WATCHDOG_ABORTED: Final[str] = "watchdog-aborted"

#: Concurrency value at or above which ``picker_factory`` becomes
#: mandatory (each worker thread builds its own LLMPicker).
_MIN_CONCURRENCY_FOR_FACTORY: Final[int] = 2

#: Valid values for ``BFFI_M9_PICKER_ORDERING`` / ``apply_reconciliation``'s
#: ``picker_ordering`` parameter. ``submission`` (default) preserves the
#: walk order ``_collect_requests`` yielded; ``prefix-cache`` sorts by
#: prompt-prefix-similarity. Phase E's 2026-05-13 A/B bench (see
#: ``docs/performance/2026-05-13-5k-m2-max-phase-e.md``) showed
#: ``prefix-cache`` was a +5 % regression on the 5 k sample, so default
#: stays on ``submission`` until a re-bench on a more-homogeneous corpus
#: shows otherwise.
PickerOrdering = LiteralType["prefix-cache", "submission"]

PICKER_ORDERING_PREFIX_CACHE: Final[PickerOrdering] = "prefix-cache"
PICKER_ORDERING_SUBMISSION: Final[PickerOrdering] = "submission"


def _finto_search_query(literal: str) -> str:
    """Build the Finto ``query`` parameter from a cataloguer literal.

    Finto exact-matches on prefLabel by default; we want prefix-match.
    Appends ``*`` unless the caller already supplied one. Trailing MARC
    punctuation (``", "`` after a name) doesn't break the wildcard —
    Finto treats it as part of the prefix and still finds entries whose
    label is the exact prefix.
    """
    return literal if literal.endswith("*") else f"{literal}*"


@dataclass(frozen=True)
class EntityRequest:
    """One reconciliation input drawn from a canonical Work.

    ``predicate_uri`` is set for subject/genre/music requests so the
    dispatcher knows whether to bind the chosen authority back as
    ``bffi:subject`` or ``bffi:genreForm`` — the cataloguer's MARC tag
    chose the predicate at M2 conversion time, and reconciliation must
    preserve it. Creator requests leave it ``None``; the creator linker
    rewrites ``bffi:contribution`` instead.
    """

    work_uri: str
    literal: str
    kind: AuthorityKind
    predicate_uri: str | None = None


@dataclass(frozen=True)
class AuthorityCandidate:
    """One candidate URI returned by an authority lookup."""

    uri: str
    pref_label: str
    source_vocabulary: str
    lexical_similarity: float


# The PickerDecision Pydantic class lives in :mod:`picker` so the
# schemas module stays import-light (Pydantic is heavy). The
# :class:`ReconciliationOutcome` below references it via TYPE_CHECKING
# (see import block at top of file) to keep schemas → picker decoupled
# at module-load time.


@dataclass(frozen=True)
class ReconciliationOutcome:
    """Final outcome for one ``EntityRequest``.

    ``was_watchdog_aborted`` is the M9 analogue of M6's
    ``STAGE_WATCHDOG``: the picker call exceeded
    ``LLM_M9_FIELD_TIMEOUT_SECONDS`` so the outcome was built from
    tier-3 fallback (highest-lexical + needs-review). The flag drives
    the ``bffi-prov:stage = "watchdog-aborted"`` literal in the
    provenance graph (overrides ``stage`` for provenance purposes);
    the canonical-graph mutation stays the same as a normal fallback.

    ``cached_activity_uuid`` is populated by the P-10 Phase B
    picker-cache lookup: when set, the freshly-minted provenance
    Activity for this outcome carries ``prov:wasInfluencedBy
    <cached_activity_uuid>`` so the audit trail distinguishes "fresh
    LLM verdict" from "reused cached verdict". ``None`` for cache
    misses and for non-picker outcomes (tier-0 / tier-1 / no-candidate
    / fictional / watchdog-aborted).
    """

    request: EntityRequest
    stage: ReconciliationStage
    chosen_uri: str | None
    confidence: float
    rationale: str
    candidates: list[AuthorityCandidate]
    needs_review: bool
    was_watchdog_aborted: bool = False
    cached_activity_uuid: str | None = None
    # P-10 Phase B.1: the raw PickerDecision that produced this outcome.
    # Populated by tier-2 dispatch paths (STAGE_LLM + STAGE_FALLBACK);
    # ``None`` for tier-0 / tier-1 / no-candidate / fictional / watchdog-
    # aborted. Phase B's write-back logic stores this verbatim so the
    # warm-run lookup can replay the same _decide_with_pick(pick=…)
    # logic and reproduce the cold-run outcome byte-stably — including
    # for low-confidence ("uncertain" / "chose with conf < 0.80")
    # decisions that map to STAGE_FALLBACK. The model's per-call
    # non-determinism near the 0.80 threshold otherwise causes
    # cold/warm tier flips (audit script
    # ``scripts/p10-phase-b-cold-warm-audit.py`` surfaces these).
    picker_decision: PickerDecision | None = None

    @property
    def is_success(self) -> bool:
        """True for any outcome that bound an authority URI (incl. fallback)."""
        return self.chosen_uri is not None


@dataclass
class ReconciliationSummary:
    """Per-tier counts for a full ``apply_reconciliation`` pass."""

    local: int = 0
    lexical: int = 0
    llm_pick: int = 0
    fallback: int = 0
    no_candidate: int = 0
    fictional: int = 0
    watchdog_aborted: int = 0
    total: int = 0

    def render(self) -> str:
        """Format the reconciliation summary as paste-ready text for the reconcile CLI."""
        return "\n".join(
            (
                "M9 reconciliation complete",
                f"  total entities:                          {self.total:,}",
                f"  reconciliation-local:                    {self.local:,}",
                f"  reconciliation-lexical:                  {self.lexical:,}",
                f"  reconciliation-llm:                      {self.llm_pick:,}",
                f"  reconciliation-fallback:                 {self.fallback:,}",
                f"  reconciliation-no-candidate:             {self.no_candidate:,}",
                f"  reconciliation-fictional-character:      {self.fictional:,}",
                f"  watchdog-aborted (subset of fallback):   {self.watchdog_aborted:,}",
            )
        )


#: All authority kinds the orchestrator can walk for. Mirrors the
#: ``AuthorityKind`` Literal; kept as a runtime ``frozenset`` so callers
#: can build subsets ergonomically (``{"person", "corporate_body"}``).
ALL_AUTHORITY_KINDS: Final[frozenset[AuthorityKind]] = frozenset(
    {
        "person",
        "corporate_body",
        "subject",
        "genre_form",
        "music_form",
        "fictional_character",
    }
)
_CREATOR_KINDS: Final[frozenset[AuthorityKind]] = frozenset({"person", "corporate_body"})
#: ``fictional_character`` walks alongside the subject kinds because
#: the marker comes from a MARC 6XX subject target whose label
#: carries the ``(fiktiivinen hahmo)`` qualifier; without it included
#: here, ``--kinds subjects`` would drop the marker before
#: ``reconcile_one`` could emit the by-design outcome.
_SUBJECT_KINDS: Final[frozenset[AuthorityKind]] = frozenset(
    {"subject", "genre_form", "music_form", "fictional_character"}
)
