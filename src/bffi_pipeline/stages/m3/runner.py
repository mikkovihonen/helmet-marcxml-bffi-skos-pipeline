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

import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Final, cast

from rdflib import Graph
from rdflib.namespace import DCTERMS, RDF, RDFS
from rdflib.term import Node

from bffi_pipeline.cataloguer_review import append_source_row
from bffi_pipeline.config import get_settings
from bffi_pipeline.contrib_variants import (
    DEFAULT_SIDECAR_NAME,
    truncate_sidecar,
)
from bffi_pipeline.observability.events import emit_if_active, get_active_emitter
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.uris import (
    register_sparql_functions,
)
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
from bffi_pipeline.stages.m3.contributions import (  # noqa: E402, F401
    _emit_extracted_contributions,
    _read_helmet_bib_id,
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

# --- Public dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class ValidationRow:
    """One row of ``_validation.jsonl`` per (Boundary-3-failing) record.

    ``run_uuid`` is populated from the active observability emitter
    so the exporter's error-tail loop (P-12 Option B) can attribute
    each row to its originating pipeline invocation. Empty string
    when no emitter is active (e.g. unit tests that bypass the CLI
    bootstrap) — rows surface under ``run_uuid=""`` in metrics.
    """

    helmet_bib_id: str
    output_file: str
    conforms: bool
    report_text: str
    run_uuid: str = ""


#: Truncate over-long rendered messages in the validation TSV so a
#: spreadsheet stays readable. The full multi-line report lives in
#: the JSONL for forensic lookup; the TSV is for cataloguer triage.
_VALIDATION_TSV_MESSAGE_MAX: Final[int] = 240

#: Regex to extract every ``sh:message Literal("…")`` clause from
#: rdflib's SHACL report serialization. The message text is the
#: cataloguer-actionable bit; the rest of the report is rdflib
#: boilerplate (severity, source shape, focus node etc.) that's only
#: useful for pipeline-team debugging.
_SH_MESSAGE_RE: Final[re.Pattern[str]] = re.compile(
    r'sh:message\s+Literal\(\s*"([^"\\]*(?:\\.[^"\\]*)*)"', re.DOTALL
)


def _extract_shape_messages(report_text: str) -> str:
    """Pull every ``sh:message Literal("…")`` out of a SHACL report.

    Joined with ``" | "`` when a single record has multiple
    violations. Falls back to the full report (with control chars
    collapsed) when no messages can be extracted — better to surface
    the rdflib boilerplate than an empty cell.
    """
    matches = _SH_MESSAGE_RE.findall(report_text)
    if not matches:
        return " ".join(report_text.replace("\t", " ").split())
    return " | ".join(m.strip() for m in matches)


def _emit_validation_tsv(path: Path, rows: list[ValidationRow]) -> None:
    """Cataloguer-facing TSV companion to ``bffi/_validation.jsonl``.

    Three columns the cataloguer can open in Excel / Sheets / Numbers
    and act on without parsing JSON:

    - ``helmet_bib_id`` — lookup key in Helmet / Sierra.
    - ``shape_message`` — the human-readable ``sh:message`` text
      extracted from the SHACL report (e.g. ``"bffi:Work must have
      skos:prefLabel in fi/sv/en."``). Multiple violations on one
      record are joined with ``" | "``. This is the
      cataloguer-actionable column.
    - ``output_file`` — the BFFI Turtle file the failed shape was
      validated against (e.g. ``b1234.ttl``); pipeline-team
      cross-reference. The full multi-line rdflib SHACL report
      stays in the JSONL companion.

    Messages over 240 chars are truncated with an ellipsis to keep
    spreadsheet rendering readable. Full report stays in JSONL.

    Always emitted — even when every record passed Boundary-3, a
    header-only TSV is written so cataloguer workflows wired to the
    artifact path don't need a missing-file guard.

    Sorted by ``helmet_bib_id`` for stable diffs across re-runs.
    Atomic write via ``.tmp`` + ``replace``.

    Mirrors the M2 ``bibframe/_errors.tsv`` + the M8
    ``canonical-mint-failures.tsv`` conventions so cataloguers see a
    consistent artifact shape across stages.
    """
    header = "helmet_bib_id\tshape_message\toutput_file\n"
    out_rows: list[tuple[str, str, str]] = []
    for row in rows:
        message = _extract_shape_messages(row.report_text)
        message_clean = " ".join(message.replace("\t", " ").split())
        if len(message_clean) > _VALIDATION_TSV_MESSAGE_MAX:
            message_clean = message_clean[: _VALIDATION_TSV_MESSAGE_MAX - 1] + "…"
        out_rows.append((row.helmet_bib_id, message_clean, row.output_file))
    out_rows.sort()
    body = "".join(f"{bib_id}\t{msg}\t{output_file}\n" for bib_id, msg, output_file in out_rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)


@dataclass
class BffiSummary:
    """Aggregate counts for an end-of-run report."""

    converted: list[str] = field(default_factory=list)
    skipped_idempotent: list[str] = field(default_factory=list)
    failed_shape: list[str] = field(default_factory=list)
    errored: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Total number of input files seen, excluding shape-only flags."""
        return len(self.converted) + len(self.skipped_idempotent) + len(self.errored)

    def render(self) -> str:
        """Format this summary as paste-ready text for the bf-to-bffi CLI."""
        lines = [
            f"BIBFRAME to BFFI conversion summary ({self.total} input file(s))",
            f"  converted: {len(self.converted)}",
            f"  skipped (already converted): {len(self.skipped_idempotent)}",
            f"  shape-failing (kept; flagged): {len(self.failed_shape)}",
            f"  errored: {len(self.errored)}",
        ]
        if self.failed_shape:
            lines.append("Shape-failing records:")
            lines.extend(f"  - {bib}" for bib in self.failed_shape)
        if self.errored:
            lines.append("Hard errors (record skipped):")
            lines.extend(f"  - {bib}: {msg}" for bib, msg in self.errored)
        return "\n".join(lines)


# --- Caching --------------------------------------------------------------


@lru_cache(maxsize=1)
def _work_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_work.rq").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _expression_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_expression.rq").read_text(encoding="utf-8")


# --- CONSTRUCT runner -----------------------------------------------------


def construct_bffi(source: Graph) -> Graph:
    """Run both CONSTRUCT passes against ``source`` and merge into one graph."""
    register_sparql_functions()
    out = Graph()
    for query in (_work_query(), _expression_query()):
        result = source.query(query)
        for triple in cast("Iterable[tuple[Node, Node, Node]]", result):
            out.add(triple)
    return out


# --- Post-processing ------------------------------------------------------


def post_process(
    bffi_graph: Graph,
    source: Graph,
    *,
    llm_detector: object | None = None,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> Graph:
    """Mutate ``bffi_graph`` in place: tag prefLabels, denormalise Helmet
    identifiers for Skosmos display, optionally extract 245$c
    contributors, bind namespaces.

    ``llm_detector`` enables the M3 title-language cascade;
    ``contrib_extractor`` enables the M3 245$c contributor-extraction
    cascade. Either / both can be ``None`` to keep that stage
    graph-only. ``variants_sidecar_path`` is where the cascade
    appends one row per detected transliteration variant; M8's
    binding pass reads the same file.
    """
    candidates = _candidate_languages(source)
    if candidates:
        _retag_pref_labels(bffi_graph, candidates, llm_detector=llm_detector)
    _emit_extracted_contributions(
        bffi_graph,
        source,
        contrib_extractor=contrib_extractor,
        variants_sidecar_path=variants_sidecar_path,
        now=now,
    )
    bffi_graph.bind("bf", V.BF)
    bffi_graph.bind("bffi", V.BFFI)
    bffi_graph.bind("bib", V.BIB)
    bffi_graph.bind("dct", DCTERMS)
    bffi_graph.bind("rdf", RDF)
    bffi_graph.bind("rdfs", RDFS)
    bffi_graph.bind("skos", V.SKOS)
    return bffi_graph


# --- Driver ---------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _is_output_fresh(input_path: Path, output_path: Path) -> bool:
    return output_path.exists() and output_path.stat().st_mtime >= input_path.stat().st_mtime


def _iter_bibframe_files(bibframe_dir: Path) -> Iterator[Path]:
    yield from sorted(p for p in bibframe_dir.glob("*.rdf") if not p.name.startswith("_"))


def _write_bffi_corpus(bffi_dir: Path, corpus_path: Path) -> int:
    """Concatenate every per-record BFFI Turtle into a single corpus file.

    M8's load (~8 min on 20 k bench, projected ~5.5 h on the 800 k
    corpus) is dominated by per-file ``open`` + parser-init overhead,
    not by graph size. Layering one ``bffi-corpus.ttl`` stream over
    the per-record store collapses ``len(bffi/*.ttl)`` opens into one
    on M8's side. P-19 Phase A.

    Idempotent: skip when the existing concat is at least as new as
    every per-record ``.ttl``. The per-record layout stays canonical
    — the concat is a derived view.

    ``@prefix`` declarations are deduplicated (single block at the
    top, per-record headers stripped) to avoid a multi-millionfold
    redeclaration that rdflib's parser would walk on a full-corpus
    parse. Returns the number of per-record files concatenated, or
    ``0`` when the concat was skipped or no input files existed.
    """
    if not bffi_dir.is_dir():
        return 0
    per_record = sorted(bffi_dir.glob("*.ttl"))
    if not per_record:
        return 0
    if corpus_path.is_file():
        corpus_mtime = corpus_path.stat().st_mtime
        if all(p.stat().st_mtime <= corpus_mtime for p in per_record):
            return 0

    seen_prefixes: set[str] = set()
    prefix_lines: list[str] = []
    body_chunks: list[str] = []
    for path in per_record:
        with path.open("r", encoding="utf-8") as fh:
            body_lines: list[str] = []
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("@prefix") or stripped.startswith("@base"):
                    if stripped not in seen_prefixes:
                        seen_prefixes.add(stripped)
                        prefix_lines.append(line.rstrip("\n"))
                    continue
                body_lines.append(line)
            body_chunks.append("".join(body_lines).rstrip("\n"))

    tmp = corpus_path.with_suffix(corpus_path.suffix + ".tmp")
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as fh:
        for line in prefix_lines:
            fh.write(line + "\n")
        fh.write("\n")
        for chunk in body_chunks:
            if not chunk.strip():
                continue
            fh.write(chunk)
            fh.write("\n\n")
    os.replace(tmp, corpus_path)
    return len(per_record)


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _convert_one(
    input_path: Path,
    output_path: Path,
    *,
    llm_detector: object | None = None,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> Graph:
    source = Graph()
    source.parse(str(input_path), format="xml")
    # Cataloguer $0 values occasionally carry stray whitespace that
    # marc2bibframe2 passes through unchanged; rdflib refuses to
    # serialize those as Turtle and the whole record's M3 conversion
    # would fail hard. Sanitize the parsed source so the CONSTRUCT
    # pass sees clean URIs.
    _sanitize_uri_whitespace(source)
    # Cataloguer-supplied date placeholders (e.g. ``"19  -  -  T00:00:00"``
    # for "year not yet entered") parse as xsd:dateTime in
    # marc2bibframe2's output but raise ValueError when rdflib tries
    # to coerce them at downstream load. Drop the datatype tag so the
    # literal survives as plain text rather than crashing the merge.
    _sanitize_date_literals(source)
    bffi_graph = construct_bffi(source)
    post_process(
        bffi_graph,
        source,
        llm_detector=llm_detector,
        contrib_extractor=contrib_extractor,
        variants_sidecar_path=variants_sidecar_path,
        now=now,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(output_path, bffi_graph.serialize(format="turtle").encode("utf-8"))
    return bffi_graph


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
