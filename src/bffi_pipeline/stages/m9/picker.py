"""M9 LLM-picker contract — :class:`PickerDecision`, :class:`LLMPicker`,
:class:`StubPicker`.

``PickerDecision`` is the structured-output Pydantic schema the picker
chain returns; ``LLMPicker`` is the Protocol every picker implements;
``StubPicker`` is the deterministic test double keyed on
``(work_uri, literal)``.

The production picker (:class:`LangChainLLMPicker`) lives in
:mod:`picker_chain` so this module stays import-light — Pydantic is
the only heavy dependency here.

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Protocol
from typing import Literal as LiteralType

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bffi_pipeline.stages.m9.schemas import AuthorityCandidate, EntityRequest

#: Stub phrases the picker rationale must NOT contain. Mirrors the M6
#: judge's policy — a hand-wavy rationale that doesn't cite candidate
#: fields can't be cached or trusted.
PICKER_STUB_PHRASES: Final[tuple[str, ...]] = (
    "i don't know",
    "unable to determine",
    "n/a",
    "not sure",
)

#: Maximum confidence allowed when ``decision="uncertain"``. Same value
#: as the M6 judge's UNCERTAIN_MAX_CONFIDENCE so cataloguers see one
#: coherent policy across stages.
PICKER_UNCERTAIN_MAX_CONFIDENCE: Final[float] = 0.7

#: Minimum rationale length, in characters.
PICKER_MIN_RATIONALE_CHARS: Final[int] = 20


class PickerDecision(BaseModel):
    """LLM-picker structured output: chosen URI or ``"uncertain"``.

    Pydantic validators enforce Boundary-4-style coherence:
    ``decision="chose"`` requires a non-null ``chosen_uri``;
    ``decision="uncertain"`` requires confidence ≤ 0.7; the rationale
    must be substantive and free of stub phrases.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: LiteralType["chose", "uncertain"]
    chosen_uri: str | None = None
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0-1.0. Use <0.7 when uncertain; reserve >0.9 for clear matches.",
    )
    rationale: str = Field(
        min_length=PICKER_MIN_RATIONALE_CHARS,
        description=(
            "2-4 sentences citing specific candidate fields (URI, prefLabel, "
            "dates) that drove the decision. Never introduce facts not present "
            "in the inputs."
        ),
    )

    @model_validator(mode="after")
    def _chose_requires_uri(self) -> PickerDecision:
        if self.decision == "chose" and not self.chosen_uri:
            raise ValueError("decision='chose' requires a non-null chosen_uri")
        return self

    @model_validator(mode="after")
    def _coherent_uncertain(self) -> PickerDecision:
        if self.decision == "uncertain" and self.confidence > PICKER_UNCERTAIN_MAX_CONFIDENCE:
            raise ValueError(
                f"decision='uncertain' is incoherent with "
                f"confidence > {PICKER_UNCERTAIN_MAX_CONFIDENCE}"
            )
        return self

    @model_validator(mode="after")
    def _rationale_is_substantive(self) -> PickerDecision:
        text = self.rationale.strip()
        if len(text) < PICKER_MIN_RATIONALE_CHARS:
            raise ValueError(f"rationale shorter than {PICKER_MIN_RATIONALE_CHARS} characters")
        lowered = text.lower()
        for phrase in PICKER_STUB_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                raise ValueError(f"rationale contains stub phrase: {phrase!r}")
        return self


class LLMPicker(Protocol):
    """Protocol for the LLM-driven authority picker.

    The phase-2 LangChain implementation will read
    ``prompts/picker_v1.txt`` and call the local Qwen3 cascade. Tests
    inject a deterministic stub via :class:`StubPicker`.
    """

    def pick(
        self,
        *,
        request: EntityRequest,
        candidates: list[AuthorityCandidate],
    ) -> PickerDecision:
        """Pick the authority URI for ``request`` from ``candidates``, or return ``uncertain``."""
        ...


@dataclass
class StubPicker:
    """Deterministic test picker keyed on (work_uri, literal)."""

    decisions: dict[tuple[str, str], PickerDecision] = field(default_factory=dict)

    def pick(
        self,
        *,
        request: EntityRequest,
        candidates: list[AuthorityCandidate],
    ) -> PickerDecision:
        """Look up a wired decision for ``(work_uri, literal)``; default to ``uncertain``."""
        key = (request.work_uri, request.literal)
        if key not in self.decisions:
            return PickerDecision(
                decision="uncertain",
                chosen_uri=None,
                confidence=0.5,
                rationale="StubPicker default: no decision wired for this request.",
            )
        return self.decisions[key]
