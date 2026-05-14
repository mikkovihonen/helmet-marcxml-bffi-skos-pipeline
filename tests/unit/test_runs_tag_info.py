"""Unit tests for ``bffi-pipeline runs tag`` / ``untag`` / ``info`` (P-32 Phase D).

The four named acceptance tests pin: tag round-trip, untag no-op on
missing tag, info rendering shape, and uuid-prefix resolution (unique
resolves, ambiguous exits non-zero with a hint).

Tests drive the CLI via ``typer.testing.CliRunner`` and round-trip
manifest state by re-reading ``bffi-run.json`` after each tag op.
``BFFI_RUNS_ROOT`` is monkeypatched to ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bffi_pipeline.cli import app
from bffi_pipeline.config import get_settings
from bffi_pipeline.run_manifest import (
    MANIFEST_FILENAME,
    RunManifest,
    read_manifest,
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
    """Create a fake run dir with a manifest + filler payload."""
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
    write_manifest(run_dir / MANIFEST_FILENAME, manifest)
    (run_dir / "payload.bin").write_bytes(b"x" * payload_bytes)
    return run_dir


@pytest.fixture(autouse=True)
def _isolate_test_state() -> Iterator[None]:
    """Reset get_settings cache + active emitter between tests."""
    get_settings.cache_clear()
    set_active_emitter(None)
    yield
    get_settings.cache_clear()
    set_active_emitter(None)


# ---------- Named acceptance tests ----------


def test_runs_tag_adds_and_persists_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``runs tag`` appends a new tag and the manifest round-trips it."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    run_dir = _make_run(tmp_path, run_uuid="tagrun000000", started_at=now, tags=["nightly"])

    result = CliRunner().invoke(app, ["runs", "tag", "tagrun000000", "qa", "release-candidate"])
    assert result.exit_code == 0, result.output

    manifest = read_manifest(run_dir / MANIFEST_FILENAME)
    assert manifest.tags == ["nightly", "qa", "release-candidate"]

    # Idempotent re-tag of an existing tag is a no-op.
    repeat = CliRunner().invoke(app, ["runs", "tag", "tagrun000000", "qa"])
    assert repeat.exit_code == 0, repeat.output
    assert "no new tags" in repeat.output
    after = read_manifest(run_dir / MANIFEST_FILENAME)
    assert after.tags == ["nightly", "qa", "release-candidate"]


def test_runs_untag_is_noop_on_missing_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``runs untag`` exits 0 with a no-op message when the tag isn't present."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    run_dir = _make_run(tmp_path, run_uuid="untagrun0000", started_at=now, tags=["nightly"])

    result = CliRunner().invoke(app, ["runs", "untag", "untagrun0000", "not-there"])
    assert result.exit_code == 0, result.output
    assert "no-op" in result.output

    # Manifest untouched.
    manifest = read_manifest(run_dir / MANIFEST_FILENAME)
    assert manifest.tags == ["nightly"]

    # Removing an existing tag actually mutates the manifest.
    real = CliRunner().invoke(app, ["runs", "untag", "untagrun0000", "nightly"])
    assert real.exit_code == 0, real.output
    after = read_manifest(run_dir / MANIFEST_FILENAME)
    assert after.tags == []


def test_runs_info_renders_manifest_and_dir_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``runs info`` surfaces run_uuid, status, size, tags + top-level artifacts."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    run_dir = _make_run(
        tmp_path,
        run_uuid="inforun00000",
        started_at=now,
        tags=["nightly", "qa"],
        description="Phase D info shape test",
        payload_bytes=2048,
    )
    # Add a JSONL file so the artifact-row branch with row-counts fires.
    (run_dir / "stage-events.jsonl").write_bytes(
        b'{"stage":"m9","event":"start"}\n{"stage":"m9","event":"end"}\n'
    )
    # Add a sub-dir so the dir-branch fires.
    bibframe = run_dir / "bibframe"
    bibframe.mkdir()
    (bibframe / "record-1.xml").write_bytes(b"<rdf:RDF/>")
    (bibframe / "record-2.xml").write_bytes(b"<rdf:RDF/>")

    result = CliRunner().invoke(app, ["runs", "info", "inforun00000"])
    assert result.exit_code == 0, result.output

    out = result.output
    assert "inforun00000" in out
    assert "completed" in out
    assert "Phase D info shape test" in out
    assert "nightly, qa" in out
    assert "Artifacts:" in out
    assert "bibframe/" in out and "2 file(s)" in out
    assert "stage-events.jsonl" in out and "2 row(s)" in out
    assert MANIFEST_FILENAME in out


def test_runs_uuid_prefix_resolution_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unique uuid prefix resolves; ambiguous prefix exits non-zero with a hint."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    now = datetime.now(UTC)
    _make_run(tmp_path, run_uuid="abc111111111", started_at=now)
    _make_run(tmp_path, run_uuid="abc222222222", started_at=now)
    _make_run(tmp_path, run_uuid="xyz000000000", started_at=now)

    # Unique 'xyz' prefix resolves cleanly.
    unique = CliRunner().invoke(app, ["runs", "tag", "xyz", "tagged"])
    assert unique.exit_code == 0, unique.output

    # Ambiguous 'abc' prefix exits 1 with a hint listing both candidates.
    ambiguous = CliRunner().invoke(app, ["runs", "tag", "abc", "tagged"])
    assert ambiguous.exit_code == 1, ambiguous.output
    err = ambiguous.stderr if hasattr(ambiguous, "stderr") else ""
    haystack = (ambiguous.output + err).lower()
    assert "ambiguous" in haystack
    assert "abc111111111" in haystack
    assert "abc222222222" in haystack
    assert "longer prefix" in haystack

    # Missing prefix exits 1 with a clear message.
    missing = CliRunner().invoke(app, ["runs", "tag", "zzzz", "tagged"])
    assert missing.exit_code == 1, missing.output
    missing_err = missing.stderr if hasattr(missing, "stderr") else ""
    assert "no run dir" in (missing.output + missing_err).lower()
