"""M6 Pydantic verdict schemas + Boundary-4 semantic validators.

Two schema shapes live here:

- :class:`WorkRecord` — the input record per side of a candidate pair.
- :class:`WorkMatchDecision` — the strict full-rationale judgment shape
  the LangChain layer parses LLM responses into. Boundary-4 validators
  enforce decision/confidence/rationale coherence (spec § 7).
- :class:`WorkMatchDecisionFast` — fast-mode variant; rationale can be
  null for confident clear-cut decisions. ``to_strict()`` rehydrates
  it into a :class:`WorkMatchDecision` (the cache + provenance writer +
  JSONL serialiser all key on the strict shape).

P-38 Phase B: extracted from m6/runner.py to keep the runner focused
on the cascade orchestration. No logic change — moves only.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Stub phrases the rationale must NOT contain. Stored already lower-cased.
STUB_PHRASES: Final[tuple[str, ...]] = (
    "i don't know",
    "unable to determine",
    "n/a",
    "not sure",
)

#: Maximum confidence allowed when the model returns ``decision="uncertain"``.
#: Anything higher is incoherent with the decision label and triggers Boundary-4.
UNCERTAIN_MAX_CONFIDENCE: Final[float] = 0.7

#: Minimum rationale length, in characters. Stops one-word answers and
#: punctuation-only payloads from passing as substantive reasoning.
MIN_RATIONALE_CHARS: Final[int] = 20

#: Confidence cutoff below which fast-mode requires a rationale (and below
#: which the primary judge cascade-escalates to the fallback model).
FALLBACK_CONFIDENCE_THRESHOLD: Final[float] = 0.85


class WorkRecord(BaseModel):
    """One side of a candidate pair, populated from the BFFI Work + BIBFRAME agent."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    creator: str | None = None
    creator_uri: str | None = None
    preferred_title: str | None = None
    variant_titles: list[str] = Field(default_factory=list)
    original_language: str | None = None
    expression_language: str | None = None
    content_type: str | None = None
    date_of_origin: str | None = None
    publication_year: str | None = None
    notes: list[str] = Field(default_factory=list)


class WorkMatchDecision(BaseModel):
    """Structured judgment. Per spec § 7 the model must fill exactly this schema."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["same_work", "different_work", "uncertain"]
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0-1.0. Use <0.7 when uncertain; reserve >0.9 for clear cases.",
    )
    rationale: str = Field(
        min_length=20,
        description=(
            "2-4 sentences citing specific field values from BOTH records. "
            "Do not introduce facts not present in the inputs."
        ),
    )
    matching_fields: list[str] = Field(default_factory=list)
    diverging_fields: list[str] = Field(default_factory=list)

    # --- Boundary 4 semantic validators (spec § 10 + § 7) -----------------

    @model_validator(mode="after")
    def _coherent_uncertain(self) -> WorkMatchDecision:
        if self.decision == "uncertain" and self.confidence > UNCERTAIN_MAX_CONFIDENCE:
            raise ValueError(
                f"decision='uncertain' is incoherent with confidence > {UNCERTAIN_MAX_CONFIDENCE}"
            )
        return self

    @model_validator(mode="after")
    def _same_work_needs_evidence(self) -> WorkMatchDecision:
        if self.decision == "same_work" and not self.matching_fields:
            raise ValueError("decision='same_work' requires at least one matching_field")
        return self

    @model_validator(mode="after")
    def _rationale_is_substantive(self) -> WorkMatchDecision:
        text = self.rationale.strip()
        if len(text) < MIN_RATIONALE_CHARS:
            raise ValueError(f"rationale shorter than {MIN_RATIONALE_CHARS} characters")
        lowered = text.lower()
        for phrase in STUB_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                raise ValueError(f"rationale contains stub phrase: {phrase!r}")
        return self


class WorkMatchDecisionFast(BaseModel):
    """Fast-mode structured judgment with conditional rationale.

    Boundary-4 contract is preserved exactly where it matters
    (``uncertain`` or ``confidence < FALLBACK_CONFIDENCE_THRESHOLD``);
    high-confidence ``same_work`` / ``different_work`` decisions may
    set ``rationale=None`` to save the rationale-generation tokens.
    Converted to :class:`WorkMatchDecision` at the boundary —
    :func:`_synthesize_fast_rationale` fills the strict schema's
    rationale from the structured fields so downstream serialisation,
    caching, and provenance writers see one unified shape.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["same_work", "different_work", "uncertain"]
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0-1.0. Use <0.7 when uncertain; reserve >0.9 for clear cases.",
    )
    rationale: str | None = Field(
        default=None,
        description=(
            "Set to null when decision is 'same_work'/'different_work' AND "
            "confidence ≥ 0.85. Required (2-4 sentences citing field values) "
            "when decision is 'uncertain' or confidence < 0.85."
        ),
    )
    matching_fields: list[str] = Field(default_factory=list)
    diverging_fields: list[str] = Field(default_factory=list)

    # --- Boundary 4 semantic validators (spec § 10 + § 7) -----------------

    @model_validator(mode="after")
    def _coherent_uncertain(self) -> WorkMatchDecisionFast:
        if self.decision == "uncertain" and self.confidence > UNCERTAIN_MAX_CONFIDENCE:
            raise ValueError(
                f"decision='uncertain' is incoherent with confidence > {UNCERTAIN_MAX_CONFIDENCE}"
            )
        return self

    @model_validator(mode="after")
    def _same_work_needs_evidence(self) -> WorkMatchDecisionFast:
        if self.decision == "same_work" and not self.matching_fields:
            raise ValueError("decision='same_work' requires at least one matching_field")
        return self

    @model_validator(mode="after")
    def _rationale_required_when_low_confidence(self) -> WorkMatchDecisionFast:
        needs_rationale = (
            self.decision == "uncertain" or self.confidence < FALLBACK_CONFIDENCE_THRESHOLD
        )
        text = (self.rationale or "").strip()
        if needs_rationale:
            if len(text) < MIN_RATIONALE_CHARS:
                raise ValueError(
                    f"rationale ≥ {MIN_RATIONALE_CHARS} chars required for 'uncertain' "
                    f"or confidence < {FALLBACK_CONFIDENCE_THRESHOLD} decisions"
                )
            lowered = text.lower()
            for phrase in STUB_PHRASES:
                if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                    raise ValueError(f"rationale contains stub phrase: {phrase!r}")
        elif text:
            # Optional rationale supplied on a high-conf decision —
            # still guard against stub phrases so a "n/a" placeholder
            # can't sneak through.
            lowered = text.lower()
            for phrase in STUB_PHRASES:
                if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                    raise ValueError(f"rationale contains stub phrase: {phrase!r}")
        return self

    def to_strict(self) -> WorkMatchDecision:
        """Convert to the strict :class:`WorkMatchDecision`.

        Synthesises a placeholder rationale from the structured fields
        when the fast-mode response left it null, so downstream code
        (provenance writer, JSONL row, cache) sees one unified shape
        regardless of mode.
        """
        rationale = (self.rationale or "").strip()
        if not rationale:
            rationale = _synthesize_fast_rationale(
                decision=self.decision,
                confidence=self.confidence,
                matching_fields=self.matching_fields,
                diverging_fields=self.diverging_fields,
            )
        return WorkMatchDecision(
            decision=self.decision,
            confidence=self.confidence,
            rationale=rationale,
            matching_fields=list(self.matching_fields),
            diverging_fields=list(self.diverging_fields),
        )


def _synthesize_fast_rationale(
    *,
    decision: str,
    confidence: float,
    matching_fields: list[str],
    diverging_fields: list[str],
) -> str:
    """Build a one-sentence rationale from structured fields for
    fast-mode high-confidence decisions that omitted natural-language
    reasoning. Passes Boundary-4 (≥ MIN_RATIONALE_CHARS chars, no
    stub phrases) so it round-trips through the strict schema.
    """
    matching = ", ".join(matching_fields) if matching_fields else "(none cited)"
    diverging = ", ".join(diverging_fields) if diverging_fields else "(none cited)"
    return (
        f"Fast-mode decision: {decision} at confidence {confidence:.2f}. "
        f"Matching fields: {matching}. Diverging fields: {diverging}. "
        "Rationale omitted by --no-full-rationale; structured fields are the evidence."
    )
