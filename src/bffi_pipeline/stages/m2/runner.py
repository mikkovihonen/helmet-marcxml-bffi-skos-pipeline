"""Stage M2: MARCXML to BIBFRAME via the LoC ``marc2bibframe2`` XSLT.

For each input file the stage:

1. Validates the filename, encoding, XML syntax, and minimum content
   (Boundary 1 — :mod:`bffi_pipeline.validation.marcxml`).
2. Runs the XSLT to produce raw BIBFRAME RDF/XML, parameterising
   ``baseuri`` so the Work and Instance URIs carry the Helmet bib ID.
3. Post-processes the graph:
   - injects ``bf:identifiedBy`` triples linking every ``bf:Work`` and
     ``bf:Instance`` to the Helmet ``bf:Source`` URI, with the bare
     numeric bib ID as ``rdf:value``;
   - emits a ``bffi-prov:MarcConversion`` Activity (``prov:used`` =
     source filename, ``bffi-prov:helmetBibId``, ``bffi-prov:converterVersion``);
   - links the Work and Instance to the Activity via ``prov:wasGeneratedBy``;
   - mints one ``bffi:AdminMetadata`` block per Work/Instance, populated
     with the M2 initial-stamp fields (see stage M2 and
     spec § 8).
4. Validates the result against ``config/shapes/bibframe-conversion.shape.ttl``
   (Boundary 2 — :mod:`bffi_pipeline.validation.bibframe`).
5. Writes ``<output_dir>/bibframe/<helmet_id>.rdf`` atomically.

A row is appended to ``<output_dir>/helmet-map.jsonl`` for every success
and to ``<output_dir>/bibframe/_errors.jsonl`` for every typed failure.
Re-runs skip files whose output already exists and is newer than the
input; pass ``force=True`` to re-convert.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Final

from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import emit_if_active, get_active_emitter
from bffi_pipeline.validation.bibframe import BibframeShapeError
from bffi_pipeline.validation.marcxml import MarcXmlValidationError

#: P-11 Phase A progress cadence for M2 marc-to-bf.
_M2_PROGRESS_CADENCE: Final[int] = 100
# P-38 Phase D: public dataclasses moved to m2/schemas.py. Re-imported
# here so callsites + tests reaching for them via .runner keep working.
# P-38 Phase D: pre-XSLT MARCXML byte-level repairs moved to
# m2/marcxml_repair.py.
# P-38 Phase D: per-record conversion pipeline moved to m2/convert.py.
from bffi_pipeline.stages.m2.convert import (  # noqa: E402, F401
    _convert_one,
    _is_output_fresh,
    _iter_xml_files,
    _output_path_for,
    _parse_to_graph,
    _run_xslt,
    post_process,
)
from bffi_pipeline.stages.m2.marcxml_repair import (  # noqa: E402, F401
    _MARC_NS,
    _MIN_SPLIT_PARTS,
    _SUBFIELD_TAG,
    _TAGGED_DAGGER_RE,
    _TRAILING_DASH_RE,
    _XML_LANG_ATTR,
    _sanitize_language_tags,
    _sanitize_subfield_separators,
)

# --- Conversion primitives ------------------------------------------------
# P-38 Phase D: provenance + AdminMetadata triple emitters moved to
# m2/provenance.py.
from bffi_pipeline.stages.m2.provenance import (  # noqa: E402, F401
    _BASEURI,
    _HELMET_RECORD_NS,
    _add_admin_metadata_block,
    _add_helmet_identifier,
    _add_marc_conversion_activity,
    _admin_metadata_uri,
    _find_root_resources,
    _utc_now,
)
from bffi_pipeline.stages.m2.schemas import (  # noqa: E402
    ConversionErrorRow,
    ConversionSummary,
    HelmetMapRow,
)

# P-38 Phase D: sidecar JSONL + TSV emitters moved to m2/sidecars.py.
from bffi_pipeline.stages.m2.sidecars import (  # noqa: E402, F401
    _ERRORS_TSV_MESSAGE_MAX,
    _append_jsonl,
    _append_source_review_m2,
    _atomic_write_bytes,
    _dedupe_helmet_map,
    _emit_errors_tsv,
)

# P-38 Phase D: XSLT cache + converter-version helper moved to m2/xslt.py.
from bffi_pipeline.stages.m2.xslt import (  # noqa: E402, F401
    _BFFI_PIPELINE_REPO_ROOT,
    _MARC2BIBFRAME2_DIR,
    _XSLT_PATH,
    _xslt,
    marc2bibframe2_version,
)


def run(
    input_dir: Path,
    *,
    output_dir: Path | None = None,
    force: bool = False,
) -> ConversionSummary:
    """Convert every ``*.xml`` file in ``input_dir`` and return a summary."""
    output_dir = output_dir or get_settings().data_dir
    summary = ConversionSummary()
    helmet_map_path = output_dir / "helmet-map.jsonl"
    errors_path = output_dir / "bibframe" / "_errors.jsonl"

    # P-12 Option B: include the active run_uuid on every error row so
    # the exporter's error-tail loop can attribute each typed failure
    # to its originating pipeline invocation. Empty when no emitter is
    # active (e.g. tests).
    emitter = get_active_emitter()
    run_uuid = emitter.run_uuid if emitter is not None else ""

    xml_files = list(_iter_xml_files(input_dir))
    emit_if_active(
        stage="m2",
        event="start",
        counters={"total": len(xml_files)},
    )

    for i, xml_path in enumerate(xml_files, start=1):
        if i % _M2_PROGRESS_CADENCE == 0:
            emit_if_active(
                stage="m2",
                event="progress",
                counters={"processed": i, "total": len(xml_files)},
                extra={
                    "succeeded": len(summary.succeeded),
                    "skipped": len(summary.skipped_idempotent),
                    "failed": len(summary.failed),
                },
            )
        try:
            map_row, status = _convert_one(xml_path, output_dir, force=force)
        except MarcXmlValidationError as exc:
            row = ConversionErrorRow(
                helmet_bib_id=xml_path.stem if xml_path.stem.isdigit() else None,
                filename=xml_path.name,
                error_type=exc.error_type,
                message=exc.message,
                run_uuid=run_uuid,
            )
            summary.failed.append(row)
            _append_jsonl(errors_path, asdict(row))
            _append_source_review_m2(row)
            continue
        except BibframeShapeError as exc:
            row = ConversionErrorRow(
                helmet_bib_id=xml_path.stem if xml_path.stem.isdigit() else None,
                filename=xml_path.name,
                error_type="bibframe-shape",
                message=exc.message,
                run_uuid=run_uuid,
            )
            summary.failed.append(row)
            _append_jsonl(
                errors_path,
                {**asdict(row), "report_text": exc.report_text},
            )
            _append_source_review_m2(row)
            continue
        except Exception as exc:
            row = ConversionErrorRow(
                helmet_bib_id=xml_path.stem if xml_path.stem.isdigit() else None,
                filename=xml_path.name,
                error_type="bibframe-conversion",
                message=str(exc),
                run_uuid=run_uuid,
            )
            summary.failed.append(row)
            _append_jsonl(errors_path, asdict(row))
            _append_source_review_m2(row)
            continue

        if status == "skipped":
            summary.skipped_idempotent.append(xml_path.name)
            continue

        assert map_row is not None  # status=='ok' implies a map row
        summary.succeeded.append(xml_path.name)
        _append_jsonl(helmet_map_path, asdict(map_row))

    _dedupe_helmet_map(helmet_map_path)
    # Cataloguer-facing TSV companion to ``bibframe/_errors.jsonl``.
    # Always written (even header-only on a clean run) so cataloguer
    # workflows wired to the artifact path don't need a missing-file
    # guard. Mirrors the M8 mint-failures TSV pattern.
    errors_tsv_path = output_dir / "bibframe" / "_errors.tsv"
    _emit_errors_tsv(errors_tsv_path, summary.failed)
    emit_if_active(
        stage="m2",
        event="end",
        counters={
            "total": len(xml_files),
            "succeeded": len(summary.succeeded),
            "skipped": len(summary.skipped_idempotent),
            "failed": len(summary.failed),
        },
    )
    return summary


__all__ = [
    "ConversionErrorRow",
    "ConversionSummary",
    "HelmetMapRow",
    "marc2bibframe2_version",
    "post_process",
    "run",
]
