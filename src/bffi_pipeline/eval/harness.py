"""Eval harness reading ``gold/gold.jsonl`` and reporting per-category accuracy (M12).

Spec § 9 describes the harness: per-category accuracy, decided accuracy
(excluding ``uncertain``), high-confidence (≥ 0.9) accuracy, median
latency, and a confusion matrix between expected and predicted labels.
The decided + per-category metrics are the load-bearing ones —
aggregate accuracy hides regressions that matter.

Design notes:

- The harness is decoupled from the M6 judge via a ``JudgePair``
  protocol (``(record_a, record_b, sim) -> (decision, cache_hit,
  latency)``). Production callers leave it ``None`` and the harness
  builds the real ``judge_pair`` lazily; tests inject a deterministic
  stub so ``pytest`` never loads the LLM stack.
- The ``GoldRecord`` → ``WorkRecord`` mapping lives in this module
  rather than in ``gold_set`` because it embodies a per-stage decision:
  the judge reads ``preferred_title``, ``original_language``,
  ``expression_language``, ``publication_year``; gold cases carry
  ``title``, ``language``, ``translated_from_lang``, ``year``. Other
  consumers (M5 embedding benchmark) map gold records differently.
- Eval is **not in CI** — it runs manually on the M5 Max via
  ``make eval`` per spec § 9. The CLI subcommand writes a JSON summary
  to ``eval-runs/<run-label>.json`` so prior runs survive a repo
  re-clone via the ``eval-runs/`` git-ignored directory or external
  storage.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Final, Protocol

from bffi_pipeline.eval.gold_set import (
    GoldCase,
    GoldDecision,
    GoldRecord,
    load_gold_set,
)

#: High-confidence band the spec calls out as the auto-commit gate.
#: Calibration check: the model's confidence ≥ 0.9 must be right at a
#: rate the operator trusts before we let it through without review.
HIGH_CONFIDENCE_THRESHOLD: Final[float] = 0.9

#: Bucket labels for the confusion matrix. Expected only ranges over
#: the two committed gold labels; predicted spans all three judge
#: outputs because ``uncertain`` is a first-class outcome.
_PREDICTED_LABELS: Final[tuple[str, ...]] = ("same_work", "different_work", "uncertain")
_EXPECTED_LABELS: Final[tuple[GoldDecision, ...]] = ("same_work", "different_work")


# --- Per-case result ------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    """One gold case scored against one judge run."""

    id: str
    category: str
    expected: str
    predicted: str
    confidence: float
    correct: bool
    rationale: str
    latency_ms: int
    holdout: bool


# --- GoldRecord → judge WorkRecord mapping --------------------------------


def _gold_to_work_record(record: GoldRecord, *, fallback_id: str) -> Any:
    """Coerce a gold-record into the M6 judge's ``WorkRecord`` shape.

    Imported lazily so this module stays cheap to import in the unit
    tests that never touch the judge. The mapping is deliberately
    conservative: only fields that exist on both sides are propagated;
    judge-only fields like ``creator_uri`` stay ``None``.
    """
    from bffi_pipeline.stages.judge import WorkRecord

    notes: list[str] = []
    if record.notes:
        notes.append(record.notes)
    if record.uniform_title and record.uniform_title != record.title:
        notes.append(f"uniform_title={record.uniform_title}")
    return WorkRecord(
        record_id=record.helmet_bib_id or fallback_id,
        creator=record.creator,
        creator_uri=None,
        preferred_title=record.title,
        variant_titles=[record.uniform_title]
        if record.uniform_title and record.uniform_title != record.title
        else [],
        original_language=record.translated_from_lang,
        expression_language=record.language,
        content_type=record.content_type,
        date_of_origin=None,
        publication_year=record.year,
        notes=notes,
    )


# --- Pluggable judge -------------------------------------------------------


class JudgePair(Protocol):
    """Match the ``judge_pair`` signature so tests can inject a stub.

    Returns ``(decision, cache_hit, latency_seconds)`` to mirror the M6
    contract. The harness only consumes ``decision`` and ``latency`` —
    ``cache_hit`` is reserved so the same protocol can later feed a
    cache-aware reporter.
    """

    def __call__(
        self,
        record_a: Any,
        record_b: Any,
        sim: float,
    ) -> tuple[Any, bool, float]: ...


def _default_judge_pair() -> JudgePair:
    """Return the real ``judge_pair`` from the M6 stage. Lazy import."""
    from bffi_pipeline.stages.judge import judge_pair

    return judge_pair


def _default_prompt_hash() -> str:
    """Return the live judge-prompt hash. Lazy import keeps tests fast."""
    from bffi_pipeline.stages.judge import prompt_hash

    return prompt_hash()


# --- Evaluation ------------------------------------------------------------


def evaluate(
    cases: Iterable[GoldCase],
    *,
    judge: JudgePair | None = None,
    prompt_hash_value: str | None = None,
) -> list[CaseResult]:
    """Score ``cases`` and return per-case results.

    ``judge`` defaults to the real ``judge_pair`` (which loads the LLM
    stack). Tests pass a deterministic stub. ``prompt_hash_value`` is
    not consumed here — :func:`summarize` carries it through — but the
    parameter is exposed so callers can record one consistent hash for
    a whole evaluation pass even if subsequent code reloads the prompt.
    """
    del prompt_hash_value  # consumed by summarize(), passed through here for symmetry
    judge_fn = judge or _default_judge_pair()
    results: list[CaseResult] = []
    for case in cases:
        a = _gold_to_work_record(case.record_a, fallback_id=f"{case.id}.a")
        b = _gold_to_work_record(case.record_b, fallback_id=f"{case.id}.b")
        sim = case.embedding_sim if case.embedding_sim is not None else 0.0
        t0 = time.perf_counter()
        decision, _cache_hit, _latency_seconds = judge_fn(a, b, sim)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        results.append(
            CaseResult(
                id=case.id,
                category=case.category,
                expected=case.expected,
                predicted=str(decision.decision),
                confidence=float(decision.confidence),
                correct=(str(decision.decision) == case.expected),
                rationale=str(decision.rationale),
                latency_ms=latency_ms,
                holdout=case.holdout,
            )
        )
    return results


# --- Summary --------------------------------------------------------------


@dataclass
class EvalSummary:
    """Aggregate metrics for one evaluation pass.

    All numeric fields are pre-rounded to 4 decimals so JSON diffs
    between runs stay readable. ``failures`` carries every wrong case
    so a reviewer can grep the JSON without re-running the harness.
    """

    run_label: str
    gold_set_path: str
    gold_set_size: int
    holdout_size: int
    prompt_hash: str
    accuracy: float
    decided_accuracy: float
    holdout_accuracy: float | None
    holdout_decided_accuracy: float | None
    uncertain_rate: float
    high_confidence_accuracy: float | None
    high_confidence_n: int
    median_latency_ms: int
    per_category: dict[str, dict[str, float | int]]
    confusion_matrix: dict[str, dict[str, int]]
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialise the summary to JSON; default indentation reads cleanly in PR diffs."""
        return json.dumps(asdict(self), indent=indent, ensure_ascii=False)


def _per_category(results: list[CaseResult]) -> dict[str, dict[str, float | int]]:
    by_cat: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_cat[r.category].append(r)
    out: dict[str, dict[str, float | int]] = {}
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        n = len(rs)
        out[cat] = {
            "n": n,
            "accuracy": round(sum(r.correct for r in rs) / n, 4),
            "uncertain_rate": round(sum(r.predicted == "uncertain" for r in rs) / n, 4),
        }
    return out


def _confusion_matrix(results: list[CaseResult]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {
        e: {p: 0 for p in _PREDICTED_LABELS} for e in _EXPECTED_LABELS
    }
    for r in results:
        if r.expected in out:
            out[r.expected][r.predicted] = out[r.expected].get(r.predicted, 0) + 1
    return out


def _ratio(num: int, den: int) -> float:
    """Round ``num/den`` to 4 decimals; return 0.0 on a zero denominator."""
    return round(num / den, 4) if den else 0.0


def summarize(
    results: list[CaseResult],
    *,
    run_label: str,
    gold_set_path: Path,
    prompt_hash_value: str,
) -> EvalSummary:
    """Aggregate per-case results into the summary spec § 9 commits to."""
    n = len(results)
    correct = sum(r.correct for r in results)
    uncertain = sum(r.predicted == "uncertain" for r in results)
    decided_n = n - uncertain
    decided_correct = sum(r.correct for r in results if r.predicted != "uncertain")

    holdout = [r for r in results if r.holdout]
    holdout_n = len(holdout)
    holdout_correct = sum(r.correct for r in holdout)
    holdout_decided = [r for r in holdout if r.predicted != "uncertain"]

    high_conf = [
        r
        for r in results
        if r.confidence >= HIGH_CONFIDENCE_THRESHOLD and r.predicted != "uncertain"
    ]
    high_conf_acc = (
        round(sum(r.correct for r in high_conf) / len(high_conf), 4) if high_conf else None
    )

    return EvalSummary(
        run_label=run_label,
        gold_set_path=str(gold_set_path),
        gold_set_size=n,
        holdout_size=holdout_n,
        prompt_hash=prompt_hash_value,
        accuracy=_ratio(correct, n),
        decided_accuracy=_ratio(decided_correct, decided_n),
        holdout_accuracy=_ratio(holdout_correct, holdout_n) if holdout_n else None,
        holdout_decided_accuracy=(
            _ratio(
                sum(r.correct for r in holdout_decided),
                len(holdout_decided),
            )
            if holdout_decided
            else None
        ),
        uncertain_rate=_ratio(uncertain, n),
        high_confidence_accuracy=high_conf_acc,
        high_confidence_n=len(high_conf),
        median_latency_ms=int(median(r.latency_ms for r in results)) if results else 0,
        per_category=_per_category(results),
        confusion_matrix=_confusion_matrix(results),
        failures=[asdict(r) for r in results if not r.correct],
    )


def render_text(summary: EvalSummary) -> str:
    """Format the summary the way ``make eval`` paste-ready output expects."""
    lines: list[str] = [
        f"Run label:          {summary.run_label}",
        f"Gold set:           {summary.gold_set_path}  (n={summary.gold_set_size})",
        f"Holdout:            n={summary.holdout_size}",
        f"Prompt hash:        {summary.prompt_hash}",
        f"Accuracy:           {summary.accuracy:.1%}",
        f"Decided accuracy:   {summary.decided_accuracy:.1%}  (excluding uncertain)",
    ]
    if summary.holdout_accuracy is not None:
        lines.append(
            f"Holdout accuracy:   {summary.holdout_accuracy:.1%}  "
            f"(decided: {summary.holdout_decided_accuracy:.1%})"
            if summary.holdout_decided_accuracy is not None
            else f"Holdout accuracy:   {summary.holdout_accuracy:.1%}"
        )
    high_conf_str = (
        f"{summary.high_confidence_accuracy:.1%}"
        if summary.high_confidence_accuracy is not None
        else "N/A"
    )
    lines.append(f"High-conf accuracy: {high_conf_str}  (n={summary.high_confidence_n})")
    lines.append(f"Uncertain rate:     {summary.uncertain_rate:.1%}")
    lines.append(f"Median latency:     {summary.median_latency_ms} ms")
    lines.append("")
    lines.append("Per category:")
    for cat, stats in summary.per_category.items():
        accuracy = float(stats["accuracy"])
        n = int(stats["n"])
        lines.append(f"  {cat:32s} {accuracy:.1%}  (n={n})")
    return "\n".join(lines)


# --- CLI entry point ------------------------------------------------------


def run_eval(
    *,
    run_label: str,
    gold_path: Path | None = None,
    output_dir: Path | None = None,
    judge: JudgePair | None = None,
    prompt_hash_value: str | None = None,
) -> tuple[EvalSummary, Path]:
    """Load the gold set, score every case, write the summary, return (summary, path).

    ``run_label`` becomes both the file stem and the JSON ``run_label``
    field. ``output_dir`` defaults to ``<repo>/eval-runs`` (gitignored
    per spec § 9 / docs/ci-strategy.md).
    """
    # Resolve the actual gold path the loader used so the summary's
    # gold_set_path is honest about which file scored the run.
    from bffi_pipeline.eval.gold_set import _DEFAULT_GOLD_PATH

    actual_gold_path = gold_path or _DEFAULT_GOLD_PATH
    cases = load_gold_set(actual_gold_path)

    ph = prompt_hash_value or _default_prompt_hash()
    results = evaluate(cases, judge=judge, prompt_hash_value=ph)
    summary = summarize(
        results,
        run_label=run_label,
        gold_set_path=actual_gold_path,
        prompt_hash_value=ph,
    )

    out_dir = output_dir or (Path.cwd() / "eval-runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_safe_label(run_label)}.json"
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(summary.to_json(), encoding="utf-8")
    tmp.replace(out_path)
    return summary, out_path


def _safe_label(label: str) -> str:
    """Make ``label`` safe to use as a filename stem.

    Hashes any path-unsafe input rather than silently dropping
    characters — a label that came in as ``"qwen3 32b / r1"`` should
    still be uniquely identifiable on disk, not flatten to a name
    collision with ``"qwen3 32b / r2"``.
    """
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label.strip())
    if cleaned and cleaned == label.strip():
        return cleaned
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned or 'run'}-{digest}"


__all__ = [
    "HIGH_CONFIDENCE_THRESHOLD",
    "CaseResult",
    "EvalSummary",
    "JudgePair",
    "evaluate",
    "render_text",
    "run_eval",
    "summarize",
]
