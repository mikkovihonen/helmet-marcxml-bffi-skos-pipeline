"""Unit tests for ``bffi-pipeline runs prune`` (P-32 Phase C).

The five named acceptance tests pin: dry-run safety, filter-required
guard on `--apply`, `--keep-tagged` preservation, `--keep-last`
preservation, and reset-hook invocation when the flags are set.

Tests drive the CLI via ``typer.testing.CliRunner`` and the discovery
helpers directly. ``BFFI_RUNS_ROOT`` is monkeypatched to ``tmp_path``;
the pipeline's normal ``_init_observability`` doesn't fire (we shell
the CLI command in isolation) so no real run manifests are written
beyond what the test fixtures explicitly create.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bffi_pipeline import runs_reset
from bffi_pipeline.cli import app
from bffi_pipeline.config import get_settings
from bffi_pipeline.run_manifest import (
    RunManifest,
    discover_runs,
    parse_duration,
    write_manifest,
)
from bffi_pipeline.stages.observability import set_active_emitter


def _make_run(
    runs_root: Path,
    *,
    run_uuid: str,
    started_at: datetime,
    status: str = "completed",
    tags: list[str] | None = None,
    description: str = "",
    payload_bytes: int = 100,
) -> Path:
    """Create a fake run dir with a manifest + filler payload.

    Returns the run dir path. The dir contains ``bffi-run.json`` plus
    a stub data file of ``payload_bytes`` bytes so size-related
    assertions have a non-zero value.
    """
    run_dir = runs_root / run_uuid
    run_dir.mkdir(parents=True)
    manifest = RunManifest(
        run_uuid=run_uuid,
        started_at=started_at,
        bffi_data_dir=str(run_dir),
        description=description,
        tags=tags or [],
        status=status,  # type: ignore[arg-type]
    )
    write_manifest(run_dir / "bffi-run.json", manifest)
    (run_dir / "payload.bin").write_bytes(b"x" * payload_bytes)
    return run_dir


@pytest.fixture(autouse=True)
def _isolate_test_state() -> Iterator[None]:
    """Reset shared module state between tests so they don't bleed.

    Two pieces of state to clear (both BEFORE and AFTER each test):

    1. ``@lru_cache`` on ``get_settings`` — ``Settings`` reads env vars
       at construction, and the per-test ``monkeypatch.setenv`` calls
       need to take effect, so we drop the cached singleton.
    2. The process-wide active emitter — ``CliRunner.invoke`` of a CLI
       command goes through ``_init_observability`` which sets one;
       leaving it set leaks into downstream tests (notably
       ``emit_watchdog_event`` forwards to the active emitter, which
       would emit a second stderr line and break tests that read
       ``capsys.readouterr().err`` as a single payload).
    """
    get_settings.cache_clear()
    set_active_emitter(None)
    yield
    get_settings.cache_clear()
    set_active_emitter(None)


def test_runs_prune_dry_run_does_not_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (no ``--apply``) is dry-run: dirs survive the invocation."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    run_dir = _make_run(tmp_path, run_uuid="aaaaaaaa", started_at=now - timedelta(days=60))

    result = CliRunner().invoke(app, ["runs", "prune", "--older-than", "30d"])

    assert result.exit_code == 0, result.output
    assert run_dir.is_dir(), "Dry-run unexpectedly deleted the run dir"
    assert "(--dry-run is the default; pass --apply to delete.)" in result.output


def test_runs_prune_apply_requires_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--apply`` without any filter exits non-zero, refuses to delete-everything."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    run_dir = _make_run(tmp_path, run_uuid="aaaaaaaa", started_at=now)

    result = CliRunner().invoke(app, ["runs", "prune", "--apply"])

    assert result.exit_code == 2, result.output
    needle = "requires at least one filter"
    stderr_text = result.stderr if hasattr(result, "stderr") else ""
    assert needle in result.output.lower() or needle in stderr_text, result.output
    assert run_dir.is_dir(), "Filterless --apply unexpectedly deleted the run dir"


def test_runs_prune_keep_tagged_preserves_tagged_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with a tag survives ``--older-than X --keep-tagged --apply``."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    untagged = _make_run(tmp_path, run_uuid="untagged0", started_at=now - timedelta(days=60))
    tagged = _make_run(
        tmp_path,
        run_uuid="tagged000",
        started_at=now - timedelta(days=60),
        tags=["gold"],
    )

    result = CliRunner().invoke(
        app,
        ["runs", "prune", "--older-than", "30d", "--keep-tagged", "--apply"],
    )

    assert result.exit_code == 0, result.output
    assert not untagged.is_dir(), "Untagged old run should have been deleted"
    assert tagged.is_dir(), "Tagged old run should have been preserved by --keep-tagged"
    assert "Preserved by --keep-tagged" in result.output


def test_runs_prune_keep_last_n_preserves_most_recent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--keep-last 2`` preserves the two most-recent runs even with --older-than."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    # Five runs from 50 days old (newest) to 90 days old (oldest).
    runs = {
        ord_idx: _make_run(
            tmp_path,
            run_uuid=f"run-{ord_idx:08d}",
            started_at=now - timedelta(days=50 + ord_idx * 10),
        )
        for ord_idx in range(5)
    }

    result = CliRunner().invoke(
        app,
        ["runs", "prune", "--older-than", "30d", "--keep-last", "2", "--apply"],
    )

    assert result.exit_code == 0, result.output
    # The two newest (index 0, 1) survive; the rest are deleted.
    assert runs[0].is_dir(), "Newest run should be preserved by --keep-last 2"
    assert runs[1].is_dir(), "Second-newest run should be preserved by --keep-last 2"
    assert not runs[2].is_dir(), "Run #3 should have been deleted"
    assert not runs[3].is_dir(), "Run #4 should have been deleted"
    assert not runs[4].is_dir(), "Run #5 should have been deleted"


def test_runs_prune_calls_reset_helpers_when_flags_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--reset-exporter`` / ``--reset-prometheus`` / ``--reset-fuseki`` invoke their helpers.

    Phase C ships the flag plumbing; the helpers are stubs in
    ``bffi_pipeline.runs_reset`` until Phases G + H wire in the real
    reset paths. This test pins that the CLI calls into the helpers
    so the wiring is in place — Phases G + H replace the bodies, not
    the call sites.
    """
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    # Patch the helpers via the CLI's local imports.
    mock_exporter = MagicMock()
    mock_prometheus = MagicMock()
    mock_fuseki = MagicMock()
    monkeypatch.setattr(runs_reset, "reset_exporter", mock_exporter)
    monkeypatch.setattr(runs_reset, "reset_prometheus", mock_prometheus)
    monkeypatch.setattr(runs_reset, "reset_fuseki", mock_fuseki)

    now = datetime.now(UTC)
    _make_run(
        tmp_path,
        run_uuid="aaaaaaaa12345678",
        started_at=now - timedelta(days=60),
    )

    result = CliRunner().invoke(
        app,
        [
            "runs",
            "prune",
            "--older-than",
            "30d",
            "--reset-exporter",
            "--reset-prometheus",
            "--reset-fuseki",
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.output
    mock_exporter.assert_called_once()
    mock_prometheus.assert_called_once()
    # reset_prometheus receives the list of deleted run_uuids.
    deleted_uuids = mock_prometheus.call_args[0][0]
    assert deleted_uuids == ["aaaaaaaa12345678"]
    mock_fuseki.assert_called_once()


# --- Supporting tests pinning the discovery + filter primitives ---------


def test_discover_runs_returns_runs_sorted_by_started_at(
    tmp_path: Path,
) -> None:
    """``discover_runs`` returns manifested runs in ascending started_at order."""
    now = datetime.now(UTC)
    _make_run(tmp_path, run_uuid="newer000", started_at=now - timedelta(days=5))
    _make_run(tmp_path, run_uuid="older000", started_at=now - timedelta(days=30))
    # A bare dir with no manifest — should be skipped (legacy data).
    (tmp_path / "legacy-no-manifest").mkdir()
    (tmp_path / "legacy-no-manifest" / "stuff.txt").write_text("legacy")

    runs = discover_runs(tmp_path)
    assert [r.manifest.run_uuid for r in runs] == ["older000", "newer000"]


def test_parse_duration_units() -> None:
    """``parse_duration`` recognises d/w/mo/y suffixes."""
    assert parse_duration("30d") == timedelta(days=30)
    assert parse_duration("2w") == timedelta(weeks=2)
    assert parse_duration("6mo") == timedelta(days=180)
    assert parse_duration("1y") == timedelta(days=365)

    with pytest.raises(ValueError, match="Invalid duration"):
        parse_duration("30")  # missing unit
    with pytest.raises(ValueError, match="Invalid duration"):
        parse_duration("30days")  # unrecognised suffix


def test_runs_prune_handles_empty_runs_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prune against an empty runs root prints a friendly message + exits 0."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    result = CliRunner().invoke(app, ["runs", "prune", "--older-than", "30d"])

    assert result.exit_code == 0, result.output
    assert "No runs found under" in result.output


def test_runs_prune_handles_no_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no runs match the filter, the command exits 0 with a no-op message."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    _make_run(tmp_path, run_uuid="recent00", started_at=now - timedelta(days=1))

    result = CliRunner().invoke(app, ["runs", "prune", "--older-than", "30d"])

    assert result.exit_code == 0, result.output
    assert "No runs match the filters" in result.output
