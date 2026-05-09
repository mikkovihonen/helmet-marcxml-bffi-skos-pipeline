"""Unit tests for eval/gold_set: loader + holdout split (M12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bffi_pipeline.eval.gold_set import (
    GoldCase,
    GoldRecord,
    assert_holdout_stratification,
    load_gold_set,
    split_by_holdout,
)


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
        record_a=GoldRecord(creator="A", title="X"),
        record_b=GoldRecord(creator="A", title="X"),
    )


def _write_jsonl(path: Path, lines: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(d) for d in lines), encoding="utf-8")


# --- load_gold_set --------------------------------------------------------


def test_load_round_trips_minimum_record(tmp_path: Path) -> None:
    payload = {
        "id": "gs-0001",
        "category": "translation",
        "expected": "same_work",
        "holdout": False,
        "record_a": {"creator": "Pushkin", "title": "Dubrovskij", "language": "rus"},
        "record_b": {"creator": "Pushkin", "title": "Aatelisrosvo Dubrovskij", "language": "fin"},
    }
    p = tmp_path / "gold.jsonl"
    _write_jsonl(p, [payload])

    cases = load_gold_set(p)
    assert len(cases) == 1
    case = cases[0]
    assert case.id == "gs-0001"
    assert case.expected == "same_work"
    assert case.record_a.creator == "Pushkin"
    assert case.record_a.synthesized is False  # default


def test_load_skips_blank_lines(tmp_path: Path) -> None:
    payload = {
        "id": "gs-0001",
        "category": "translation",
        "expected": "same_work",
        "record_a": {"title": "x"},
        "record_b": {"title": "y"},
    }
    p = tmp_path / "gold.jsonl"
    p.write_text("\n" + json.dumps(payload) + "\n\n", encoding="utf-8")

    assert len(load_gold_set(p)) == 1


def test_load_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_gold_set(tmp_path / "missing.jsonl")


def test_load_raises_on_unknown_category(tmp_path: Path) -> None:
    payload = {
        "id": "gs-bad",
        "category": "made-up-category",
        "expected": "same_work",
        "record_a": {"title": "x"},
        "record_b": {"title": "y"},
    }
    p = tmp_path / "gold.jsonl"
    _write_jsonl(p, [payload])
    with pytest.raises(ValidationError):
        load_gold_set(p)


def test_load_rejects_extra_fields(tmp_path: Path) -> None:
    payload = {
        "id": "gs-extra",
        "category": "translation",
        "expected": "same_work",
        "record_a": {"title": "x"},
        "record_b": {"title": "y"},
        "unknown_field": "should-fail-extra-forbid",
    }
    p = tmp_path / "gold.jsonl"
    _write_jsonl(p, [payload])
    with pytest.raises(ValidationError):
        load_gold_set(p)


def test_load_raises_on_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "gold.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Bad JSON"):
        load_gold_set(p)


# --- split_by_holdout -----------------------------------------------------


def test_split_partitions_on_holdout_flag() -> None:
    cases = [
        _make_case("a", holdout=False),
        _make_case("b", holdout=True),
        _make_case("c", holdout=False),
        _make_case("d", holdout=True),
    ]
    split = split_by_holdout(cases)
    assert [c.id for c in split.training] == ["a", "c"]
    assert [c.id for c in split.holdout] == ["b", "d"]
    assert split.total == 4


def test_split_handles_empty_iterable() -> None:
    split = split_by_holdout([])
    assert split.training == []
    assert split.holdout == []
    assert split.total == 0


# --- assert_holdout_stratification ----------------------------------------


def test_stratification_passes_when_every_category_has_two_holdouts() -> None:
    cases = [_make_case(f"t{i}", category="translation", holdout=True) for i in range(2)] + [
        _make_case(f"a{i}", category="adaptation", holdout=True) for i in range(3)
    ]
    assert_holdout_stratification(cases, min_per_category=2)  # no raise


def test_stratification_raises_when_a_category_has_one_holdout() -> None:
    cases = [
        _make_case("t1", category="translation", holdout=True),
        _make_case("t2", category="translation", holdout=True),
        _make_case("a1", category="adaptation", holdout=True),
    ]
    with pytest.raises(ValueError, match="adaptation"):
        assert_holdout_stratification(cases, min_per_category=2)


def test_stratification_only_counts_holdouts() -> None:
    """Training-only categories don't fail the holdout stratification check."""
    cases = [
        _make_case("a1", category="adaptation", holdout=False),  # training only
        _make_case("t1", category="translation", holdout=True),
        _make_case("t2", category="translation", holdout=True),
    ]
    assert_holdout_stratification(cases, min_per_category=2)  # no raise
