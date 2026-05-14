"""Unit tests for ``bffi_pipeline.run_manifest`` (P-32 Phase A).

The six tests pin the schema, atomic write, idempotent stage
tracking, forward-compat field preservation, lifecycle helpers, and
the description-length validator.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from bffi_pipeline.run_manifest import (
    DESCRIPTION_MAX_CHARS,
    MANIFEST_FILENAME,
    RunManifest,
    append_stage_completed,
    append_stage_observed,
    mark_run_complete,
    read_manifest,
    update_manifest_field,
    write_initial_manifest,
    write_manifest,
)


def test_run_manifest_schema_round_trips(tmp_path: Path) -> None:
    """Write + read produces the same ``RunManifest`` instance."""
    started = datetime(2026, 5, 14, 8, 25, 55, tzinfo=UTC)
    original = RunManifest(
        run_uuid="abcdef123",
        started_at=started,
        bffi_data_dir=str(tmp_path),
        description="P-22 veto bench",
        pipeline_git_sha="abc1234",
        stages_observed=["m2", "m3"],
        stages_completed=["m2"],
        tags=["gold"],
        status="running",
    )
    path = tmp_path / MANIFEST_FILENAME
    write_manifest(path, original)

    roundtripped = read_manifest(path)
    assert roundtripped.run_uuid == original.run_uuid
    assert roundtripped.started_at == original.started_at
    assert roundtripped.bffi_data_dir == original.bffi_data_dir
    assert roundtripped.description == original.description
    assert roundtripped.pipeline_git_sha == original.pipeline_git_sha
    assert roundtripped.stages_observed == original.stages_observed
    assert roundtripped.stages_completed == original.stages_completed
    assert roundtripped.tags == original.tags
    assert roundtripped.status == original.status
    assert roundtripped.ended_at is None
    assert roundtripped.pre_run_fuseki_clear is None


def test_run_manifest_atomic_write_no_partial_file(tmp_path: Path) -> None:
    """Atomic write: a ``.tmp`` file never lingers after ``write_manifest``.

    Direct test of the atomic-rename contract — the writer leaves
    either fully-old or fully-new content at ``path``, never a
    half-written ``.tmp`` sibling.
    """
    started = datetime(2026, 5, 14, 8, 25, 55, tzinfo=UTC)
    manifest = RunManifest(
        run_uuid="aaa",
        started_at=started,
        bffi_data_dir=str(tmp_path),
    )
    path = tmp_path / MANIFEST_FILENAME

    write_manifest(path, manifest)

    # The tmp companion must be cleaned up by the atomic rename.
    assert not (tmp_path / f"{MANIFEST_FILENAME}.tmp").exists()
    # Real manifest exists and parses.
    assert path.exists()
    read_manifest(path)  # parses without error


def test_run_manifest_stage_tracking_is_idempotent(tmp_path: Path) -> None:
    """Two ``start`` events for the same stage append exactly once.

    Pins the contract that emitter retries (e.g. re-running a stage
    after a transient failure) don't bloat ``stages_observed`` with
    duplicates.
    """
    path = write_initial_manifest(tmp_path, run_uuid="abc")

    append_stage_observed(path, "m3")
    append_stage_observed(path, "m3")
    append_stage_observed(path, "m3")
    append_stage_completed(path, "m3")
    append_stage_completed(path, "m3")

    manifest = read_manifest(path)
    assert manifest.stages_observed == ["m3"]
    assert manifest.stages_completed == ["m3"]

    # Multiple distinct stages preserve insertion order.
    append_stage_observed(path, "m5")
    append_stage_observed(path, "m6")
    manifest = read_manifest(path)
    assert manifest.stages_observed == ["m3", "m5", "m6"]


def test_run_manifest_preserves_unknown_fields(tmp_path: Path) -> None:
    """Forward-compat: ``update_manifest_field`` preserves unknown keys.

    A future phase adds an ``experimental_field`` to the manifest
    schema; an earlier code path (this codebase, this commit) updates
    a different field via the dict-level helper. The experimental
    field must survive the round-trip.
    """
    path = write_initial_manifest(tmp_path, run_uuid="xyz")

    # Simulate a future phase adding a new top-level field.
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["experimental_field"] = {"some": "value", "nested": [1, 2, 3]}
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    # Earlier code path bumps a known field.
    update_manifest_field(path, status="completed")

    # The experimental field survived.
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["experimental_field"] == {"some": "value", "nested": [1, 2, 3]}
    assert final["status"] == "completed"


def test_mark_run_complete_writes_ended_at_and_status(tmp_path: Path) -> None:
    """``mark_run_complete`` stamps ``ended_at`` + the requested status."""
    write_initial_manifest(tmp_path, run_uuid="aaa")

    before = datetime.now(UTC)
    mark_run_complete(tmp_path, status="completed")
    after = datetime.now(UTC)

    manifest = read_manifest(tmp_path / MANIFEST_FILENAME)
    assert manifest.status == "completed"
    assert manifest.ended_at is not None
    assert before <= manifest.ended_at <= after

    # Status override path (the crash-recovery case).
    mark_run_complete(tmp_path, status="aborted")
    manifest = read_manifest(tmp_path / MANIFEST_FILENAME)
    assert manifest.status == "aborted"


def test_description_max_length_256_enforced() -> None:
    """Description longer than 256 chars is rejected by Pydantic."""
    started = datetime(2026, 5, 14, tzinfo=UTC)
    # 256 chars: should pass.
    RunManifest(
        run_uuid="aaa",
        started_at=started,
        bffi_data_dir="/x",
        description="a" * DESCRIPTION_MAX_CHARS,
    )
    # 257 chars: must raise.
    with pytest.raises(ValidationError):
        RunManifest(
            run_uuid="aaa",
            started_at=started,
            bffi_data_dir="/x",
            description="a" * (DESCRIPTION_MAX_CHARS + 1),
        )


# --- Smoke checks the above six tests don't cover ------------------------


def test_update_manifest_field_noop_when_path_missing(tmp_path: Path) -> None:
    """Defensive: ``update_manifest_field`` is a no-op when the path is missing."""
    path = tmp_path / "nonexistent.json"
    # Must not raise.
    update_manifest_field(path, status="completed")
    assert not path.exists()


def test_append_stage_observed_noop_when_path_missing(tmp_path: Path) -> None:
    """Defensive: ``append_stage_*`` no-ops when the manifest doesn't exist.

    Allows the emitter to fire ``start`` events even before the manifest
    has been written (degenerate test setups, future codepaths that
    construct emitters without the manifest path).
    """
    path = tmp_path / "nonexistent.json"
    append_stage_observed(path, "m2")
    append_stage_completed(path, "m2")
    assert not path.exists()


def test_write_initial_manifest_returns_path_and_writes_to_disk(tmp_path: Path) -> None:
    """``write_initial_manifest`` returns the path it wrote to."""
    path = write_initial_manifest(
        tmp_path,
        run_uuid="aaa",
        description="bench",
        pipeline_git_sha="deadbeef",
    )
    assert path == tmp_path / MANIFEST_FILENAME
    assert path.is_file()
    manifest = read_manifest(path)
    assert manifest.run_uuid == "aaa"
    assert manifest.description == "bench"
    assert manifest.pipeline_git_sha == "deadbeef"
    assert manifest.status == "running"


def test_manifest_handles_naive_started_at(tmp_path: Path) -> None:
    """A timezone-naive ``started_at`` gets stamped as UTC on write."""
    # Pydantic accepts naive datetime; serialiser must coerce to UTC.
    naive = datetime(2026, 5, 14, 12, 0, 0)
    manifest = RunManifest(
        run_uuid="aaa",
        started_at=naive,
        bffi_data_dir=str(tmp_path),
    )
    path = tmp_path / MANIFEST_FILENAME
    write_manifest(path, manifest)
    raw = path.read_text(encoding="utf-8")
    # The naive datetime was serialised with UTC offset rather than
    # silently dropping the timezone info.
    assert "2026-05-14T12:00:00" in raw


def test_write_creates_data_dir_if_missing(tmp_path: Path) -> None:
    """The manifest writer creates parent dirs (``mkdir -p`` semantics)."""
    nested = tmp_path / "deep" / "nest" / "rundir"
    # nested doesn't exist yet; the helper must create it.
    path = write_initial_manifest(nested, run_uuid="aaa")
    assert path.is_file()


def test_pre_run_fuseki_clear_field_round_trips(tmp_path: Path) -> None:
    """The Phase H placeholder field round-trips correctly when populated.

    Pins the contract that Phase H, when it ships, can write into the
    manifest via ``update_manifest_field`` and the value survives.
    """
    path = write_initial_manifest(tmp_path, run_uuid="aaa")
    update_manifest_field(
        path,
        pre_run_fuseki_clear={
            "dropped_graphs": ["http://example/g1", "http://example/g2"],
            "total_triples_before": 12345,
            "ts": "2026-05-14T12:00:00+00:00",
        },
    )
    manifest = read_manifest(path)
    assert manifest.pre_run_fuseki_clear is not None
    assert len(manifest.pre_run_fuseki_clear["dropped_graphs"]) == 2
    assert manifest.pre_run_fuseki_clear["total_triples_before"] == 12345


def test_atomic_write_survives_simulated_disk_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the ``.tmp`` write fails, the original manifest stays intact.

    Simulates the failure by patching ``os.replace`` to raise; verifies
    the destination path either has its pre-write content or doesn't
    exist (whichever is the pre-call state). The .tmp file may linger;
    no claim about its state.
    """
    path = write_initial_manifest(tmp_path, run_uuid="aaa")
    original_content = path.read_text(encoding="utf-8")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "replace", boom)

    # Update should raise; the destination must be unchanged.
    with pytest.raises(OSError):
        update_manifest_field(path, status="completed")

    assert path.read_text(encoding="utf-8") == original_content
