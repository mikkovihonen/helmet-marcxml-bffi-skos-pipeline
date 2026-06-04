"""Zip-bundle build / read / import for cataloguer review.

This module owns the stage-agnostic infrastructure:

- :func:`build_bundle` — assemble a review zip from a list of stage
  selections. For each selected stage it calls the stage handler's
  ``sample`` to produce per-stage ``candidates.jsonl``, then looks up
  each candidate's MARCXML in the source directory and packs the
  found files as ``<stage>/marc/<bib>.xml`` sidecars. Bibs missing
  from the source dir are recorded in ``manifest.json``'s
  ``marc_missing`` so the HTML can render an inline "no MARC available"
  stub for those rows.
- :func:`read_bundle` — parse a bundle (manifest + raw file map) for
  inspection (smoke tests, debugging). The HTML does the equivalent
  client-side via fflate.
- :func:`import_results` — read a cataloguer's results zip, dispatch
  each ``<stage>/results.jsonl`` to its handler's ``import_results``,
  and return a per-stage summary.

The bundle format is documented in :mod:`__init__`'s module docstring.
The Python side and the HTML side both round-trip the same on-wire
format — :func:`build_bundle` writes what the HTML reads; the HTML
exports what :func:`import_results` reads.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from bffi_pipeline.eval.review_bundle.manifest import (
    MANIFEST_SCHEMA,
    Manifest,
    StageEntry,
)
from bffi_pipeline.eval.review_bundle.stages import STAGE_REGISTRY, StageHandler

#: Default Helmet MARCXML directory on the operator's machine. The
#: bundle builder reads ``<bib>.xml`` from here per candidate row.
DEFAULT_HELMET_MARC_DIR: Final[Path] = Path(
    "/Users/mikkovihonen/Workspace/helmet-sierra-data-tools/output/marcxml"
)

#: Source of truth for the HTML reviewer. The cataloguer-bundle stage
#: and the ``review-bundle-build --from-run`` CLI both copy this file
#: alongside the produced bundle so the run artefacts are
#: self-contained — operator emails the run dir (or a zip of it) and
#: the cataloguer gets HTML + bundle in one place.
HTML_REVIEWER_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "gold" / "cataloguer-review.html"
)


class BundleBuildError(RuntimeError):
    """Raised when the bundle builder cannot satisfy the requested
    stages (unknown stage id, pool file missing, etc.)."""


class BundleReadError(RuntimeError):
    """Raised when a bundle's contents don't match the manifest
    (missing ``candidates.jsonl``, unparseable ``manifest.json``)."""


@dataclass
class BundleResult:
    """Operator-facing result of :func:`build_bundle`."""

    output_path: Path
    stages: list[StageEntry] = field(default_factory=list)
    per_stage_histograms: dict[str, dict[str, int]] = field(default_factory=dict)
    per_stage_marc_attached: dict[str, int] = field(default_factory=dict)
    per_stage_marc_missing: dict[str, list[str]] = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"review-bundle-build summary → {self.output_path}"]
        for stage in self.stages:
            lines.append(f"  [{stage.id}]  schema={stage.schema_}")
            hist = self.per_stage_histograms.get(stage.id, {})
            if hist:
                for cat, n in sorted(hist.items()):
                    lines.append(f"    sampled {cat:35} {n}")
            attached = self.per_stage_marc_attached.get(stage.id, 0)
            missing = self.per_stage_marc_missing.get(stage.id, [])
            if stage.marc_dir is not None:
                lines.append(f"    MARC sidecars attached: {attached}")
                if missing:
                    preview = ", ".join(missing[:5])
                    extra = len(missing) - 5
                    more = "" if extra <= 0 else f" (and {extra} more)"
                    lines.append(f"    MARC missing for {len(missing)} bib(s): {preview}{more}")
        return "\n".join(lines)


@dataclass
class ImportDispatchResult:
    """Operator-facing result of :func:`import_results` over a zip.

    ``per_stage_summaries`` maps each stage id to whatever its handler's
    ``import_results`` returned (typically an
    :class:`~contrib.ImportSummary` carrying its own ``.render()``).
    ``unknown_stages`` lists schema ids found in the zip that no
    handler is registered for — the operator gets a clear "you need a
    newer pipeline to import this" message rather than silently
    dropping their cataloguer's work.
    """

    per_stage_summaries: dict[str, Any] = field(default_factory=dict)
    unknown_stages: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = ["review-bundle-import summary"]
        for stage_id, summary in self.per_stage_summaries.items():
            lines.append(f"  --- {stage_id} ---")
            renderer = getattr(summary, "render", None)
            text = renderer() if callable(renderer) else str(summary)
            lines.extend(f"  {ln}" for ln in text.splitlines())
        if self.unknown_stages:
            lines.append("  --- unknown stages ---")
            for schema in self.unknown_stages:
                lines.append(f"    {schema} — no handler registered (need a newer pipeline)")
        return "\n".join(lines)


def _resolve_handler(schema: str) -> StageHandler:
    handler = STAGE_REGISTRY.get(schema)
    if handler is None:
        registered = ", ".join(sorted(STAGE_REGISTRY)) or "(none)"
        raise BundleBuildError(
            f"No stage handler registered for schema {schema!r}. Registered: {registered}"
        )
    return handler


# --- Build ---------------------------------------------------------------


def build_bundle(
    *,
    output_path: Path,
    operator: str,
    stage_schemas: list[str] | None = None,
    per_category: int = 25,
    seed: str | None = None,
    marc_dir: Path = DEFAULT_HELMET_MARC_DIR,
    pool_overrides: dict[str, Path] | None = None,
) -> BundleResult:
    """Assemble a review bundle zip.

    ``stage_schemas`` is the list of stage schema ids to include
    (default: every registered stage). For each one, the handler's
    ``sample`` runs against its ``default_pool_path`` (or
    ``pool_overrides[schema]`` if set), the candidates land at
    ``<stage_id>/candidates.jsonl`` inside the zip, and each row's
    MARCXML is read from ``marc_dir`` and packed at
    ``<stage_id>/marc/<bib>.xml``. Missing MARCs are recorded in
    ``manifest.json``'s ``marc_missing`` so the HTML can render a
    "no MARC available" stub without guessing.

    Returns a :class:`BundleResult` for the operator. The zip is
    written atomically (temp file + rename) per the project
    idempotency convention.
    """
    # Preserve registry insertion order (pipeline-stage order: M2 → M3
    # → M6 → M9) rather than sorting alphabetically, so the HTML
    # reviewer's tabs land in logical chain order regardless of which
    # path built the bundle (in-chain dispatcher vs. operator CLI).
    schemas = stage_schemas if stage_schemas else list(STAGE_REGISTRY)
    pool_overrides = pool_overrides or {}

    stage_entries: list[StageEntry] = []
    histograms: dict[str, dict[str, int]] = {}
    marc_attached: dict[str, int] = {}
    marc_missing: dict[str, list[str]] = {}
    file_map: dict[str, bytes] = {}

    for schema in schemas:
        handler = _resolve_handler(schema)
        pool = pool_overrides.get(schema) or handler.default_pool_path
        if pool is None:
            raise BundleBuildError(
                f"Stage {schema!r}: no default pool path and no override given. "
                "Per-run stages (e.g. judge) need the cataloguer-bundle dispatcher "
                "or --from-run to supply the run's per-stage audit log path."
            )
        if not pool.exists():
            raise BundleBuildError(
                f"Stage {schema!r}: pool file not found at {pool}. "
                "Pass an override or run the producing stage first."
            )

        candidates_in_zip = f"{handler.stage_id}/candidates.jsonl"
        marc_dir_in_zip = f"{handler.stage_id}/marc/"

        # Sample → write candidates to a tmp path, read bytes, drop the tmp.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            hist = handler.sample(
                input_path=pool,
                output_path=tmp_path,
                per_category=per_category,
                seed=seed,
            )
            candidates_bytes = tmp_path.read_bytes()
        finally:
            tmp_path.unlink(missing_ok=True)

        file_map[candidates_in_zip] = candidates_bytes
        histograms[handler.stage_id] = hist

        # MARC sidecars.
        bib_ids = handler.bib_ids_for_marc(candidates_bytes)
        attached_count = 0
        missing_for_stage: list[str] = []
        seen_bibs: set[str] = set()
        for bib in bib_ids:
            if bib in seen_bibs:
                continue
            seen_bibs.add(bib)
            marc_path = marc_dir / f"{bib}.xml"
            if marc_path.exists():
                file_map[f"{marc_dir_in_zip}{bib}.xml"] = marc_path.read_bytes()
                attached_count += 1
            else:
                missing_for_stage.append(bib)

        marc_attached[handler.stage_id] = attached_count
        marc_missing[handler.stage_id] = missing_for_stage

        stage_entries.append(
            StageEntry(
                id=handler.stage_id,
                label=handler.label,
                candidates=candidates_in_zip,
                marc_dir=marc_dir_in_zip,
                schema=schema,
                marc_missing=missing_for_stage,
            )
        )

    manifest = Manifest(
        schema=MANIFEST_SCHEMA,
        created=datetime.now(UTC).isoformat(),
        operator=operator,
        stages=stage_entries,
    )
    file_map["manifest.json"] = json.dumps(
        manifest.model_dump(by_alias=True), ensure_ascii=False, indent=2
    ).encode("utf-8")

    # Atomic write: temp file + rename.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = output_path.with_suffix(output_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname in sorted(file_map):
            zf.writestr(arcname, file_map[arcname])
    tmp_out.replace(output_path)

    return BundleResult(
        output_path=output_path,
        stages=stage_entries,
        per_stage_histograms=histograms,
        per_stage_marc_attached=marc_attached,
        per_stage_marc_missing=marc_missing,
    )


# --- Read (smoke / debug) -----------------------------------------------


def read_bundle(path: Path) -> tuple[Manifest, dict[str, bytes]]:
    """Load a bundle from disk. Returns the parsed manifest + a
    ``{arcname: bytes}`` view of every file in the zip.

    Used by tests and the operator-side import. The HTML does the
    equivalent client-side via fflate.
    """
    if not path.exists():
        raise BundleReadError(f"Bundle not found at {path}")
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            files[info.filename] = zf.read(info)
    if "manifest.json" not in files:
        raise BundleReadError(f"Bundle at {path} is missing manifest.json")
    try:
        manifest_json = json.loads(files["manifest.json"].decode("utf-8"))
    except json.JSONDecodeError as e:
        raise BundleReadError(f"manifest.json is not valid JSON: {e}") from e
    manifest = Manifest.model_validate(manifest_json)
    return manifest, files


# --- Import -------------------------------------------------------------


def import_results(
    *,
    input_path: Path,
    gold_overrides: dict[str, Path] | None = None,
) -> ImportDispatchResult:
    """Import a cataloguer's results zip.

    The input zip mirrors the build-side bundle layout but carries
    ``<stage>/results.jsonl`` files instead of ``<stage>/candidates.jsonl``.
    For each stage entry in the manifest:

    - If the schema is registered in :data:`STAGE_REGISTRY`, the
      handler's ``import_results`` is called with the per-stage
      results.jsonl and the stage's gold file
      (``gold_overrides[schema]`` if set, otherwise the handler's
      ``default_gold_path``).
    - If the schema is unknown, the stage is added to
      ``unknown_stages`` and skipped — the known stages still get
      imported.
    """
    gold_overrides = gold_overrides or {}
    manifest, files = read_bundle(input_path)

    dispatch = ImportDispatchResult()

    for stage in manifest.stages:
        results_path_in_zip = stage.candidates.replace("candidates.jsonl", "results.jsonl")
        if results_path_in_zip not in files:
            raise BundleReadError(
                f"Stage {stage.id!r}: expected {results_path_in_zip} in the zip, not found."
            )
        handler = STAGE_REGISTRY.get(stage.schema_)
        if handler is None:
            dispatch.unknown_stages.append(stage.schema_)
            continue

        gold_path = gold_overrides.get(stage.schema_, handler.default_gold_path)

        # The handler reads from a Path — extract the results to a tmp file.
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".jsonl", delete=False) as tmp:
            tmp.write(files[results_path_in_zip])
            tmp_path = Path(tmp.name)
        try:
            summary = handler.import_results(input_path=tmp_path, output_path=gold_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        dispatch.per_stage_summaries[stage.id] = summary

    return dispatch
