"""Unit tests for ``bffi-pipeline runs list`` (P-32 Phase B).

The four named acceptance tests pin: started_at descending sort,
tag + status AND-filter, JSON-output schema, and legacy-dir skip /
opt-in behaviour. Supporting tests cover sort-by-size, the
``--older-than`` predicate, and TSV output shape.

Tests drive the CLI via ``typer.testing.CliRunner`` and the discovery
helpers indirectly via the run-dir fixture. ``BFFI_RUNS_ROOT`` is
monkeypatched to ``tmp_path``; the runtime's normal
``_init_observability`` writes its own manifest into the active
``data_dir``, which under canonical resolution is
``BFFI_RUNS_ROOT/<uuid>/`` — that extra dir is harmless for these
assertions because we filter on uuid prefix or explicit row count.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bffi_pipeline.cli import app
from bffi_pipeline.config import get_settings
from bffi_pipeline.run_manifest import (
    RunManifest,
    write_manifest,
)
from bffi_pipeline.stages.observability import set_active_emitter


def _make_run(
    runs_root: Path,
    *,
    run_uuid: str,
    started_at: datetime,
    ended_at: datetime | None = None,
    status: str = "completed",
    tags: list[str] | None = None,
    description: str = "",
    payload_bytes: int = 100,
) -> Path:
    """Create a fake run dir with a manifest + filler payload.

    Mirrors the helper in ``test_runs_prune`` so the two test files
    can be read independently.
    """
    run_dir = runs_root / run_uuid
    run_dir.mkdir(parents=True)
    manifest = RunManifest(
        run_uuid=run_uuid,
        started_at=started_at,
        ended_at=ended_at,
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

    Two pieces of state:

    1. ``@lru_cache`` on ``get_settings`` — ``Settings`` reads env vars
       at construction, and the per-test ``monkeypatch.setenv`` needs
       to take effect, so we drop the cached singleton.
    2. The process-wide active emitter — ``CliRunner.invoke`` of a CLI
       command goes through ``_init_observability`` which sets one;
       leaving it set leaks into downstream tests.
    """
    get_settings.cache_clear()
    set_active_emitter(None)
    yield
    get_settings.cache_clear()
    set_active_emitter(None)


# ---------- Named acceptance tests ----------


def test_runs_list_renders_runs_in_started_at_descending_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default sort is ``--sort started`` descending: newest row appears first."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    _make_run(tmp_path, run_uuid="aaaaaaaaaaaa", started_at=now - timedelta(days=10))
    _make_run(tmp_path, run_uuid="bbbbbbbbbbbb", started_at=now - timedelta(days=2))
    _make_run(tmp_path, run_uuid="cccccccccccc", started_at=now - timedelta(days=20))

    result = CliRunner().invoke(app, ["runs", "list"])
    assert result.exit_code == 0, result.output

    # Find the first occurrence of each uuid prefix in the rendered output.
    out = result.output
    pos_b = out.find("bbbbbbbbbbbb")
    pos_a = out.find("aaaaaaaaaaaa")
    pos_c = out.find("cccccccccccc")
    assert -1 not in (pos_a, pos_b, pos_c), out
    # Order: bbbb (2d ago) < aaaa (10d ago) < cccc (20d ago) by position.
    assert pos_b < pos_a < pos_c, out


def test_runs_list_filters_by_tag_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--tag T1 --tag T2 --status completed`` returns only runs matching ALL."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    # Matches both tags + status.
    _make_run(
        tmp_path,
        run_uuid="match0000000",
        started_at=now,
        status="completed",
        tags=["nightly", "qa"],
    )
    # Tag mismatch.
    _make_run(
        tmp_path,
        run_uuid="onlynightly0",
        started_at=now,
        status="completed",
        tags=["nightly"],
    )
    # Status mismatch.
    _make_run(
        tmp_path,
        run_uuid="abortedtag00",
        started_at=now,
        status="aborted",
        tags=["nightly", "qa"],
    )

    result = CliRunner().invoke(
        app,
        [
            "runs",
            "list",
            "--tag",
            "nightly",
            "--tag",
            "qa",
            "--status",
            "completed",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "match0000000" in result.output
    assert "onlynightly0" not in result.output
    assert "abortedtag00" not in result.output


def test_runs_list_json_output_is_parseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` emits a parseable array with the documented per-run schema."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    _make_run(
        tmp_path,
        run_uuid="json00000000",
        started_at=now - timedelta(hours=1),
        ended_at=now,
        status="completed",
        tags=["nightly"],
        description="JSON shape test",
    )

    result = CliRunner().invoke(app, ["runs", "list", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    expected_keys = {
        "run_uuid",
        "started_at",
        "ended_at",
        "status",
        "size_bytes",
        "tags",
        "description",
        "path",
    }
    assert expected_keys.issubset(row.keys()), row.keys()
    assert row["run_uuid"] == "json00000000"
    assert row["status"] == "completed"
    assert row["tags"] == ["nightly"]
    assert row["description"] == "JSON shape test"
    assert isinstance(row["size_bytes"], int)
    assert row["size_bytes"] > 0


def test_runs_list_handles_legacy_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy dirs (no manifest) skipped by default; ``--include-legacy`` opts in."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    _make_run(tmp_path, run_uuid="manifested00", started_at=now)

    legacy = tmp_path / "overnight-sample-2026-05-13"
    legacy.mkdir()
    (legacy / "stage-events.jsonl").write_bytes(b'{"stage":"m9","ts":"..."}\n')

    # Default: legacy dir hidden.
    default_result = CliRunner().invoke(app, ["runs", "list"])
    assert default_result.exit_code == 0, default_result.output
    assert "manifested00" in default_result.output
    assert "overnight-sample-2026-05-13" not in default_result.output
    assert "legacy-" not in default_result.output

    # Opt-in: legacy dir surfaces with synth uuid + status="unknown".
    legacy_result = CliRunner().invoke(app, ["runs", "list", "--include-legacy"])
    assert legacy_result.exit_code == 0, legacy_result.output
    assert "manifested00" in legacy_result.output
    assert "legacy-" in legacy_result.output
    assert "unknown" in legacy_result.output


# ---------- Supporting coverage ----------


def test_runs_list_sort_size_orders_biggest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--sort size`` orders the biggest-on-disk row first."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    _make_run(
        tmp_path, run_uuid="smallrun0000", started_at=now, payload_bytes=10
    )
    _make_run(
        tmp_path,
        run_uuid="bigrun000000",
        started_at=now - timedelta(days=5),
        payload_bytes=100_000,
    )

    result = CliRunner().invoke(app, ["runs", "list", "--sort", "size"])
    assert result.exit_code == 0, result.output
    pos_big = result.output.find("bigrun000000")
    pos_small = result.output.find("smallrun0000")
    assert pos_big != -1
    assert pos_small != -1
    assert pos_big < pos_small, result.output


def test_runs_list_older_than_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--older-than 30d`` excludes recent runs."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    _make_run(tmp_path, run_uuid="recentrun000", started_at=now - timedelta(days=2))
    _make_run(tmp_path, run_uuid="oldrun000000", started_at=now - timedelta(days=90))

    result = CliRunner().invoke(app, ["runs", "list", "--older-than", "30d"])
    assert result.exit_code == 0, result.output
    assert "oldrun000000" in result.output
    assert "recentrun000" not in result.output


def test_runs_list_tsv_output_is_tab_separated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--tsv`` emits a header row + one row per run, tab-separated."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    _make_run(
        tmp_path,
        run_uuid="tsvrow000000",
        started_at=now,
        status="completed",
        tags=["nightly"],
        description="row with\ttab",
    )

    result = CliRunner().invoke(app, ["runs", "list", "--tsv"])
    assert result.exit_code == 0, result.output

    lines = result.stdout.strip().splitlines()
    assert lines[0].split("\t")[:3] == ["run_uuid", "started_at", "ended_at"]
    data_lines = [line for line in lines[1:] if "tsvrow000000" in line]
    assert len(data_lines) == 1
    fields = data_lines[0].split("\t")
    assert fields[0] == "tsvrow000000"
    # description tab was sanitised to a space (round-trip safe).
    assert "row with tab" in fields[-1]
