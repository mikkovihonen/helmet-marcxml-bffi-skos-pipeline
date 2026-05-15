"""Stage M3: BIBFRAME to BFFI Work + Expression.

Runs the two CONSTRUCTs in ``sparql/`` against each ``<output_dir>/bibframe/<id>.rdf``,
combines them, post-processes ``skos:prefLabel`` with language tags derived
from ``bf:language``, validates against ``config/shapes/bffi.shape.ttl``
(Boundary 3 — *non-blocking*), and writes a Turtle file per record.

SHACL failures do not halt the pipeline — counts and per-record
validation reports go to ``<output_dir>/bffi/_validation.jsonl``;
the CLI prints a summary warning.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Final

from bffi_pipeline.cataloguer_review import append_source_row
from bffi_pipeline.config import get_settings
from bffi_pipeline.contrib_variants import (
    DEFAULT_SIDECAR_NAME,
    truncate_sidecar,
)
from bffi_pipeline.observability.events import emit_if_active, get_active_emitter
from bffi_pipeline.validation.bffi import validate_graph

_BFFI_PIPELINE_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_SPARQL_DIR: Final[Path] = _BFFI_PIPELINE_REPO_ROOT / "sparql"

#: P-11 Phase A progress cadence for M3 bf-to-bffi.
_M3_PROGRESS_CADENCE: Final[int] = 100

#: P-19 Phase A — concatenated BFFI corpus written next to the per-
#: record ``bffi/`` dir so M8's load phase reads one stream instead of
#: 800 k individual files. M8 keeps the same filename constant —
#: stages don't import each other per CLAUDE.md "Stage isolation".
BFFI_CORPUS_FILENAME: Final[str] = "bffi-corpus.ttl"

# P-38 Phase B: language_detect.py, sanitize.py, and contributions.py
# were carved out of this file. Re-imported here so:
#   1. callsites within this module find the helpers they call into;
#   2. tests that reach for private symbols via `bffi_pipeline.stages.m3
#      .runner` (the pre-split path) keep working bit-identically.
# F401 noqa: ruff doesn't know `_is_parseable_date` etc. are reachable
# from this module's namespace by external callers.
# P-38 Phase D: SPARQL CONSTRUCT runner moved to m3/construct.py.
from bffi_pipeline.stages.m3.construct import (  # noqa: E402, F401
    _expression_query,
    _work_query,
    construct_bffi,
)
from bffi_pipeline.stages.m3.contributions import (  # noqa: E402, F401
    _emit_extracted_contributions,
    _read_helmet_bib_id,
)

# P-38 Phase D: per-record conversion pipeline + corpus concat moved to
# m3/convert.py.
from bffi_pipeline.stages.m3.convert import (  # noqa: E402, F401
    _append_jsonl,
    _atomic_write_bytes,
    _convert_one,
    _is_output_fresh,
    _iter_bibframe_files,
    _write_bffi_corpus,
)
from bffi_pipeline.stages.m3.language_detect import (  # noqa: E402, F401
    _DETECTABLE_LANGS,
    _LANG_3_TO_2,
    _LANG_URI_PREFIX,
    _RDA_PARALLEL_SEPARATORS,
    SKOS_prefLabel,
    _candidate_languages,
    _retag_pref_labels,
)

# P-38 Phase D: graph post-processing moved to m3/post_process.py.
from bffi_pipeline.stages.m3.post_process import post_process  # noqa: E402
from bffi_pipeline.stages.m3.sanitize import (  # noqa: E402, F401
    _DATE_DATATYPES,
    _DATE_VALIDATORS,
    _GYEAR_LENGTH,
    _GYEAR_MONTH_LENGTH,
    _MAX_MONTH,
    _WHITESPACE_PERCENT_ENCODE,
    _XSD_DATE,
    _XSD_DATETIME,
    _XSD_GYEAR,
    _XSD_GYEAR_MONTH,
    _date_is_valid,
    _datetime_is_valid,
    _gyear_is_valid,
    _gyear_month_is_valid,
    _is_parseable_date,
    _sanitize_date_literals,
    _sanitize_uri,
    _sanitize_uri_whitespace,
)

# P-38 Phase D: public dataclasses moved to m3/schemas.py.
from bffi_pipeline.stages.m3.schemas import (  # noqa: E402
    BffiSummary,
    ValidationRow,
)

# P-38 Phase D: SHACL-validation TSV emitter moved to m3/validation_emit.py.
from bffi_pipeline.stages.m3.validation_emit import (  # noqa: E402, F401
    _SH_MESSAGE_RE,
    _VALIDATION_TSV_MESSAGE_MAX,
    _emit_validation_tsv,
    _extract_shape_messages,
)


def run(
    bibframe_dir: Path | None = None,
    *,
    output_dir: Path | None = None,
    force: bool = False,
    llm_detector: object | None = None,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> BffiSummary:
    """Convert every ``<bibframe_dir>/<id>.rdf`` to a BFFI Turtle file.

    Pass ``llm_detector`` (a
    :class:`bffi_pipeline.title_lang_llm.TitleLangDetector`) to enable
    the title-language cascade. Pass ``contrib_extractor`` (a
    :class:`bffi_pipeline.contrib_extract_llm.ContribExtractor`) to
    enable 245$c contributor extraction. Without either, M3 stays
    graph-only.

    ``variants_sidecar_path`` defaults to
    ``<output_dir>/contrib-variants.jsonl`` and is the F2 sidecar
    where the contributor cascade persists transliteration claims.
    On ``force=True`` the sidecar is truncated at the start of the
    run so cascade re-runs don't accumulate stale rows.
    """
    base = output_dir or get_settings().data_dir
    bibframe_dir = bibframe_dir or (base / "bibframe")
    summary = BffiSummary()
    validation_path = base / "bffi" / "_validation.jsonl"
    sidecar_path = variants_sidecar_path or (base / DEFAULT_SIDECAR_NAME)
    if force:
        truncate_sidecar(sidecar_path)

    # P-12 Option B: include the active run_uuid on every validation
    # row so the exporter's error-tail loop attributes Boundary-3
    # failures to their originating pipeline invocation. Empty when
    # no emitter is active (tests).
    emitter = get_active_emitter()
    run_uuid = emitter.run_uuid if emitter is not None else ""

    rdf_files = list(_iter_bibframe_files(bibframe_dir))
    # Accumulate the full ValidationRow per failing record so the
    # cataloguer-facing TSV at run end can re-emit them in a
    # spreadsheet-shaped form. The JSONL writer below still appends
    # row-by-row (established M3 pattern); the in-memory list is the
    # source of truth for the TSV emit only.
    validation_rows: list[ValidationRow] = []
    emit_if_active(
        stage="m3",
        event="start",
        counters={"total": len(rdf_files)},
    )

    for i, rdf_path in enumerate(rdf_files, start=1):
        if i % _M3_PROGRESS_CADENCE == 0:
            emit_if_active(
                stage="m3",
                event="progress",
                counters={"processed": i, "total": len(rdf_files)},
                extra={
                    "converted": len(summary.converted),
                    "skipped": len(summary.skipped_idempotent),
                    "errored": len(summary.errored),
                    "failed_shape": len(summary.failed_shape),
                },
            )
        bib_id = rdf_path.stem
        out_path = base / "bffi" / f"{bib_id}.ttl"
        if not force and _is_output_fresh(rdf_path, out_path):
            summary.skipped_idempotent.append(bib_id)
            continue

        try:
            graph = _convert_one(
                rdf_path,
                out_path,
                llm_detector=llm_detector,
                contrib_extractor=contrib_extractor,
                variants_sidecar_path=sidecar_path,
                now=now,
            )
        except Exception as exc:
            summary.errored.append((bib_id, str(exc)))
            continue

        report = validate_graph(graph)
        if not report.conforms:
            summary.failed_shape.append(bib_id)
            row = ValidationRow(
                helmet_bib_id=bib_id,
                output_file=str(out_path.name),
                conforms=False,
                report_text=report.text,
                run_uuid=run_uuid,
            )
            validation_rows.append(row)
            _append_jsonl(validation_path, asdict(row))
            # P-31 Phase B: also mirror into the unified source-review
            # TSV. `details` is the SHACL message extract (matches the
            # per-stage TSV column), `severity` is warning because
            # Boundary-3 failures don't block the bib_id from making
            # it through M3 — the per-record `.ttl` was written.
            append_source_row(
                bib_id=bib_id,
                stage="m3",
                severity="warning",
                details=_extract_shape_messages(report.text),
            )
        summary.converted.append(bib_id)

    # P-19 Phase A: write the concatenated BFFI corpus so M8's load
    # phase doesn't open every per-record file individually. Skips
    # when the existing concat is already fresh.
    _write_bffi_corpus(base / "bffi", base / BFFI_CORPUS_FILENAME)

    # Cataloguer-facing TSV companion to ``bffi/_validation.jsonl``.
    # Always written (header-only on clean runs) so cataloguer
    # workflows wired to the artifact path don't need a missing-file
    # guard. Mirrors the M2 ``bibframe/_errors.tsv`` + M8
    # ``canonical-mint-failures.tsv`` conventions.
    validation_tsv_path = base / "bffi" / "_validation.tsv"
    _emit_validation_tsv(validation_tsv_path, validation_rows)

    emit_if_active(
        stage="m3",
        event="end",
        counters={
            "total": len(rdf_files),
            "converted": len(summary.converted),
            "skipped": len(summary.skipped_idempotent),
            "errored": len(summary.errored),
            "failed_shape": len(summary.failed_shape),
        },
    )
    return summary


__all__ = [
    "BFFI_CORPUS_FILENAME",
    "BffiSummary",
    "ValidationRow",
    "construct_bffi",
    "post_process",
    "run",
]
