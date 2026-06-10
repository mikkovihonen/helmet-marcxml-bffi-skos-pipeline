"""Unit tests for the canonical run-directory convention."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bffi_pipeline.runs import (
    RUN_DIR_PATTERN,
    InvalidRunDirError,
    is_canonical_run_dir,
    mint_run_dir,
    validate_under_run_dir,
)

# --- pattern ------------------------------------------------------------


def test_pattern_accepts_canonical_name() -> None:
    assert RUN_DIR_PATTERN.fullmatch("20260610-1715-a3f2c1")


def test_pattern_rejects_truncated_hash() -> None:
    assert RUN_DIR_PATTERN.fullmatch("20260610-1715-a3f") is None


def test_pattern_rejects_uppercase_hex() -> None:
    """The hash segment is lowercase by convention (urandom().hex() output)."""
    assert RUN_DIR_PATTERN.fullmatch("20260610-1715-A3F2C1") is None


def test_pattern_rejects_missing_timestamp() -> None:
    assert RUN_DIR_PATTERN.fullmatch("a3f2c1") is None


def test_pattern_rejects_filename_lookalike() -> None:
    assert RUN_DIR_PATTERN.fullmatch("runs/20260610-1715-a3f2c1") is None
    assert RUN_DIR_PATTERN.fullmatch("test-step5-20k") is None


# --- mint_run_dir -------------------------------------------------------


def test_mint_run_dir_creates_directory(tmp_path: Path) -> None:
    parent = tmp_path / "runs"
    run = mint_run_dir(parent=parent)
    assert run.is_dir()
    assert run.parent == parent
    assert RUN_DIR_PATTERN.fullmatch(run.name)


def test_mint_run_dir_unique_per_call(tmp_path: Path) -> None:
    parent = tmp_path / "runs"
    runs = {mint_run_dir(parent=parent) for _ in range(8)}
    assert len(runs) == 8  # all 8 hashes are different


def test_mint_run_dir_name_starts_with_utc_timestamp(tmp_path: Path) -> None:
    parent = tmp_path / "runs"
    before = datetime.now(UTC)
    run = mint_run_dir(parent=parent)
    after = datetime.now(UTC)

    match = RUN_DIR_PATTERN.fullmatch(run.name)
    assert match is not None
    ts = run.name.rsplit("-", 1)[0]
    minted = datetime.strptime(ts, "%Y%m%d-%H%M").replace(tzinfo=UTC)
    assert before.replace(second=0, microsecond=0) <= minted
    assert minted <= after.replace(second=0, microsecond=0)


# --- is_canonical_run_dir ----------------------------------------------


def test_is_canonical_run_dir_accepts_runs_relative_path(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "20260610-1715-a3f2c1"
    path.mkdir(parents=True)
    assert is_canonical_run_dir(path)


def test_is_canonical_run_dir_rejects_arbitrary_parent(tmp_path: Path) -> None:
    """Only ``<parent>/runs/<canonical>/`` is acceptable — not any
    ``<canonical>`` directory."""
    path = tmp_path / "elsewhere" / "20260610-1715-a3f2c1"
    path.mkdir(parents=True)
    assert not is_canonical_run_dir(path)


# --- validate_under_run_dir --------------------------------------------


def test_validate_accepts_canonical_run_dir(tmp_path: Path) -> None:
    run = mint_run_dir(parent=tmp_path / "runs")
    ancestor = validate_under_run_dir(run)
    assert ancestor == run.resolve()


def test_validate_accepts_subpath_of_canonical_run_dir(tmp_path: Path) -> None:
    """Stage outputs typically write to ``<run>/<stage>/`` subdirs."""
    run = mint_run_dir(parent=tmp_path / "runs")
    output = run / "bibframe"
    ancestor = validate_under_run_dir(output)
    assert ancestor == run.resolve()


def test_validate_accepts_deeply_nested_subpath(tmp_path: Path) -> None:
    run = mint_run_dir(parent=tmp_path / "runs")
    nested = run / "stage" / "buckets" / "deep"
    ancestor = validate_under_run_dir(nested)
    assert ancestor == run.resolve()


def test_validate_rejects_path_outside_runs(tmp_path: Path) -> None:
    bad = tmp_path / "somewhere-else" / "output"
    bad.mkdir(parents=True)
    with pytest.raises(InvalidRunDirError, match="canonical"):
        validate_under_run_dir(bad)


def test_validate_rejects_runs_with_non_canonical_subdir(tmp_path: Path) -> None:
    """Old ad-hoc names like ``runs/test-step5-20k/`` fail validation."""
    bad = tmp_path / "runs" / "test-step5-20k"
    bad.mkdir(parents=True)
    with pytest.raises(InvalidRunDirError):
        validate_under_run_dir(bad)


def test_validate_error_message_points_at_new_run(tmp_path: Path) -> None:
    bad = tmp_path / "ad-hoc"
    bad.mkdir()
    with pytest.raises(InvalidRunDirError, match=re.escape("bffi-pipeline new-run")):
        validate_under_run_dir(bad)
