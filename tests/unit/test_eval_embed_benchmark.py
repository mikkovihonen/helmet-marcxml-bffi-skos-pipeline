"""Unit tests for eval/embed_benchmark (M5 / M12).

The real sentence-transformers encoder is never loaded. ``benchmark_one_model``
takes an injectable ``similarity_fn``; the tests pass a deterministic stub
and verify the aggregation logic (per-category gap, overall gap, ranking).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from bffi_pipeline.eval.embed_benchmark import (
    DEFAULT_MODELS,
    ModelBenchmarkResult,
    benchmark_one_model,
    render_comparison,
)
from bffi_pipeline.eval.gold_set import GoldCase, GoldRecord


def _make_case(
    case_id: str,
    *,
    category: str = "translation",
    expected: str = "same_work",
    holdout: bool = False,
) -> GoldCase:
    return GoldCase(
        id=case_id,
        category=category,  # type: ignore[arg-type]
        expected=expected,  # type: ignore[arg-type]
        holdout=holdout,
        record_a=GoldRecord(creator="A", title="X", language="fin"),
        record_b=GoldRecord(creator="A", title="X", language="rus"),
    )


def _stub_sims(values: list[float]) -> Any:
    """Return a similarity_fn replacement that emits ``values`` in order."""

    def fn(
        pairs: Sequence[tuple[str, str]],
        *,
        model_name: str,
        device: str,
        batch_size: int,
    ) -> list[float]:
        if len(values) != len(pairs):
            raise AssertionError(
                f"Stub similarity_fn was given {len(pairs)} pairs but "
                f"only knows {len(values)} values."
            )
        return list(values)

    return fn


# --- benchmark_one_model --------------------------------------------------


def test_returns_zero_n_for_empty_gold_set() -> None:
    result = benchmark_one_model([], model_name="m", similarity_fn=_stub_sims([]))
    assert result.n_cases == 0
    assert result.same_work_mean is None
    assert result.different_work_mean is None
    assert result.overall_gap is None


def test_overall_gap_is_same_minus_diff_mean() -> None:
    cases = [
        _make_case("s1", expected="same_work"),
        _make_case("s2", expected="same_work"),
        _make_case("d1", expected="different_work"),
        _make_case("d2", expected="different_work"),
    ]
    sims = [0.90, 0.80, 0.40, 0.30]  # sw mean 0.85, dw mean 0.35, gap 0.50
    result = benchmark_one_model(cases, model_name="m", similarity_fn=_stub_sims(sims))
    assert result.same_work_mean == pytest.approx(0.85)
    assert result.different_work_mean == pytest.approx(0.35)
    assert result.overall_gap == pytest.approx(0.50)


def test_per_category_aggregates_correctly() -> None:
    cases = [
        _make_case("t1", category="translation", expected="same_work"),
        _make_case("t2", category="translation", expected="different_work"),
        _make_case("a1", category="adaptation", expected="different_work"),
        _make_case("a2", category="adaptation", expected="different_work"),
    ]
    sims = [0.92, 0.55, 0.30, 0.40]
    result = benchmark_one_model(cases, model_name="m", similarity_fn=_stub_sims(sims))

    trans = result.per_category["translation"]
    assert trans.same_work_n == 1
    assert trans.same_work_mean == pytest.approx(0.92)
    assert trans.different_work_n == 1
    assert trans.different_work_mean == pytest.approx(0.55)
    assert trans.gap == pytest.approx(0.92 - 0.55)

    adapt = result.per_category["adaptation"]
    assert adapt.same_work_n == 0
    assert adapt.different_work_n == 2
    assert adapt.different_work_mean == pytest.approx(0.35)
    assert adapt.gap is None  # no same_work in this category


def test_pair_scores_carry_holdout_flag() -> None:
    cases = [
        _make_case("h", holdout=True),
        _make_case("t", holdout=False),
    ]
    result = benchmark_one_model(cases, model_name="m", similarity_fn=_stub_sims([0.9, 0.8]))
    by_id = {p.case_id: p for p in result.pair_scores}
    assert by_id["h"].holdout is True
    assert by_id["t"].holdout is False


def test_raises_when_similarity_fn_returns_wrong_length() -> None:
    cases = [_make_case("a"), _make_case("b")]
    with pytest.raises(AssertionError):  # the stub itself raises before benchmark sees it
        benchmark_one_model(cases, model_name="m", similarity_fn=_stub_sims([0.5]))


def test_handles_only_same_work_cases() -> None:
    cases = [_make_case(f"s{i}", expected="same_work") for i in range(3)]
    result = benchmark_one_model(cases, model_name="m", similarity_fn=_stub_sims([0.9, 0.85, 0.8]))
    assert result.same_work_mean == pytest.approx((0.9 + 0.85 + 0.8) / 3)
    assert result.different_work_mean is None
    assert result.overall_gap is None


# --- render_comparison + ranking -----------------------------------------


def test_render_comparison_ranks_by_widest_gap() -> None:
    a = ModelBenchmarkResult(
        model_name="model-A",
        n_cases=4,
        same_work_mean=0.85,
        different_work_mean=0.55,
        overall_gap=0.30,
    )
    b = ModelBenchmarkResult(
        model_name="model-B",
        n_cases=4,
        same_work_mean=0.92,
        different_work_mean=0.40,
        overall_gap=0.52,
    )
    c = ModelBenchmarkResult(
        model_name="model-C",
        n_cases=4,
        same_work_mean=0.80,
        different_work_mean=0.60,
        overall_gap=0.20,
    )
    rendered = render_comparison([a, b, c])
    # Winner is announced; B has the widest gap.
    assert "Winner" in rendered
    assert "model-B" in rendered.splitlines()[-1]


def test_render_comparison_handles_no_results() -> None:
    assert render_comparison([]) == "No model results to render."


def test_default_models_lists_three_candidates() -> None:
    """Sanity check that the configured candidate set matches the spec."""
    assert "BAAI/bge-m3" in DEFAULT_MODELS
    assert "intfloat/multilingual-e5-large" in DEFAULT_MODELS
    assert "jinaai/jina-embeddings-v3" in DEFAULT_MODELS
