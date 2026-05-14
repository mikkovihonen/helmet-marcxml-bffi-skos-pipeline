"""Unit tests for the BFFI export stage."""

from __future__ import annotations

import json
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages import export


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point ``BFFI_DATA_DIR`` at a per-test tmp dir so :func:`export.run`
    reads a clean fixture each time."""
    monkeypatch.setenv("BFFI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")
    monkeypatch.setenv("BFFI_RUN_UUID", "testrun-uuid")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_PROV_DEFAULT = "@prefix prov: <http://example/> . prov:Activity a prov:Activity .\n"


def _write_fixture(
    data_dir: Path,
    *,
    canonical_ttl: str = "@prefix bffi: <http://example/> . bffi:Work a bffi:Work .\n",
    provenance_ttl: str | None = _PROV_DEFAULT,
    helmet_map: str | None = '{"helmet_bib_id":"b1","raw_work_uri":"http://example/work"}\n',
    mint_failures_tsv: str | None = "helmet_bib_id\treason\nb999\tno-pref-label\n",
    bffi_dir_files: dict[str, str] | None = None,
) -> None:
    """Lay down a synthetic data dir matching the post-M9 layout."""
    (data_dir / "canonical.ttl").write_text(canonical_ttl)
    if provenance_ttl is not None:
        (data_dir / "provenance.ttl").write_text(provenance_ttl)
    if helmet_map is not None:
        (data_dir / "helmet-map.jsonl").write_text(helmet_map)
    if mint_failures_tsv is not None:
        (data_dir / "canonical-mint-failures.tsv").write_text(mint_failures_tsv)
    if bffi_dir_files is not None:
        bffi_dir = data_dir / "bffi"
        bffi_dir.mkdir()
        for name, content in bffi_dir_files.items():
            (bffi_dir / name).write_text(content)


def _read_archive_members(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return sorted(m.name for m in tar.getmembers())


def _read_archive_file(archive: Path, member_name: str) -> bytes:
    with tarfile.open(archive, "r:gz") as tar:
        f = tar.extractfile(member_name)
        assert f is not None, f"Missing member {member_name!r}"
        return f.read()


def test_export_bundles_canonical_and_companions(tmp_path: Path) -> None:
    """The default export includes canonical.ttl + provenance.ttl +
    helmet-map.jsonl + mint-failures.tsv + manifest.json + README.md.
    Nothing else, nothing missing."""
    _write_fixture(tmp_path)
    result = export.run()

    assert result.output_path == tmp_path / "bffi-export-testrun-uuid.tar.gz"
    members = _read_archive_members(result.output_path)
    assert members == [
        "README.md",
        "canonical-mint-failures.tsv",
        "canonical.ttl",
        "helmet-map.jsonl",
        "manifest.json",
        "provenance.ttl",
    ]
    assert set(result.included_files) == {
        "canonical.ttl",
        "provenance.ttl",
        "helmet-map.jsonl",
        "canonical-mint-failures.tsv",
    }
    assert result.skipped_missing == []
    assert result.per_record_count == 0


def test_export_skipped_files_recorded_in_manifest(tmp_path: Path) -> None:
    """Missing companion files (e.g. no mint failures this run) are
    silently skipped and recorded in manifest.skipped_missing."""
    _write_fixture(tmp_path, mint_failures_tsv=None, helmet_map=None)
    result = export.run()

    assert result.skipped_missing == [
        "helmet-map.jsonl",
        "canonical-mint-failures.tsv",
    ]
    manifest_bytes = _read_archive_file(result.output_path, "manifest.json")
    manifest = json.loads(manifest_bytes)
    assert manifest["skipped_missing"] == [
        "helmet-map.jsonl",
        "canonical-mint-failures.tsv",
    ]
    assert manifest["license"] == "CC0-1.0"


def test_export_raises_when_canonical_missing(tmp_path: Path) -> None:
    """``canonical.ttl`` is the only hard requirement; without it the
    export is meaningless and should fail loudly."""
    # No canonical.ttl written.
    (tmp_path / "provenance.ttl").write_text("# noop\n")
    with pytest.raises(FileNotFoundError, match=r"canonical\.ttl not found"):
        export.run()


def test_include_per_record_bundles_nested_archive(tmp_path: Path) -> None:
    """--include-per-record adds per-record-ttls.tar.gz inside the outer."""
    _write_fixture(
        tmp_path,
        bffi_dir_files={
            "b10001.ttl": "@prefix bffi: <http://example/> . bffi:E1 a bffi:Expression .\n",
            "b10002.ttl": "@prefix bffi: <http://example/> . bffi:E2 a bffi:Expression .\n",
        },
    )
    result = export.run(include_per_record=True)

    assert result.per_record_count == 2
    members = _read_archive_members(result.output_path)
    assert "per-record-ttls.tar.gz" in members

    # The nested archive holds the per-record TTLs under bffi/.
    inner_bytes = _read_archive_file(result.output_path, "per-record-ttls.tar.gz")
    nested_path = tmp_path / "extracted-nested.tar.gz"
    nested_path.write_bytes(inner_bytes)
    with tarfile.open(nested_path, "r:gz") as nested:
        nested_members = sorted(m.name for m in nested.getmembers())
    assert nested_members == ["bffi/b10001.ttl", "bffi/b10002.ttl"]


def test_include_per_record_skips_when_bffi_dir_missing(tmp_path: Path) -> None:
    """Opt-in flag with no bffi/ dir present: just doesn't emit the
    nested archive. Not an error — operator may legitimately have
    pruned that directory."""
    _write_fixture(tmp_path, bffi_dir_files=None)
    result = export.run(include_per_record=True)
    assert result.per_record_count == 0
    assert "per-record-ttls.tar.gz" not in _read_archive_members(result.output_path)


def test_manifest_carries_run_metadata(tmp_path: Path) -> None:
    """The manifest declares license + ontology version + run UUID +
    file inclusion lists so the receiver can audit the archive's
    contents without unpacking."""
    _write_fixture(tmp_path)
    result = export.run()
    manifest = json.loads(_read_archive_file(result.output_path, "manifest.json"))
    assert manifest["schema_version"] == 1
    assert manifest["license"] == "CC0-1.0"
    assert manifest["bffi_ontology_version"] == export.BFFI_ONTOLOGY_VERSION
    assert manifest["run"]["run_uuid"] in ("testrun-uuid", str(tmp_path.name))
    assert "export_created_at" in manifest


def test_readme_declares_cc0_and_lists_contents(tmp_path: Path) -> None:
    """The README is the receiver's at-a-glance orientation. It must
    name the CC0 license and list every included file."""
    _write_fixture(tmp_path)
    result = export.run()
    readme = _read_archive_file(result.output_path, "README.md").decode("utf-8")
    assert "CC0" in readme
    assert "canonical.ttl" in readme
    assert "provenance.ttl" in readme


def test_custom_output_path_respected(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    target = tmp_path / "custom" / "release.tar.gz"
    result = export.run(output_path=target)
    assert result.output_path == target
    assert target.is_file()
