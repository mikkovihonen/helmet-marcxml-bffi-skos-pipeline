"""Unit tests for the typer CLI's pure-helper logic.

The full CLI subcommands integrate against the network (LLM, Finto) and
filesystem; this module unit-tests just the argument parsers that are
non-trivial enough to warrant their own assertions.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import typer
from typer.testing import CliRunner

from bffi_pipeline import cli as cli_module
from bffi_pipeline.cli import _parse_reconcile_kinds, app
from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.m2 import ConversionErrorRow, ConversionSummary
from bffi_pipeline.stages.m3 import BffiSummary
from bffi_pipeline.stages.observability import set_active_emitter

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.fixture(autouse=True)
def _isolate_test_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Stop ``CliRunner().invoke()`` from littering the real ``runs/``.

    Every ``invoke`` here walks typer's root callback → ``_init_observability``
    → ``write_initial_manifest`` under ``settings.runs_root / run_uuid``. With
    the operator's live ``.env`` saying ``BFFI_RUNS_ROOT=./runs``, those land
    in the project's real runs dir. Three pieces of state get isolated here:

    1. ``BFFI_RUNS_ROOT`` → a throwaway ``tmp_path`` so manifests land in
       pytest's temp area instead.
    2. ``BFFI_OBSERVABILITY_SIDECAR=none`` → skip the manifest write entirely
       for these tests (they don't read it back, so writing is pure waste).
    3. ``@lru_cache`` on ``get_settings`` cleared before AND after each test,
       plus the process-wide active emitter cleared, mirroring the fixture in
       ``test_runs_*.py`` so test ordering can't leak settings cross-module.
    """
    runs_root = tmp_path_factory.mktemp("test-cli-runs")
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(runs_root))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")
    get_settings.cache_clear()
    set_active_emitter(None)
    yield
    get_settings.cache_clear()
    set_active_emitter(None)


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


# --- marc-to-bf / bf-to-bffi partial-failure exit policy ----------------
#
# Real 800 k-record batches always have a long tail of validation
# failures (missing 336/337/338, 1XX/7XX). The CLI must NOT exit
# non-zero on partial failures — `set -e` shell drivers depend on
# exit 0 to keep the multi-stage pipeline moving. Only a total
# wipeout (zero progress) is a real abort signal.


def _make_error_row(filename: str = "1.xml") -> ConversionErrorRow:
    return ConversionErrorRow(
        helmet_bib_id=filename.removesuffix(".xml"),
        filename=filename,
        error_type="marcxml-content-minimum",
        message="Missing required MARC fields",
    )


def test_marc_to_bf_exits_zero_on_partial_failure(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Some succeed, some fail → exit 0; failures stay observable via
    ``summary.render()`` + ``_errors.jsonl``."""
    summary = ConversionSummary(
        succeeded=["a.xml", "b.xml"],
        failed=[_make_error_row("c.xml")],
    )
    monkeypatch.setattr(cli_module.m2, "run", lambda *a, **kw: summary)
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    result = CliRunner().invoke(app, ["marc-to-bf", str(input_dir)])
    assert result.exit_code == 0, result.output


def test_marc_to_bf_exits_one_on_total_failure(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """No successes AND no idempotent skips → exit 1 (genuine
    catastrophe; abort the shell driver)."""
    summary = ConversionSummary(
        succeeded=[],
        failed=[_make_error_row("a.xml"), _make_error_row("b.xml")],
    )
    monkeypatch.setattr(cli_module.m2, "run", lambda *a, **kw: summary)
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    result = CliRunner().invoke(app, ["marc-to-bf", str(input_dir)])
    assert result.exit_code == 1, result.output


def test_marc_to_bf_exits_zero_when_only_idempotent_skips(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A re-run that hits 100 % idempotent skips is a no-op success,
    not a failure — even if a previous run logged failures."""
    summary = ConversionSummary(
        skipped_idempotent=["a.xml", "b.xml"],
        failed=[_make_error_row("c.xml")],
    )
    monkeypatch.setattr(cli_module.m2, "run", lambda *a, **kw: summary)
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    result = CliRunner().invoke(app, ["marc-to-bf", str(input_dir)])
    assert result.exit_code == 0, result.output


def test_bf_to_bffi_exits_zero_on_partial_failure(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Mirror of the marc-to-bf rule — partial errors don't abort."""
    summary = BffiSummary(
        converted=["a", "b"],
        errored=[("c", "boom")],
    )
    monkeypatch.setattr(cli_module.m3, "run", lambda *a, **kw: summary)
    bibframe_dir = tmp_path / "bibframe"
    bibframe_dir.mkdir()
    result = CliRunner().invoke(app, ["bf-to-bffi", "--bibframe-dir", str(bibframe_dir)])
    assert result.exit_code == 0, result.output


def test_bf_to_bffi_exits_one_on_total_failure(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    summary = BffiSummary(errored=[("a", "boom"), ("b", "boom")])
    monkeypatch.setattr(cli_module.m3, "run", lambda *a, **kw: summary)
    bibframe_dir = tmp_path / "bibframe"
    bibframe_dir.mkdir()
    result = CliRunner().invoke(app, ["bf-to-bffi", "--bibframe-dir", str(bibframe_dir)])
    assert result.exit_code == 1, result.output
