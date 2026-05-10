"""Unit tests for the typer CLI's pure-helper logic.

The full CLI subcommands integrate against the network (LLM, Finto) and
filesystem; this module unit-tests just the argument parsers that are
non-trivial enough to warrant their own assertions.
"""

from __future__ import annotations

import pytest
import typer

from bffi_pipeline.cli import _parse_reconcile_kinds


def test_parse_reconcile_kinds_none_returns_none() -> None:
    assert _parse_reconcile_kinds(None) is None


def test_parse_reconcile_kinds_blank_returns_none() -> None:
    assert _parse_reconcile_kinds("   ") is None


def test_parse_reconcile_kinds_all_returns_none() -> None:
    assert _parse_reconcile_kinds("all") is None


def test_parse_reconcile_kinds_creators_expands_to_person_corp_body() -> None:
    out = _parse_reconcile_kinds("creators")
    assert out == frozenset({"person", "corporate_body"})


def test_parse_reconcile_kinds_subjects_expands_to_subject_only() -> None:
    out = _parse_reconcile_kinds("subjects")
    assert out == frozenset({"subject"})


def test_parse_reconcile_kinds_genres_includes_kauno_and_muso() -> None:
    out = _parse_reconcile_kinds("genres")
    assert out == frozenset({"genre_form", "music_form"})


def test_parse_reconcile_kinds_combination_unions_groups() -> None:
    out = _parse_reconcile_kinds("creators,subjects")
    assert out == frozenset({"person", "corporate_body", "subject"})


def test_parse_reconcile_kinds_is_case_insensitive_and_trims_whitespace() -> None:
    out = _parse_reconcile_kinds("  Creators , SUBJECTS ")
    assert out == frozenset({"person", "corporate_body", "subject"})


def test_parse_reconcile_kinds_unknown_group_raises() -> None:
    with pytest.raises(typer.BadParameter):
        _parse_reconcile_kinds("typo_group")


def test_parse_reconcile_kinds_all_among_other_groups_short_circuits_to_none() -> None:
    """``all`` anywhere in the list collapses the filter to "every kind"."""
    assert _parse_reconcile_kinds("creators,all") is None
