"""Live cascade test against the local LLM (M3 contributor extraction).

Marked ``requires_llm``: excluded from CI by ``-m "not requires_llm"``.
Runs on the user's M5 Max where Ollama (`:11434`) or mlx-lm
(`:8000`) is alive, the configured Qwen3 8B model is pulled, and
``LLM_BASE_URL`` points at the right port.

Three representative 245$c cases are picked from the live smoke run
that surfaced during the cascade build-out. Each tests a distinct
LLM responsibility: pure new-agent extraction, role classification
beyond the obvious cataloguer marker, and transliteration / variant
detection. At most one of the three may land on a wrong shape;
the rest must produce something sensible. Use ``pytest -v -s`` to
see the extracted candidates on success.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import pytest

from bffi_pipeline.contrib_extract_llm import (
    ContribExtractDecision,
    LangChainContribExtractor,
)


@dataclass(frozen=True)
class _Case:
    label: str
    c_subfield: str
    existing_agents: tuple[str, ...]
    accept: Callable[[ContribExtractDecision], bool]
    accept_description: str


def _has_relator_for(
    name_substring: str, codes: set[str]
) -> Callable[[ContribExtractDecision], bool]:
    """Predicate: at least one contribution whose name contains
    ``name_substring`` (case-insensitive) AND whose ``relator_code``
    is in ``codes``."""

    def _check(decision: ContribExtractDecision) -> bool:
        for c in decision.contributions:
            if name_substring.lower() in c.name.lower() and c.relator_code in codes:
                return True
        return False

    return _check


def _flags_as_variant_of(canonical: str) -> Callable[[ContribExtractDecision], bool]:
    """Predicate: at least one contribution flags ``canonical`` as the
    transliteration target. Treats either pure-variant (relator None)
    or relator-hint-plus-variant outputs as acceptable — the emitter
    skips both."""

    def _check(decision: ContribExtractDecision) -> bool:
        return any(c.transliteration_of == canonical for c in decision.contributions)

    return _check


_CASES: Final[tuple[_Case, ...]] = (
    _Case(
        label="Hogwood (new conductor)",
        c_subfield="Vivaldi ; Simon Standage, The Academy of Ancient Music & Christopher Hogwood",
        existing_agents=("Vivaldi, Antonio", "Standage, Simon", "Academy of Ancient Music"),
        # Hogwood is famously a conductor of the Academy of Ancient Music;
        # cnd / prf / ctb (generic contributor) all defensible.
        accept=_has_relator_for("Hogwood", {"cnd", "prf", "ctb"}),
        accept_description="Christopher Hogwood emitted with relator in {cnd, prf, ctb}",
    ),
    _Case(
        label="Spector (foreword author)",
        c_subfield="Justine Pattison ; with a foreword by professor Tim Spector.",
        existing_agents=("Pattison, Justine",),
        # Foreword author maps to aui (author of introduction) or aft
        # (author of afterword); some models pick wpr (writer of preface)
        # which is also valid MARC. Be tolerant.
        accept=_has_relator_for("Spector", {"aui", "aft", "ctb"}),
        accept_description="Tim Spector emitted with relator in {aui, aft, ctb}",
    ),
    _Case(
        label="Anssi/Assi (variant detection)",
        c_subfield="Froberger, Johann Jacob ; Anssi Karttunen, cembalo",
        existing_agents=("Froberger, Johann Jakob", "Karttunen, Assi"),
        # 245$c says 'Anssi', 700 says 'Assi' (real Helmet record 1714651
        # carries this contradiction). The LLM should flag the 245$c
        # form as a variant of the 700 entry — script-variant detection
        # is what the cascade is for.
        accept=_flags_as_variant_of("Karttunen, Assi"),
        accept_description="Anssi Karttunen flagged as transliteration_of 'Karttunen, Assi'",
    ),
)


_MAX_WRONG: Final[int] = 1


pytestmark = pytest.mark.requires_llm


def test_contrib_cascade_handles_three_representative_cases() -> None:
    if not os.environ.get("LLM_BASE_URL"):
        pytest.skip(
            "LLM_BASE_URL not set; live contributor-extraction test requires "
            "a running Ollama / mlx-lm server."
        )

    extractor = LangChainContribExtractor()

    wrong: list[tuple[_Case, ContribExtractDecision]] = []
    print()  # leading newline so -s output reads cleanly
    for case in _CASES:
        decision = extractor.extract(
            c_subfield=case.c_subfield,
            existing_agents=case.existing_agents,
        )
        verdict = "✓" if case.accept(decision) else "✗"
        if verdict == "✗":
            wrong.append((case, decision))
        rendered = (
            ", ".join(
                f"{c.name!r} relator={c.relator_code!r} translit_of={c.transliteration_of!r}"
                for c in decision.contributions
            )
            or "(none)"
        )
        print(f"  {verdict} {case.label:<35s} → {rendered}")

    if len(wrong) > _MAX_WRONG:
        details = "\n".join(
            f"  {case.label}: expected {case.accept_description}; got "
            f"{[(c.name, c.relator_code, c.transliteration_of) for c in dec.contributions]}"
            for case, dec in wrong
        )
        pytest.fail(
            f"Cascade failed {len(wrong)} of {len(_CASES)} cases "
            f"(allowed: at most {_MAX_WRONG}):\n{details}"
        )
