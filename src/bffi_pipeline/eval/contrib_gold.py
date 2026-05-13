"""Contributor-extraction gold set loader (M3 cascade quality).

Mirror of :mod:`bffi_pipeline.eval.gold_set` for a different LLM task.
Where the M6 gold set carries pairwise (record_a, record_b) decisions,
this gold set carries *single-record* extraction cases: given the
``245$c`` text and the structured ``existing_agents`` list (drawn
from 100/700 in the same record), what should the cascade emit?

Each case pins:
- ``c_subfield`` — the verbatim 245$c text
- ``existing_agents`` — the agent labels already in 100/110/111/700/710/711
- ``expected_contributions`` — the extraction outcomes the cascade
  should produce, where ``relator_code`` is one of the controlled
  MARC codes (or ``None`` if the entry is purely a transliteration
  pointer) and ``transliteration_of`` points at one of the strings
  in ``existing_agents`` when the 245$c form is a variant of an
  already-captured agent.

Categories observed during the M3 build-out:

- ``pure-new-agent``: 245$c introduces an agent not in 100/700, plain
  role assignment (e.g. "Vivaldi ; Christopher Hogwood" with Hogwood
  already missing from 700).
- ``role-classification``: agent extraction is easy, the test is
  whether the LLM picks the right MARC relator code (foreword author
  → ``aft``/``aui``).
- ``within-record-typo``: 245$c spells the same person differently
  from 700 in the same record (Helmet bib 1714651 has both 'Anssi
  Karttunen' in 245$c and 'Karttunen, Assi' in 700; same Karttunen
  pair surfaces 'Johann Jacob' vs 'Johann Jakob' on Froberger).
- ``cyrillic-latin-transliteration``: 245$c gives a Latin-script
  transliteration of an agent whose 100/700 entry is in Cyrillic, or
  vice versa.
- ``corporate-body``: 245$c introduces an organisation (publisher,
  standards body) not in 710.

This module is pure data-handling — loading + Pydantic validation +
holdout split. No cascade imports here; eval orchestration imports
the cascade at the call site.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Categories observed during the M3 build-out. New categories should
#: be added here (and to docs/external-dependencies.md when asking
#: cataloguers for fresh examples) rather than freeform-typed.
ContribGoldCategory = Literal[
    "pure-new-agent",
    "role-classification",
    "within-record-typo",
    "cyrillic-latin-transliteration",
    "corporate-body",
]


_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_DEFAULT_GOLD_PATH: Final[Path] = _REPO_ROOT / "gold" / "contrib.jsonl"


class ContribGoldExpected(BaseModel):
    """One extraction the cascade is expected to produce on a gold case.

    Same shape as :class:`bffi_pipeline.contrib_extract_llm.ContribCandidate`
    but with relaxed validation: the gold set declares acceptable
    *outcomes*, and the eval harness does the at-least-one /
    relator-vocabulary checking against the live cascade's output.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    relator_code: str | None = Field(
        default=None,
        description=(
            "Acceptable MARC relator code, or one of a comma-separated set "
            "(e.g. 'aft|aui|wpr') when multiple codes are defensible. "
            "``None`` means the entry is purely a transliteration pointer."
        ),
    )
    transliteration_of: str | None = Field(
        default=None,
        description=(
            "Exact 100/700 agent string (must appear in the case's "
            "``existing_agents`` list) the 245$c name is a variant of."
        ),
    )


class ContribGoldCase(BaseModel):
    """One single-record extraction case from ``gold/contrib.jsonl``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: ContribGoldCategory
    helmet_bib_id: str | None = None
    c_subfield: str = Field(min_length=1)
    existing_agents: tuple[str, ...] = Field(default_factory=tuple)
    expected_contributions: list[ContribGoldExpected] = Field(default_factory=list)
    holdout: bool = False
    added: str | None = None
    added_by: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ContribGoldSplit:
    """Training + holdout halves of a contrib gold set."""

    training: list[ContribGoldCase]
    holdout: list[ContribGoldCase]

    @property
    def total(self) -> int:
        return len(self.training) + len(self.holdout)


# --- Loaders --------------------------------------------------------------


def load_contrib_gold_set(path: Path | None = None) -> list[ContribGoldCase]:
    """Read ``gold/contrib.jsonl`` (or the supplied path) into cases.

    Raises ``FileNotFoundError`` if the file is missing. The cascade
    eval depends on this — silently falling back to "zero cases" would
    let a regression slip past quietly.
    """
    target = path or _DEFAULT_GOLD_PATH
    if not target.is_file():
        raise FileNotFoundError(
            f"Contributor-extraction gold set not found at {target!s}. "
            "Bootstrap with cases drawn from real Helmet records "
            "(see docs/plans/backlog/p-05-m3-cascade-follow-ups.md)."
        )
    cases: list[ContribGoldCase] = []
    for line_no, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Bad JSON at {target!s}:{line_no}: {exc}") from exc
        cases.append(ContribGoldCase.model_validate(data))
    _assert_transliteration_pointers_resolve(cases)
    return cases


def _assert_transliteration_pointers_resolve(cases: Iterable[ContribGoldCase]) -> None:
    """Every ``transliteration_of`` pointer must name a string in the
    same case's ``existing_agents`` tuple. Catches typos in the gold
    JSONL before they trip up the eval harness with a confusing
    "phantom pointer" mismatch."""
    for case in cases:
        existing = set(case.existing_agents)
        for exp in case.expected_contributions:
            if exp.transliteration_of is not None and exp.transliteration_of not in existing:
                raise ValueError(
                    f"Gold case {case.id!r}: expected_contributions entry "
                    f"{exp.name!r} has transliteration_of={exp.transliteration_of!r} "
                    f"which doesn't appear in existing_agents {sorted(existing)!r}."
                )


def split_by_holdout(cases: Iterable[ContribGoldCase]) -> ContribGoldSplit:
    """Partition cases on the ``holdout`` flag."""
    training: list[ContribGoldCase] = []
    holdout: list[ContribGoldCase] = []
    for case in cases:
        (holdout if case.holdout else training).append(case)
    return ContribGoldSplit(training=training, holdout=holdout)


__all__ = [
    "ContribGoldCase",
    "ContribGoldCategory",
    "ContribGoldExpected",
    "ContribGoldSplit",
    "load_contrib_gold_set",
    "split_by_holdout",
]
