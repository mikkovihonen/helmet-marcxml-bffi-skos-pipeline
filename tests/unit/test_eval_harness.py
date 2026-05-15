"""Unit tests for eval/harness: scoring + summarisation (M12).

The LLM judge is not exercised here — every test injects a stub that
maps ``(record_a, record_b, sim)`` to a deterministic
``WorkMatchDecision``. Pure aggregation logic (per-category accuracy,
confusion matrix, decided-vs-undecided split) is unit-tested against
hand-rolled ``CaseResult`` lists so the tests stay decoupled from the
gold-set on disk and the judge wiring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bffi_pipeline.eval.gold_set import GoldCase, GoldRecord
from bffi_pipeline.eval.harness import (
    HIGH_CONFIDENCE_THRESHOLD,
    CaseResult,
    evaluate,
    render_text,
    run_eval,
    summarize,
)
from bffi_pipeline.stages.m6 import WorkMatchDecision

# --- summarize: pure aggregation -----------------------------------------


def _result(
    *,
    case_id: str = "gs-0001",
    category: str = "translation",
    expected: str = "same_work",
    predicted: str = "same_work",
    confidence: float = 0.95,
    correct: bool | None = None,
    latency_ms: int = 100,
    holdout: bool = False,
) -> CaseResult:
    return CaseResult(
        id=case_id,
        category=category,
        expected=expected,
        predicted=predicted,
        confidence=confidence,
        correct=expected == predicted if correct is None else correct,
        rationale="Plenty of detail here, more than twenty characters total.",
        latency_ms=latency_ms,
        holdout=holdout,
    )


def _summarize(results: list[CaseResult]) -> Any:
    return summarize(
        results,
        run_label="test-run",
        gold_set_path=Path("/tmp/gold.jsonl"),
        prompt_hash_value="sha256:test",
    )


def test_summarize_empty_results_returns_zero_metrics() -> None:
    summary = _summarize([])
    assert summary.gold_set_size == 0
    assert summary.accuracy == 0.0
    assert summary.decided_accuracy == 0.0
    assert summary.median_latency_ms == 0
    assert summary.holdout_accuracy is None
    assert summary.high_confidence_accuracy is None
    assert summary.failures == []


def test_summarize_aggregate_accuracy_across_all_results() -> None:
    results = [
        _result(case_id="a", correct=True),
        _result(case_id="b", correct=True),
        _result(case_id="c", correct=False, predicted="different_work"),
        _result(case_id="d", correct=False, predicted="different_work"),
    ]
    summary = _summarize(results)
    assert summary.accuracy == 0.5
    # 4 decided, 2 correct → 0.5
    assert summary.decided_accuracy == 0.5
    assert summary.uncertain_rate == 0.0


def test_summarize_decided_accuracy_excludes_uncertain() -> None:
    """Uncertain predictions don't count against decided accuracy."""
    results = [
        _result(case_id="a", correct=True),
        _result(case_id="b", correct=True),
        _result(case_id="c", predicted="uncertain", confidence=0.5, correct=False),
    ]
    summary = _summarize(results)
    assert summary.accuracy == round(2 / 3, 4)  # 2/3 across the whole set
    assert summary.decided_accuracy == 1.0  # 2/2 of the *decided*
    assert summary.uncertain_rate == round(1 / 3, 4)


def test_summarize_per_category_buckets() -> None:
    results = [
        _result(case_id="a", category="translation", correct=True),
        _result(case_id="b", category="translation", correct=False, predicted="different_work"),
        _result(case_id="c", category="adaptation", correct=True),
    ]
    summary = _summarize(results)
    assert summary.per_category == {
        "adaptation": {"n": 1, "accuracy": 1.0, "uncertain_rate": 0.0},
        "translation": {"n": 2, "accuracy": 0.5, "uncertain_rate": 0.0},
    }


def test_summarize_confusion_matrix_counts_all_cells() -> None:
    results = [
        _result(case_id="a", expected="same_work", predicted="same_work"),
        _result(
            case_id="b",
            expected="same_work",
            predicted="different_work",
            correct=False,
        ),
        _result(
            case_id="c",
            expected="different_work",
            predicted="same_work",
            correct=False,
        ),
        _result(
            case_id="d",
            expected="different_work",
            predicted="different_work",
        ),
        _result(
            case_id="e",
            expected="same_work",
            predicted="uncertain",
            confidence=0.4,
            correct=False,
        ),
    ]
    summary = _summarize(results)
    assert summary.confusion_matrix == {
        "same_work": {"same_work": 1, "different_work": 1, "uncertain": 1},
        "different_work": {"same_work": 1, "different_work": 1, "uncertain": 0},
    }


def test_summarize_high_confidence_accuracy_uses_threshold() -> None:
    """Only predictions with confidence ≥ 0.9 (and not uncertain) count."""
    results = [
        _result(case_id="a", confidence=HIGH_CONFIDENCE_THRESHOLD, correct=True),
        _result(case_id="b", confidence=0.95, correct=True),
        _result(
            case_id="c",
            confidence=0.97,
            correct=False,
            predicted="different_work",
        ),
        # below threshold — should not contribute
        _result(case_id="d", confidence=0.85, correct=True),
    ]
    summary = _summarize(results)
    assert summary.high_confidence_n == 3
    assert summary.high_confidence_accuracy == round(2 / 3, 4)


def test_summarize_high_confidence_skips_uncertain_band() -> None:
    """Uncertain predictions never count toward the high-conf bucket even if confidence ≥ 0.9."""
    results = [
        _result(case_id="a", predicted="uncertain", confidence=0.0, correct=False),
    ]
    summary = _summarize(results)
    assert summary.high_confidence_n == 0
    assert summary.high_confidence_accuracy is None


def test_summarize_holdout_metrics_split() -> None:
    """Holdout-only metrics report the hand-marked subset's accuracy."""
    results = [
        _result(case_id="a", holdout=False, correct=True),
        _result(case_id="b", holdout=True, correct=True),
        _result(case_id="c", holdout=True, correct=False, predicted="different_work"),
        _result(case_id="d", holdout=True, predicted="uncertain", confidence=0.5, correct=False),
    ]
    summary = _summarize(results)
    assert summary.holdout_size == 3
    assert summary.holdout_accuracy == round(1 / 3, 4)
    # holdout_decided drops the uncertain → 1/2 correct
    assert summary.holdout_decided_accuracy == 0.5


def test_summarize_median_latency_handles_odd_and_even_sizes() -> None:
    odd = [_result(case_id=f"r{i}", latency_ms=ms) for i, ms in enumerate([100, 200, 300])]
    even = [_result(case_id=f"r{i}", latency_ms=ms) for i, ms in enumerate([100, 200, 300, 400])]
    assert _summarize(odd).median_latency_ms == 200
    # statistics.median averages the two middle elements on even sizes.
    assert _summarize(even).median_latency_ms == 250


def test_summarize_failures_carry_full_context() -> None:
    results = [
        _result(case_id="ok"),
        _result(case_id="bad", expected="same_work", predicted="different_work", correct=False),
    ]
    summary = _summarize(results)
    failure_ids = [f["id"] for f in summary.failures]
    assert failure_ids == ["bad"]
    assert summary.failures[0]["expected"] == "same_work"
    assert summary.failures[0]["predicted"] == "different_work"
    assert summary.failures[0]["rationale"]


def test_summarize_carries_run_label_and_prompt_hash() -> None:
    summary = _summarize([_result(case_id="a", correct=True)])
    assert summary.run_label == "test-run"
    assert summary.prompt_hash == "sha256:test"
    assert summary.gold_set_path == "/tmp/gold.jsonl"


# --- evaluate: end-to-end with a stub judge ------------------------------


def _good_decision(label: str, *, confidence: float = 0.95) -> WorkMatchDecision:
    return WorkMatchDecision(
        decision=label,  # type: ignore[arg-type]
        confidence=confidence,
        rationale="Stub-judge rationale long enough to satisfy the validator.",
        matching_fields=["creator"] if label == "same_work" else [],
        diverging_fields=["preferred_title"] if label == "different_work" else [],
    )


def _gold_case(
    *,
    case_id: str,
    expected: str,
    holdout: bool = False,
    creator: str | None = "Pushkin",
    title: str | None = "Dubrovskij",
) -> GoldCase:
    return GoldCase(
        id=case_id,
        category="translation",  # type: ignore[arg-type]
        expected=expected,  # type: ignore[arg-type]
        holdout=holdout,
        record_a=GoldRecord(creator=creator, title=title, language="rus"),
        record_b=GoldRecord(creator=creator, title=title, language="fin"),
    )


def test_evaluate_runs_judge_per_case_and_records_predictions() -> None:
    cases = [
        _gold_case(case_id="g1", expected="same_work"),
        _gold_case(case_id="g2", expected="different_work", holdout=True),
    ]
    seen: list[tuple[str, float]] = []

    def stub_judge(record_a: Any, record_b: Any, sim: float) -> tuple[Any, bool, float]:
        seen.append((record_a.record_id, sim))
        return _good_decision("same_work"), False, 0.01

    results = evaluate(cases, judge=stub_judge)
    assert [r.predicted for r in results] == ["same_work", "same_work"]
    assert results[0].correct is True  # g1: same_work == same_work
    assert results[1].correct is False  # g2: different_work expected but predicted same_work
    assert results[1].holdout is True
    # Stub was invoked once per case, with the GoldRecord-derived ids.
    assert {sid for sid, _ in seen} == {"g1.a", "g2.a"}


def test_evaluate_passes_embedding_sim_to_the_judge() -> None:
    case = _gold_case(case_id="g1", expected="same_work")
    case_with_sim = case.model_copy(update={"embedding_sim": 0.84})
    case_without = case.model_copy(update={"id": "g2", "embedding_sim": None})

    sims: list[float] = []

    def stub_judge(_a: Any, _b: Any, sim: float) -> tuple[Any, bool, float]:
        sims.append(sim)
        return _good_decision("same_work"), False, 0.01

    evaluate([case_with_sim, case_without], judge=stub_judge)
    # Cases with no embedding_sim default to 0.0 so the judge sees a stable input.
    assert sims == [0.84, 0.0]


def test_evaluate_propagates_uncertain_decision() -> None:
    cases = [_gold_case(case_id="g1", expected="same_work")]

    def stub_judge(_a: Any, _b: Any, _sim: float) -> tuple[Any, bool, float]:
        return _good_decision("uncertain", confidence=0.4), False, 0.01

    results = evaluate(cases, judge=stub_judge)
    assert results[0].predicted == "uncertain"
    assert results[0].correct is False  # uncertain never counts as correct


def test_evaluate_uses_holdout_flag_from_gold_case() -> None:
    cases = [
        _gold_case(case_id="t1", expected="same_work", holdout=False),
        _gold_case(case_id="h1", expected="same_work", holdout=True),
    ]

    def stub_judge(_a: Any, _b: Any, _sim: float) -> tuple[Any, bool, float]:
        return _good_decision("same_work"), False, 0.01

    results = evaluate(cases, judge=stub_judge)
    assert [r.holdout for r in results] == [False, True]


# --- run_eval: writes JSON, returns summary ------------------------------


def _write_gold(path: Path, cases: list[GoldCase]) -> None:
    path.write_text(
        "\n".join(c.model_dump_json(exclude_none=False) for c in cases) + "\n",
        encoding="utf-8",
    )


def test_run_eval_writes_json_summary_and_returns_path(tmp_path: Path) -> None:
    gold_path = tmp_path / "gold.jsonl"
    cases = [
        _gold_case(case_id="g1", expected="same_work"),
        _gold_case(case_id="g2", expected="different_work", holdout=True),
    ]
    _write_gold(gold_path, cases)

    def stub_judge(_a: Any, _b: Any, _sim: float) -> tuple[Any, bool, float]:
        return _good_decision("same_work"), False, 0.01

    summary, out_path = run_eval(
        run_label="qwen3-32b-test",
        gold_path=gold_path,
        output_dir=tmp_path / "eval-runs",
        judge=stub_judge,
        prompt_hash_value="sha256:fixed",
    )
    assert out_path == tmp_path / "eval-runs" / "qwen3-32b-test.json"
    assert out_path.is_file()
    payload = json.loads(out_path.read_text())
    assert payload["run_label"] == "qwen3-32b-test"
    assert payload["prompt_hash"] == "sha256:fixed"
    assert payload["gold_set_size"] == 2
    assert summary.gold_set_size == 2


def test_run_eval_label_with_unsafe_chars_falls_back_to_hashed_filename(tmp_path: Path) -> None:
    """Filename-unsafe labels round-trip through a deterministic hash so they don't collide."""
    gold_path = tmp_path / "gold.jsonl"
    _write_gold(gold_path, [_gold_case(case_id="g1", expected="same_work")])

    def stub_judge(_a: Any, _b: Any, _sim: float) -> tuple[Any, bool, float]:
        return _good_decision("same_work"), False, 0.01

    _, out_path = run_eval(
        run_label="qwen3 32b / r1",  # contains spaces and slash
        gold_path=gold_path,
        output_dir=tmp_path / "eval-runs",
        judge=stub_judge,
        prompt_hash_value="sha256:fixed",
    )
    # Original unsafe label was sanitised → underscores plus hash suffix.
    assert "/" not in out_path.name
    assert " " not in out_path.name
    assert out_path.suffix == ".json"


def test_run_eval_missing_gold_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_eval(
            run_label="x",
            gold_path=tmp_path / "missing.jsonl",
            output_dir=tmp_path / "eval-runs",
            judge=lambda *_a, **_kw: (_good_decision("same_work"), False, 0.01),
            prompt_hash_value="sha256:fixed",
        )


# --- render_text: paste-ready output -------------------------------------


def test_render_text_includes_paste_ready_lines() -> None:
    results = [
        _result(case_id="a", category="translation", correct=True),
        _result(case_id="b", category="translation", correct=False, predicted="different_work"),
    ]
    text = render_text(_summarize(results))
    assert "Run label:          test-run" in text
    assert "Accuracy:           50.0%" in text
    assert "Per category:" in text
    assert "translation" in text
