"""Unit tests for ``bffi_pipeline.eval.contrib_gold``.

Loader + Pydantic round-trip + the phantom-pointer guard. Keeps the
M3-cascade gold-set scaffolding honest before cataloguers extend it
with hand-curated cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bffi_pipeline.eval.contrib_gold import (
    ContribGoldCase,
    ContribGoldExpected,
    load_contrib_gold_set,
    split_by_holdout,
)

# --- Pydantic schema -----------------------------------------------------


def test_expected_contribution_minimal_shape() -> None:
    """A pure new-agent extraction declares relator_code only."""
    e = ContribGoldExpected(name="Christopher Hogwood", relator_code="cnd")
    assert e.transliteration_of is None


def test_expected_contribution_pure_transliteration_pointer() -> None:
    """A variant case omits relator_code."""
    e = ContribGoldExpected(name="Bridžet Kollinz", transliteration_of="Collins, Bridget")
    assert e.relator_code is None


def test_expected_contribution_extra_fields_rejected() -> None:
    """``extra=forbid`` blocks accidental field bloat in the gold JSONL."""
    with pytest.raises(ValidationError):
        ContribGoldExpected.model_validate(
            {"name": "x", "relator_code": "aut", "secret_field": True}
        )


def test_case_schema_validates_minimal_shape() -> None:
    case = ContribGoldCase(
        id="cg-test",
        category="pure-new-agent",
        c_subfield="Vivaldi ; Christopher Hogwood",
        existing_agents=("Vivaldi, Antonio",),
        expected_contributions=[
            ContribGoldExpected(name="Christopher Hogwood", relator_code="cnd")
        ],
    )
    assert case.holdout is False  # default


def test_case_unknown_category_rejected() -> None:
    """The category enum is closed: new buckets must be added to the
    Literal alongside docs/external-dependencies.md updates."""
    with pytest.raises(ValidationError):
        ContribGoldCase.model_validate(
            {
                "id": "cg-x",
                "category": "freeform-bucket-not-in-enum",
                "c_subfield": "...",
                "existing_agents": [],
                "expected_contributions": [],
            }
        )


# --- Bootstrap JSONL -----------------------------------------------------


def test_bootstrap_jsonl_loads_and_validates() -> None:
    """The committed ``gold/contrib.jsonl`` round-trips through the
    Pydantic schema. Catches malformed lines on commit."""
    cases = load_contrib_gold_set()
    assert len(cases) >= 3, "bootstrap set must seed at least the live-smoke trio"
    ids = [c.id for c in cases]
    assert ids == sorted(ids), "ids must be lexically sorted for stable diffs"


def test_bootstrap_covers_every_category_we_built_for() -> None:
    """Every category we observed during the M3 build-out has at least
    one bootstrap example. New cataloguer-supplied cases extend
    breadth; categories without an example are a smell — either we
    haven't seen them yet or the category enum is wrong."""
    cases = load_contrib_gold_set()
    categories = {c.category for c in cases}
    assert "pure-new-agent" in categories
    assert "role-classification" in categories
    assert "within-record-typo" in categories
    assert "cyrillic-latin-transliteration" in categories


def test_bootstrap_transliteration_pointers_resolve() -> None:
    """Every ``transliteration_of`` in the bootstrap JSONL points at a
    string that actually appears in the same case's
    ``existing_agents``. Phantom pointers would mislead the eval
    harness; the loader's guard catches them on load."""
    cases = load_contrib_gold_set()
    for case in cases:
        existing = set(case.existing_agents)
        for exp in case.expected_contributions:
            if exp.transliteration_of is not None:
                assert exp.transliteration_of in existing, (
                    f"{case.id}: {exp.name!r} → "
                    f"transliteration_of={exp.transliteration_of!r} not in existing"
                )


def test_bootstrap_anssi_assi_case_is_present() -> None:
    """The Karttunen case (Helmet bib ``b17146513``) is the kickoff
    within-record-typo example and worth pinning by id so a future
    refactor of the bootstrap set can't accidentally drop it.

    Pinned against the canonical Sierra bib-ID form (``b<num><check>``)
    that the gold set carries post-2026-05-12 migration."""
    cases = load_contrib_gold_set()
    matching = [c for c in cases if c.helmet_bib_id == "b17146513"]
    assert len(matching) == 1
    case = matching[0]
    assert case.category == "within-record-typo"
    assert any(e.transliteration_of == "Karttunen, Assi" for e in case.expected_contributions)


# --- Loader edge cases ---------------------------------------------------


def test_loader_raises_on_missing_file(tmp_path: Path) -> None:
    """Silent fallback to "zero cases" would let a regression slip
    through — be loud about a missing gold file."""
    with pytest.raises(FileNotFoundError):
        load_contrib_gold_set(tmp_path / "missing.jsonl")


def test_loader_raises_on_phantom_transliteration_pointer(tmp_path: Path) -> None:
    """Per the loader guard: a transliteration_of that doesn't appear
    in existing_agents is rejected on load with a clear message."""
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(
            {
                "id": "cg-bad",
                "category": "within-record-typo",
                "c_subfield": "...",
                "existing_agents": ["Real, Agent"],
                "expected_contributions": [
                    {"name": "Variant", "transliteration_of": "Phantom, NotInList"}
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"doesn't appear in existing_agents"):
        load_contrib_gold_set(bad)


def test_loader_skips_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "blanks.jsonl"
    p.write_text(
        "\n"
        + json.dumps(
            {
                "id": "cg-1",
                "category": "pure-new-agent",
                "c_subfield": "x",
                "existing_agents": [],
                "expected_contributions": [{"name": "y", "relator_code": "aut"}],
            }
        )
        + "\n\n",
        encoding="utf-8",
    )
    cases = load_contrib_gold_set(p)
    assert len(cases) == 1


# --- Holdout split -------------------------------------------------------


def test_split_partitions_on_holdout_flag() -> None:
    cases = [
        ContribGoldCase(
            id="cg-train",
            category="pure-new-agent",
            c_subfield="x",
            existing_agents=(),
            expected_contributions=[ContribGoldExpected(name="a", relator_code="aut")],
            holdout=False,
        ),
        ContribGoldCase(
            id="cg-hold",
            category="pure-new-agent",
            c_subfield="x",
            existing_agents=(),
            expected_contributions=[ContribGoldExpected(name="b", relator_code="aut")],
            holdout=True,
        ),
    ]
    split = split_by_holdout(cases)
    assert [c.id for c in split.training] == ["cg-train"]
    assert [c.id for c in split.holdout] == ["cg-hold"]
    assert split.total == 2
