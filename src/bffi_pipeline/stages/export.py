"""Stage `export`: bundle the finalised BFFI as a CC0 release artifact.

The post-M9 ``canonical.ttl`` is the load-bearing payload — it carries
the merged canonical Works + Expressions plus M9's ``prov:specializationOf``
reconciliation triples. This stage tars + gzips it together with the
small companion files a downstream recipient (NLF, peer libraries, an
offline harvester) needs to make sense of the data:

- ``provenance.ttl`` — ``bffi-prov:Activity`` audit trail
- ``helmet-map.jsonl`` — source ``bib_id`` ↔ canonical Work URI mapping
- ``canonical-mint-failures.tsv`` — cataloguer-facing record of bib_ids
  that didn't make it into the canonical graph, with reasons
- ``manifest.json`` — run UUID, git SHA, pipeline version, timestamps,
  list of files present in the archive (so a partial export is
  obvious to the receiver)
- ``README.md`` — one-screen orientation: what the archive is, the
  CC0 declaration, the pipeline commit, and a one-line "load it"
  hint

Per-record BFFI Turtles (`runs/<uuid>/bffi/*.ttl`) are excluded by
default and bundled as a **nested** ``per-record-ttls.tar.gz`` inside
the outer archive when ``include_per_record=True``. The nested
form keeps the default archive's size predictable while preserving
the option to ship full provenance for recipients who want to
re-process.

Missing-input policy: every auto-included file is included if it
exists; missing files get a ``skipped_missing`` entry in the manifest
so the receiver can tell a deliberately-partial export apart from a
broken one. The stage never fails on a missing companion — the
``canonical.ttl`` is the only hard requirement.
"""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from bffi_pipeline.config import get_settings
from bffi_pipeline.run_manifest import read_manifest
from bffi_pipeline.stages.observability import emit_if_active

#: Files auto-included from ``<BFFI_DATA_DIR>``. Order is the order they
#: land in the tarball — ``canonical.ttl`` first so a casual ``tar tzf``
#: shows the headline payload at the top.
_AUTO_INCLUDED_FILES: Final[tuple[str, ...]] = (
    "canonical.ttl",
    "provenance.ttl",
    "helmet-map.jsonl",
    "canonical-mint-failures.tsv",
)

#: BFFI ontology version baked into the manifest. Hardcoded because the
#: vendored ``docs/lkd.rdf`` doesn't carry a machine-readable
#: ``owl:versionInfo`` we can read at runtime, and the version
#: hasn't moved since project inception. Bump when the upstream
#: vocabulary does.
BFFI_ONTOLOGY_VERSION: Final[str] = "1.0.0"


@dataclass(frozen=True)
class ExportSummary:
    """Result of an :func:`run` invocation, rendered to stdout via :meth:`render`."""

    output_path: Path
    included_files: list[str] = field(default_factory=list)
    skipped_missing: list[str] = field(default_factory=list)
    per_record_count: int = 0
    archive_bytes: int = 0

    def render(self) -> str:
        lines = [
            f"export → {self.output_path}",
            f"  archive size: {self.archive_bytes:,} bytes",
            f"  included:     {', '.join(self.included_files) or '(none)'}",
        ]
        if self.skipped_missing:
            lines.append(f"  skipped:      {', '.join(self.skipped_missing)}")
        if self.per_record_count:
            lines.append(
                f"  per-record:   {self.per_record_count} TTLs bundled as per-record-ttls.tar.gz"
            )
        return "\n".join(lines)


def _git_sha() -> str | None:
    """Return ``git rev-parse HEAD`` of the pipeline repo, or ``None``.

    Used to stamp the export's manifest with the pipeline commit that
    produced it. Falls back to ``None`` when invoked outside a git
    checkout (e.g., from a packaged wheel) so the manifest stays valid.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _build_manifest(
    *,
    data_dir: Path,
    output_path: Path,
    included_files: list[str],
    skipped_missing: list[str],
    include_per_record: bool,
    per_record_count: int,
) -> dict[str, object]:
    """Compose the export's ``manifest.json`` payload.

    Reads the run's ``bffi-run.json`` (if present) for the run-level
    metadata: UUID, description, start/end timestamps. Falls back to
    sensible defaults on missing manifest so a hand-crafted data dir
    can still be exported.
    """
    run_meta: dict[str, object] = {}
    bffi_run_path = data_dir / "bffi-run.json"
    if bffi_run_path.is_file():
        try:
            m = read_manifest(bffi_run_path)
            run_meta = {
                "run_uuid": m.run_uuid,
                "description": m.description,
                "run_started_at": m.started_at.isoformat(),
                "run_ended_at": m.ended_at.isoformat() if m.ended_at else None,
                "pipeline_git_sha_at_run": m.pipeline_git_sha,
                "pipeline_version_at_run": m.pipeline_version,
                "stages_completed": list(m.stages_completed),
                "tags": list(m.tags),
            }
        except (ValueError, OSError):
            # Lenient: a malformed manifest shouldn't block the export.
            run_meta = {"run_uuid": data_dir.name, "manifest_read_error": True}
    else:
        run_meta = {"run_uuid": data_dir.name, "bffi_run_json": "missing"}

    return {
        "schema_version": 1,
        "export_created_at": datetime.now(UTC).isoformat(),
        "export_git_sha": _git_sha(),
        "bffi_ontology_version": BFFI_ONTOLOGY_VERSION,
        "output_path": str(output_path),
        "included_files": included_files,
        "skipped_missing": skipped_missing,
        "include_per_record": include_per_record,
        "per_record_count": per_record_count,
        "license": "CC0-1.0",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "run": run_meta,
    }


def _build_readme(
    *,
    run_meta: dict[str, object],
    included_files: list[str],
    per_record_count: int,
) -> str:
    """Compose the export's ``README.md``. One-screen orientation.

    Deliberately terse — receivers should be able to understand what
    they got without reading code. The pipeline repo URL + commit hash
    in the manifest are the audit-trail anchor.
    """
    run_uuid = run_meta.get("run_uuid", "<unknown>")
    description = run_meta.get("description") or ""
    desc_line = f"\n**Description**: {description}\n" if description else ""
    per_record_line = (
        f"\n- `per-record-ttls.tar.gz` — {per_record_count} per-record BFFI Turtles\n"
        if per_record_count
        else ""
    )
    files_md = "\n".join(f"- `{f}`" for f in included_files)
    return (
        f"# BFFI export — run `{run_uuid}`\n"
        "\n"
        "Canonical BFFI Works + Expressions produced by the Helmet "
        "MARCXML → BFFI pipeline, finalised after M9 "
        "(KANTO / VIAF / YSO / KAUNO / MUSO reconciliation).\n"
        f"{desc_line}"
        "\n"
        "## Contents\n"
        "\n"
        f"{files_md}\n"
        f"- `manifest.json` — run UUID, pipeline commit, ontology version, "
        "timestamps\n"
        f"{per_record_line}"
        "\n"
        "## License\n"
        "\n"
        "The RDF data is released into the public domain under "
        "[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/), "
        "matching the licensing convention of the source Finto "
        "vocabularies. The pipeline code that produced this archive is "
        "Apache 2.0; see the pipeline repository.\n"
        "\n"
        "## How to load\n"
        "\n"
        "```bash\n"
        "# Into Apache Jena Fuseki (or any SPARQL 1.1 Graph Store)\n"
        "curl -X PUT --data-binary @canonical.ttl \\\n"
        "  -H 'Content-Type: text/turtle' \\\n"
        "  '<fuseki-url>/data?graph=<your-graph-uri>'\n"
        "```\n"
    )


def _build_per_record_tarball(bffi_dir: Path, out_path: Path) -> int:
    """Bundle every ``bffi_dir/*.ttl`` into a gzip-compressed tarball.

    Returns the count of files included. ``bffi/_validation.jsonl`` /
    ``_validation.tsv`` are excluded — those are pipeline-internal
    audit artifacts, not the data.
    """
    count = 0
    with tarfile.open(out_path, "w:gz") as tar:
        for ttl in sorted(bffi_dir.glob("*.ttl")):
            tar.add(ttl, arcname=f"bffi/{ttl.name}")
            count += 1
    return count


def run(
    *,
    output_path: Path | None = None,
    include_per_record: bool = False,
) -> ExportSummary:
    """Build the BFFI export tarball.

    ``output_path`` defaults to ``<BFFI_DATA_DIR>/bffi-export-<run_uuid>.tar.gz``.
    ``include_per_record`` controls whether ``bffi/*.ttl`` per-record
    Turtles are bundled as a nested archive.

    Writes atomically: composes into a ``.tmp`` file first, ``os.replace``
    on success. A crashed writer leaves the previous export (if any)
    intact.
    """
    settings = get_settings()
    data_dir = settings.data_dir

    emit_if_active(stage="export", event="start")

    run_uuid = settings.run_uuid
    if output_path is None:
        output_path = data_dir / f"bffi-export-{run_uuid}.tar.gz"
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Classify each auto-included file: present → bundle, missing → note.
    included: list[str] = []
    skipped: list[str] = []
    for name in _AUTO_INCLUDED_FILES:
        if (data_dir / name).is_file():
            included.append(name)
        else:
            skipped.append(name)

    if "canonical.ttl" not in included:
        raise FileNotFoundError(
            f"canonical.ttl not found at {data_dir / 'canonical.ttl'!s}; "
            "run M9 (reconcile) — or at minimum M8 (merge) — before exporting."
        )

    # Compose into a temp dir colocated with the final output so the
    # final ``os.replace`` is a same-filesystem rename (atomic).
    with tempfile.TemporaryDirectory(dir=output_path.parent) as staging:
        staging_path = Path(staging)
        per_record_tar = staging_path / "per-record-ttls.tar.gz"
        per_record_count = 0
        if include_per_record:
            bffi_dir = data_dir / "bffi"
            if bffi_dir.is_dir():
                per_record_count = _build_per_record_tarball(bffi_dir, per_record_tar)

        # Manifest needs the per-record count and skipped list, so build it
        # AFTER classification + per-record tarballing.
        manifest_payload = _build_manifest(
            data_dir=data_dir,
            output_path=output_path,
            included_files=included,
            skipped_missing=skipped,
            include_per_record=include_per_record,
            per_record_count=per_record_count,
        )
        manifest_path = staging_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        readme_path = staging_path / "README.md"
        readme_path.write_text(
            _build_readme(
                run_meta=manifest_payload["run"],  # type: ignore[arg-type]
                included_files=included,
                per_record_count=per_record_count,
            ),
            encoding="utf-8",
        )

        tmp_archive = staging_path / "bffi-export.tar.gz.tmp"
        with tarfile.open(tmp_archive, "w:gz") as tar:
            for name in included:
                tar.add(data_dir / name, arcname=name)
            tar.add(manifest_path, arcname="manifest.json")
            tar.add(readme_path, arcname="README.md")
            if per_record_count:
                tar.add(per_record_tar, arcname="per-record-ttls.tar.gz")

        archive_bytes = tmp_archive.stat().st_size
        os.replace(tmp_archive, output_path)

    summary = ExportSummary(
        output_path=output_path,
        included_files=included,
        skipped_missing=skipped,
        per_record_count=per_record_count,
        archive_bytes=archive_bytes,
    )
    emit_if_active(
        stage="export",
        event="end",
        counters={
            "archive_bytes": archive_bytes,
            "included_files": len(included),
            "per_record_count": per_record_count,
        },
        extra={"output_path": str(output_path)},
    )
    return summary
