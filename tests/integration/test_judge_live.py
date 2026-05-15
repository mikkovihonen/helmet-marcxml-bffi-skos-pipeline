"""Live cascade test against the local LLM (M6 phase 1).

Marked ``requires_llm``: excluded from CI by ``-m "not requires_llm"``.
Runs on the user's M5 Max where Ollama (`:11434`) or mlx-lm (`:8000`)
is alive, the configured Qwen3 32 B / 72 B models are pulled, and
``LLM_BASE_URL`` points at the right port.

The test picks one gold case from each of five categories
(translation, transliteration, adaptation, music-recording-vs-notated,
cross-genre-different-work), runs ``cascade_judge`` end-to-end, and
checks that at most one of the five lands on the *wrong* decision —
counting ``"uncertain"`` as a soft pass since it correctly routes to
human review per spec § 9.

When the test fails, the per-case decisions are printed so the user
can see which categories the cascade is regressing on. Use ``pytest -v
-s`` to see them on success too.
"""

from __future__ import annotations

import os
from typing import Final

import pytest

from bffi_pipeline.eval.gold_set import GoldCase, GoldRecord, load_gold_set
from bffi_pipeline.stages.m6 import (
    JudgeOutcome,
    WorkRecord,
    cascade_judge,
)

# Five categories to sample, one case each. Pick the *first* gold case
# in each category that has the right expected decision (i.e. doesn't
# require the rare other-side label). This keeps the test stable as
# the gold set grows.
_CATEGORY_TARGETS: Final[tuple[tuple[str, str], ...]] = (
    ("translation", "same_work"),
    ("transliteration", "same_work"),
    ("adaptation", "different_work"),
    ("music-recording-vs-notated", "different_work"),
    ("cross-genre-different-work", "different_work"),
)

# At most one of the five may be wrong. "Uncertain" does not count as wrong.
MAX_WRONG: Final[int] = 1


pytestmark = pytest.mark.requires_llm


def _gold_record_to_judge_record(record: GoldRecord, *, side: str) -> WorkRecord:
    """Coerce a GoldRecord into the WorkRecord schema judge_pair expects."""
    return WorkRecord(
        record_id=record.helmet_bib_id or f"synthesized-{side}",
        creator=record.creator,
        preferred_title=record.title,
        original_language=record.translated_from_lang or record.language,
        expression_language=record.language,
        content_type=record.content_type,
        publication_year=record.year,
    )


def _pick_one(cases: list[GoldCase], category: str, expected: str) -> GoldCase | None:
    for c in cases:
        if c.category == category and c.expected == expected:
            return c
    return None


def _summarise(outcome: JudgeOutcome) -> str:
    last = outcome.steps[-1]
    parts = [
        f"final={outcome.final.decision}",
        f"confidence={outcome.final.confidence:.2f}",
        f"steps={len(outcome.steps)}",
        f"model={last.model_name}",
    ]
    if outcome.used_cascade:
        parts.append("cascade=YES")
    return ", ".join(parts)


def test_cascade_handles_five_categories() -> None:
    if not os.environ.get("LLM_BASE_URL"):
        pytest.skip(
            "LLM_BASE_URL not set; live judge test requires a running Ollama/mlx-lm server."
        )

    cases = load_gold_set()
    selected: list[GoldCase] = []
    for category, expected in _CATEGORY_TARGETS:
        case = _pick_one(cases, category, expected)
        if case is None:
            pytest.skip(
                f"Bootstrap gold set has no {category!r} case with expected={expected!r}; "
                "extend gold/gold.jsonl before re-enabling."
            )
        selected.append(case)

    wrong: list[tuple[str, str, str, JudgeOutcome]] = []
    print()  # leading newline so -s output reads cleanly
    for case in selected:
        record_a = _gold_record_to_judge_record(case.record_a, side="a")
        record_b = _gold_record_to_judge_record(case.record_b, side="b")
        outcome = cascade_judge(record_a, record_b, sim=case.embedding_sim or 0.84)
        verdict = "✓"
        if outcome.final.decision not in (case.expected, "uncertain"):
            verdict = "✗"
            wrong.append((case.id, case.category, case.expected, outcome))
        print(
            f"  {verdict} {case.id:<8s} {case.category:<32s} "
            f"expected={case.expected:<14s} {_summarise(outcome)}"
        )

    if len(wrong) > MAX_WRONG:
        details = "\n".join(
            f"  {cid} {cat}: expected={exp}, got={out.final.decision} "
            f"(confidence={out.final.confidence:.2f}); rationale: {out.final.rationale}"
            for cid, cat, exp, out in wrong
        )
        pytest.fail(
            f"Cascade regressed on {len(wrong)} of {len(selected)} categories "
            f"(allowed: at most {MAX_WRONG}):\n{details}"
        )
