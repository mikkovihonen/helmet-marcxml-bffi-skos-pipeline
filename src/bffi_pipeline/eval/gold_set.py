"""Gold-set loader with hand-marked holdout split (spec § 9).

The gold set is a JSONL file at ``gold/gold.jsonl`` — one Work-pair
per line — used to:

1. **Benchmark candidate embedding models** against ``same_work`` vs
   ``different_work`` cosine-similarity gap (M5 sub-task).
2. **Tune ``efSearch``** by checking that high-similarity ``same_work``
   pairs are returned within the top-k from the FAISS index (M5).
3. **Score the M6 LLM judge** on per-category accuracy and confidence
   calibration (full M12).

Each case carries a ``holdout`` flag, hand-set per case (not
hash-derived per spec § 9). The committed split target is 30% but the
flag is the source of truth — stratification across categories matters
more than the exact percentage.

This module is pure data-handling: it loads the JSONL, validates the
shape with Pydantic, and splits training vs holdout. Nothing here
depends on the embedding stage or the LLM judge — both are imported
*at the call site*, never here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

GoldDecision = Literal["same_work", "different_work"]
GoldCategory = Literal[
    "translation",
    "transliteration",
    "adaptation",
    "abridgement",
    "common-title-collision",
    "compilation-vs-constituent",
    "edition-revision",
    "music-recording-vs-notated",
    "same-author-different-titles",
    "cross-genre-different-work",
]

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_DEFAULT_GOLD_PATH: Final[Path] = _REPO_ROOT / "gold" / "gold.jsonl"


class GoldRecord(BaseModel):
    """Per-record fields a gold case carries about each side of the pair.

    Carries the embedding-relevant fields M5 reads
    (``creator/title/language/year/content_type``) plus M6/M12 context
    fields (``helmet_bib_id``, ``uniform_title``, ``translated_from_lang``).
    Fields beyond the embedding subset are optional — M5 ignores them.
    """

    model_config = ConfigDict(extra="forbid")

    helmet_bib_id: str | None = None
    creator: str | None = None
    title: str | None = None
    uniform_title: str | None = None
    language: str | None = None
    year: str | None = None
    content_type: str | None = None
    translated_from_lang: str | None = None
    synthesized: bool = False
    notes: str | None = None


class GoldCase(BaseModel):
    """One pair from ``gold/gold.jsonl``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: GoldCategory
    expected: GoldDecision
    holdout: bool = False
    added: str | None = None
    added_by: str | None = None
    notes: str | None = None
    record_a: GoldRecord
    record_b: GoldRecord
    embedding_sim: float | None = Field(
        default=None,
        description=(
            "Cached cosine similarity from the last benchmark; informational, not authoritative."
        ),
    )


@dataclass(frozen=True)
class GoldSplit:
    """Training + holdout halves of a gold set."""

    training: list[GoldCase]
    holdout: list[GoldCase]

    @property
    def total(self) -> int:
        """Total number of cases across both halves of the split."""
        return len(self.training) + len(self.holdout)


# --- Loaders --------------------------------------------------------------


def load_gold_set(path: Path | None = None) -> list[GoldCase]:
    """Read ``gold/gold.jsonl`` (or the supplied path) into a list of cases.

    Raises ``FileNotFoundError`` if the file is missing — gold is a
    hard dependency of the M5 benchmark / M12 eval and we don't want
    to silently fall back to "zero cases".
    """
    target = path or _DEFAULT_GOLD_PATH
    if not target.is_file():
        raise FileNotFoundError(
            f"Gold set not found at {target!s}. Bootstrap with cases drawn from real "
            "Helmet records (see docs/marcxml-to-bffi-skosmos-pipeline.md § 9)."
        )
    cases: list[GoldCase] = []
    for line_no, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Bad JSON at {target!s}:{line_no}: {exc}") from exc
        cases.append(GoldCase.model_validate(data))
    return cases


def split_by_holdout(cases: Iterable[GoldCase]) -> GoldSplit:
    """Partition a list of gold cases on the ``holdout`` flag."""
    training: list[GoldCase] = []
    holdout: list[GoldCase] = []
    for case in cases:
        (holdout if case.holdout else training).append(case)
    return GoldSplit(training=training, holdout=holdout)


def assert_holdout_stratification(cases: Iterable[GoldCase], min_per_category: int = 2) -> None:
    """Raise ``ValueError`` if any holdout category has fewer than ``min_per_category`` cases.

    Spec § 9: "every category needs at least 2-3 hold-out cases". This
    helper makes the constraint explicit and lets callers (e.g. the
    benchmark CLI) assert it before reporting per-category metrics.
    """
    by_category: dict[str, int] = {}
    for case in cases:
        if case.holdout:
            by_category[case.category] = by_category.get(case.category, 0) + 1
    weak = {cat: n for cat, n in by_category.items() if n < min_per_category}
    if weak:
        details = ", ".join(f"{cat} (n={n})" for cat, n in sorted(weak.items()))
        raise ValueError(
            f"Holdout under-stratified: every category needs at least "
            f"{min_per_category} holdout cases, but {details}."
        )


__all__ = [
    "GoldCase",
    "GoldCategory",
    "GoldDecision",
    "GoldRecord",
    "GoldSplit",
    "assert_holdout_stratification",
    "load_gold_set",
    "split_by_holdout",
]
