"""Embedding-model benchmark over the gold set (M5 sub-task / partial M12).

Picks the embedding model for Stage 2 by measuring the cosine-similarity
gap between ``same_work`` and ``different_work`` gold pairs. The wider
the gap, the more discriminating the model. Spec § 6 / BUILD_PLAN M5
budget half a day for this comparison across BGE-M3, multilingual-e5,
and jina-v3.

This module produces the comparison harness; the actual benchmark run
(which downloads model weights) happens on the user's M5 Max via the
``bffi-pipeline embed-benchmark`` CLI. Unit tests exercise the
harness with a monkeypatched encoder so the model is never loaded
during ``pytest``.

Heavy ML imports are deferred to function bodies, matching the pattern
in ``stages/embeddings``.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from bffi_pipeline.eval.gold_set import GoldCase, GoldRecord, load_gold_set
from bffi_pipeline.stages.embeddings import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_DEVICE,
    WorkEmbeddingInput,
    embedding_input_string,
)

if TYPE_CHECKING:
    import numpy as np

# Models the spec asks the benchmark to compare. The first entry is
# the BGE-M3 default; the others are the candidates that must beat it
# on this corpus's gold set to override the default.
DEFAULT_MODELS: Final[tuple[str, ...]] = (
    "BAAI/bge-m3",
    "intfloat/multilingual-e5-large",
    "jinaai/jina-embeddings-v3",
)


def _record_to_input(record: GoldRecord) -> WorkEmbeddingInput:
    """Coerce a ``GoldRecord`` into the ``WorkEmbeddingInput`` the encoder expects."""
    return WorkEmbeddingInput(
        work_uri=f"gold:{record.helmet_bib_id or 'synthesized'}",
        creator=record.creator,
        title=record.title,
        language=record.language,
        year=record.year,
        content_type=record.content_type,
    )


@dataclass(frozen=True)
class PairScore:
    """Per-pair cosine similarity from one model run."""

    case_id: str
    category: str
    expected: str
    holdout: bool
    similarity: float


@dataclass
class CategoryAggregate:
    """Mean cosine similarity for a single category, split by expected label."""

    same_work_mean: float | None = None
    same_work_n: int = 0
    different_work_mean: float | None = None
    different_work_n: int = 0

    @property
    def gap(self) -> float | None:
        """``same_work`` minus ``different_work`` mean — wider is better."""
        if self.same_work_mean is None or self.different_work_mean is None:
            return None
        return self.same_work_mean - self.different_work_mean


@dataclass
class ModelBenchmarkResult:
    """Aggregate stats for one (model, gold set) run."""

    model_name: str
    n_cases: int
    same_work_mean: float | None
    different_work_mean: float | None
    overall_gap: float | None
    per_category: dict[str, CategoryAggregate] = field(default_factory=dict)
    pair_scores: list[PairScore] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Model: {self.model_name}",
            f"  cases:               {self.n_cases}",
        ]
        sw = self.same_work_mean
        dw = self.different_work_mean
        gap = self.overall_gap
        sw_str = f"{sw:.4f}" if sw is not None else "-"
        dw_str = f"{dw:.4f}" if dw is not None else "-"
        gap_str = f"{gap:+.4f}" if gap is not None else "-"
        lines.append(f"  same_work mean:      {sw_str}")
        lines.append(f"  different_work mean: {dw_str}")
        lines.append(f"  gap (sw - dw):       {gap_str}")
        if self.per_category:
            lines.append("  per category:")
            for cat in sorted(self.per_category):
                agg = self.per_category[cat]
                cat_gap = (
                    f"{agg.gap:+.4f}"
                    if agg.gap is not None
                    else (
                        f"sw {agg.same_work_mean:.4f} (n={agg.same_work_n})"
                        if agg.same_work_mean is not None
                        else f"dw {agg.different_work_mean:.4f} (n={agg.different_work_n})"
                        if agg.different_work_mean is not None
                        else "—"
                    )
                )
                lines.append(
                    f"    {cat:<32s}  sw n={agg.same_work_n:>2}  "
                    f"dw n={agg.different_work_n:>2}  gap={cat_gap}"
                )
        return "\n".join(lines)


# --- Encoder + cosine-similarity primitives --------------------------------


def _encode(
    strings: Sequence[str],
    *,
    model_name: str,
    device: str,
    batch_size: int,
) -> np.ndarray[Any, Any]:
    """Encode ``strings`` with the named sentence-transformers model.

    L2-normalises the resulting matrix so subsequent cosine-similarity
    is just an inner product.
    """
    import numpy as np
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name, device=device)
    raw = model.encode(
        list(strings),
        batch_size=batch_size,
        convert_to_numpy=True,
        normalize_embeddings=False,
        show_progress_bar=False,
    )
    matrix = np.asarray(raw, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalised: np.ndarray[Any, Any] = matrix / norms
    return normalised


def _cosine_similarities(
    pairs: Sequence[tuple[str, str]],
    *,
    model_name: str,
    device: str,
    batch_size: int,
) -> list[float]:
    """Return the cosine similarity between each (a, b) pair after a single encode call."""
    flattened = [s for pair in pairs for s in pair]
    matrix = _encode(flattened, model_name=model_name, device=device, batch_size=batch_size)
    sims: list[float] = []
    for i in range(len(pairs)):
        a = matrix[2 * i]
        b = matrix[2 * i + 1]
        sims.append(float((a * b).sum()))
    return sims


# --- Public entry points --------------------------------------------------


def benchmark_one_model(
    cases: Sequence[GoldCase],
    *,
    model_name: str,
    device: str = DEFAULT_DEVICE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    similarity_fn: Any = None,
) -> ModelBenchmarkResult:
    """Run the embedding benchmark over ``cases`` using ``model_name``.

    ``similarity_fn`` is an optional injection point for tests — when
    supplied, it is called as
    ``similarity_fn(pairs, model_name=..., device=..., batch_size=...)``
    and must return a list of cosine similarities aligned with
    ``pairs``. Production calls leave it ``None`` so the real
    sentence-transformers encoder runs.
    """
    if not cases:
        return ModelBenchmarkResult(
            model_name=model_name,
            n_cases=0,
            same_work_mean=None,
            different_work_mean=None,
            overall_gap=None,
        )

    pair_strings: list[tuple[str, str]] = [
        (
            embedding_input_string(_record_to_input(case.record_a)),
            embedding_input_string(_record_to_input(case.record_b)),
        )
        for case in cases
    ]

    fn = similarity_fn or _cosine_similarities
    sims = fn(pair_strings, model_name=model_name, device=device, batch_size=batch_size)
    if len(sims) != len(cases):
        raise ValueError(f"similarity_fn returned {len(sims)} values for {len(cases)} cases.")

    pair_scores = [
        PairScore(
            case_id=case.id,
            category=case.category,
            expected=case.expected,
            holdout=case.holdout,
            similarity=float(sim),
        )
        for case, sim in zip(cases, sims, strict=True)
    ]

    same = [p.similarity for p in pair_scores if p.expected == "same_work"]
    diff = [p.similarity for p in pair_scores if p.expected == "different_work"]
    same_mean = statistics.fmean(same) if same else None
    diff_mean = statistics.fmean(diff) if diff else None
    overall_gap = same_mean - diff_mean if same_mean is not None and diff_mean is not None else None

    by_cat_same: dict[str, list[float]] = defaultdict(list)
    by_cat_diff: dict[str, list[float]] = defaultdict(list)
    for p in pair_scores:
        (by_cat_same if p.expected == "same_work" else by_cat_diff)[p.category].append(p.similarity)
    per_category: dict[str, CategoryAggregate] = {}
    for cat in sorted(set(by_cat_same) | set(by_cat_diff)):
        sw = by_cat_same.get(cat, [])
        dw = by_cat_diff.get(cat, [])
        per_category[cat] = CategoryAggregate(
            same_work_mean=statistics.fmean(sw) if sw else None,
            same_work_n=len(sw),
            different_work_mean=statistics.fmean(dw) if dw else None,
            different_work_n=len(dw),
        )

    return ModelBenchmarkResult(
        model_name=model_name,
        n_cases=len(cases),
        same_work_mean=same_mean,
        different_work_mean=diff_mean,
        overall_gap=overall_gap,
        per_category=per_category,
        pair_scores=pair_scores,
    )


def benchmark_models(
    cases: Sequence[GoldCase] | None = None,
    *,
    models: Sequence[str] = DEFAULT_MODELS,
    gold_path: Path | None = None,
    device: str = DEFAULT_DEVICE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    similarity_fn: Any = None,
) -> list[ModelBenchmarkResult]:
    """Run :func:`benchmark_one_model` across multiple candidate models."""
    if cases is None:
        cases = load_gold_set(gold_path)
    return [
        benchmark_one_model(
            cases,
            model_name=name,
            device=device,
            batch_size=batch_size,
            similarity_fn=similarity_fn,
        )
        for name in models
    ]


def render_comparison(results: Sequence[ModelBenchmarkResult]) -> str:
    """Print a side-by-side comparison ranked by overall gap (descending)."""
    if not results:
        return "No model results to render."
    ranked = sorted(
        results,
        key=lambda r: r.overall_gap if r.overall_gap is not None else float("-inf"),
        reverse=True,
    )
    blocks = [r.render() for r in ranked]
    winner = ranked[0]
    blocks.append(
        "Winner (widest same_work / different_work gap): "
        f"{winner.model_name} "
        + (f"(gap={winner.overall_gap:+.4f})" if winner.overall_gap is not None else "(gap=—)")
    )
    return "\n\n".join(blocks)


__all__ = [
    "DEFAULT_MODELS",
    "CategoryAggregate",
    "ModelBenchmarkResult",
    "PairScore",
    "benchmark_models",
    "benchmark_one_model",
    "render_comparison",
]
