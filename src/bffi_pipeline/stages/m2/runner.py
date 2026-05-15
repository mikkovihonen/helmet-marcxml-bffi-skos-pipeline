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

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from lxml import etree
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from bffi_pipeline import __version__ as PIPELINE_VERSION
from bffi_pipeline.cataloguer_review import append_source_row
from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import emit_if_active, get_active_emitter
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.validation.bibframe import BibframeShapeError, assert_conforms
from bffi_pipeline.validation.marcxml import MarcXmlValidationError, validate

_BASEURI: Final[str] = "http://urn.fi/URN:NBN:fi:bib:raw/"
_HELMET_RECORD_NS: Final[str] = "http://urn.fi/URN:NBN:fi:bib:helmet/"

#: P-11 Phase A progress cadence for M2 marc-to-bf.
_M2_PROGRESS_CADENCE: Final[int] = 100
# P-38 Phase D: public dataclasses moved to m2/schemas.py. Re-imported
# here so callsites + tests reaching for them via .runner keep working.
from bffi_pipeline.stages.m2.schemas import (  # noqa: E402
    ConversionErrorRow,
    ConversionSummary,
    HelmetMapRow,
)

# P-38 Phase D: XSLT cache + converter-version helper moved to m2/xslt.py.
from bffi_pipeline.stages.m2.xslt import (  # noqa: E402, F401
    _BFFI_PIPELINE_REPO_ROOT,
    _MARC2BIBFRAME2_DIR,
    _XSLT_PATH,
    _xslt,
    marc2bibframe2_version,
)

# --- Conversion primitives ------------------------------------------------


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# P-38 Phase D: pre-XSLT MARCXML byte-level repairs moved to
# m2/marcxml_repair.py.
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


def _run_xslt(tree: etree._ElementTree, helmet_id: str) -> etree._ElementTree:
    """Run marc2bibframe2 on ``tree`` and return the resulting RDF/XML tree."""
    result = _xslt()(
        tree,
        baseuri=etree.XSLT.strparam(_BASEURI),
        idfield=etree.XSLT.strparam("001"),
    )
    if result is None:
        raise RuntimeError(f"XSLT produced no output for {helmet_id}")
    return result


def _parse_to_graph(rdf_xml: bytes) -> Graph:
    g = Graph()
    g.parse(data=rdf_xml, format="xml")
    return g


def _find_root_resources(g: Graph) -> tuple[URIRef, URIRef]:
    """Return ``(work_uri, instance_uri)`` from the XSLT-produced graph.

    marc2bibframe2 emits one ``bf:Work`` per record's main entry plus
    additional ``bf:Work`` resources for "contained" / "related" works
    referenced via MARC 700 ind2=2 (analytical added entry), 740
    (added entry — uncontrolled name), 776 (additional physical form),
    and similar. The contained Works come into the graph as the
    *object* of ``bf:associatedResource`` triples on the main Work or
    Instance; the main Work is the one whose only inbound reference is
    ``bf:instanceOf`` from its Instance.
    """
    all_works = [s for s in g.subjects(RDF.type, V.BF.Work) if isinstance(s, URIRef)]
    if not all_works:
        raise RuntimeError("XSLT output contains no bf:Work")
    contained: set[URIRef] = {
        o for _, _, o in g.triples((None, V.BF.associatedResource, None)) if isinstance(o, URIRef)
    }
    main_works = [w for w in all_works if w not in contained]
    if not main_works:
        raise RuntimeError(
            "XSLT output contains bf:Work resources but all of them are "
            "referenced as bf:associatedResource — no main Work identifiable"
        )
    if len(main_works) > 1:
        raise RuntimeError(
            f"XSLT output contains {len(main_works)} candidate main bf:Work "
            f"resources (out of {len(all_works)} total); expected exactly 1"
        )
    work = main_works[0]
    instances = [o for o in g.objects(work, V.BF.hasInstance) if isinstance(o, URIRef)]
    if not instances:
        raise RuntimeError("XSLT output contains no bf:Instance linked to the Work")
    # marc2bibframe2 sometimes emits multiple bf:Instance resources
    # attached to the same Work via bf:hasInstance — most commonly an
    # `#Instance856-NN` secondary from a MARC 856 (Electronic Location
    # and Access) field, alongside the main `#Instance`. Without a
    # deterministic tie-breaker, ``instances[0]`` depends on rdflib
    # iteration order and post_process can attach Helmet identifier +
    # AdminMetadata to the wrong Instance; the Boundary-2 shape then
    # fires on the un-stamped main Instance. Prefer the URI ending in
    # ``#Instance`` (the marc2bibframe2 convention for the main
    # Instance); fall back to the first one if the convention doesn't
    # hold (defensive — shouldn't happen on Helmet exports).
    main_instances = [i for i in instances if str(i).endswith("#Instance")]
    instance = main_instances[0] if main_instances else instances[0]
    return work, instance


def _add_helmet_identifier(g: Graph, target: URIRef, helmet_id: str) -> None:
    ident = BNode()
    g.add((target, V.BF.identifiedBy, ident))
    g.add((ident, RDF.type, V.BF.Local))
    g.add((ident, RDF.value, Literal(helmet_id)))
    g.add((ident, V.BF.source, V.HELMET_SOURCE_URI))


def _add_marc_conversion_activity(
    g: Graph,
    *,
    work: URIRef,
    instance: URIRef,
    helmet_id: str,
    source_file: Path,
    converted_at: str,
) -> URIRef:
    activity = V.BIB[f"activity/marc-conv/{uuid.uuid4()}"]
    g.add((activity, RDF.type, V.PROV.Activity))
    g.add((activity, RDF.type, V.MarcConversion))
    g.add((activity, V.PROV.startedAtTime, Literal(converted_at, datatype=XSD.dateTime)))
    g.add((activity, V.PROV.endedAtTime, Literal(converted_at, datatype=XSD.dateTime)))
    g.add((activity, V.PROV.wasAssociatedWith, V.AGENT_MARC2BIBFRAME2))
    g.add((activity, V.PROV.used, URIRef(source_file.resolve().as_uri())))
    g.add((activity, V.helmetBibId, Literal(helmet_id)))
    g.add((activity, V.converterVersion, Literal(marc2bibframe2_version())))
    g.add((work, V.PROV.wasGeneratedBy, activity))
    g.add((instance, V.PROV.wasGeneratedBy, activity))
    return activity


def _admin_metadata_uri(target: URIRef, helmet_id: str) -> URIRef:
    suffix = "raw-work" if target.endswith("#Work") else "raw-instance"
    return V.BIB[f"adminmeta/{suffix}/{helmet_id}"]


def _add_admin_metadata_block(
    g: Graph,
    *,
    target: URIRef,
    helmet_id: str,
    activity: URIRef,
    converted_at: str,
) -> URIRef:
    am = _admin_metadata_uri(target, helmet_id)
    timestamp = Literal(converted_at, datatype=XSD.dateTime)
    helmet_record = URIRef(f"{_HELMET_RECORD_NS}{helmet_id}")
    pipeline_version_uri = V.BIB[f"gen-process/bffi-pipeline/v{PIPELINE_VERSION}"]

    g.add((target, V.adminMetadata, am))
    g.add((am, RDF.type, V.AdminMetadata))
    g.add((am, V.adminMetadataFor, target))
    g.add((am, V.descriptionCreationDate, timestamp))
    g.add((am, V.dateGenerated, timestamp))
    g.add((am, V.descriptionModifier, V.AGENT_MARC2BIBFRAME2))
    g.add((am, V.generationProcess, pipeline_version_uri))
    g.add((am, V.descriptionConventions, V.DESC_CONV_BFFI_1_0_0))
    g.add((am, V.descriptionLevel, V.DESC_LEVEL_MINIMUM))
    g.add((am, V.encodingLevel, V.ENC_LEVEL_AUTO))
    g.add((am, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    g.add((am, V.recordingSource, V.RECORDING_SOURCE_HELMET))
    g.add((am, V.metadataLicensor, V.METADATA_LICENSOR_CC0))
    g.add((am, V.sourceMetadata, helmet_record))
    g.add((am, V.PROV.wasGeneratedBy, activity))
    return am


def post_process(
    g: Graph,
    *,
    helmet_id: str,
    source_file: Path,
    converted_at: str | None = None,
) -> tuple[URIRef, URIRef]:
    """Add Helmet identifiers, conversion provenance, and AdminMetadata blocks.

    Returns ``(work_uri, instance_uri)`` for the side-effect graph. Mutates
    ``g`` in place.
    """
    converted_at = converted_at or _utc_now()
    work, instance = _find_root_resources(g)
    _add_helmet_identifier(g, work, helmet_id)
    _add_helmet_identifier(g, instance, helmet_id)
    activity = _add_marc_conversion_activity(
        g,
        work=work,
        instance=instance,
        helmet_id=helmet_id,
        source_file=source_file,
        converted_at=converted_at,
    )
    _add_admin_metadata_block(
        g,
        target=work,
        helmet_id=helmet_id,
        activity=activity,
        converted_at=converted_at,
    )
    _add_admin_metadata_block(
        g,
        target=instance,
        helmet_id=helmet_id,
        activity=activity,
        converted_at=converted_at,
    )
    g.bind("bf", V.BF)
    g.bind("bffi", V.BFFI)
    g.bind("bffi-prov", V.BFFI_PROV)
    g.bind("bib", V.BIB)
    g.bind("prov", V.PROV)
    g.bind("rdfs", RDFS)
    return work, instance


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# --- Driver ---------------------------------------------------------------


def _is_output_fresh(input_path: Path, output_path: Path) -> bool:
    return output_path.exists() and output_path.stat().st_mtime >= input_path.stat().st_mtime


def _output_path_for(output_dir: Path, helmet_id: str) -> Path:
    return output_dir / "bibframe" / f"{helmet_id}.rdf"


def _iter_xml_files(input_dir: Path) -> Iterator[Path]:
    yield from sorted(input_dir.glob("*.xml"))


def _convert_one(
    input_path: Path,
    output_dir: Path,
    *,
    force: bool,
) -> tuple[HelmetMapRow | None, str]:
    """Convert one record. Returns ``(map_row, status)`` where status is one of
    ``"ok"``, ``"skipped"``; raises typed errors on failure.

    The caller catches errors and routes them to ``_errors.jsonl``.
    """
    validated = validate(input_path)
    helmet_id = validated.helmet_bib_id
    out = _output_path_for(output_dir, helmet_id)
    if not force and _is_output_fresh(input_path, out):
        return None, "skipped"

    converted_at = _utc_now()
    # Recover ``‡<code>``-separator copy-paste before the XSLT sees it:
    # cataloguers sometimes paste from a legacy ILS display that uses
    # ``‡`` as a visible subfield boundary, producing
    # ``<subfield code="a">value‡2slm/fin‡0http://...</subfield>``.
    # The split puts the right $2 / $0 / etc. content back under
    # proper subfield codes so marc2bibframe2 emits a proper
    # bf:source + cataloguer-supplied $0 URI binding.
    _sanitize_subfield_separators(validated.tree)
    rdf_tree = _run_xslt(validated.tree, helmet_id)
    # Repair invalid BCP-47 ``xml:lang`` attributes the XSLT
    # occasionally emits (``ru-``, ``uk-``) before handing them to
    # rdflib's parser, which would otherwise raise ``ValueError``.
    _sanitize_language_tags(rdf_tree)
    rdf_bytes = etree.tostring(rdf_tree, xml_declaration=True, encoding="utf-8")
    g = _parse_to_graph(rdf_bytes)
    work, instance = post_process(
        g,
        helmet_id=helmet_id,
        source_file=input_path,
        converted_at=converted_at,
    )
    assert_conforms(g, source_path=input_path)

    out.parent.mkdir(parents=True, exist_ok=True)
    serialised = g.serialize(format="pretty-xml").encode("utf-8")
    _atomic_write_bytes(out, serialised)

    return (
        HelmetMapRow(
            helmet_bib_id=helmet_id,
            source_file=input_path.name,
            raw_work_uri=str(work),
            raw_instance_uri=str(instance),
            converted_at=converted_at,
            marc2bibframe2_version=marc2bibframe2_version(),
        ),
        "ok",
    )


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _append_source_review_m2(row: ConversionErrorRow) -> None:
    """Mirror a ``ConversionErrorRow`` into the unified source-review TSV.

    Pairs with each ``_append_jsonl(errors_path, …)`` call so the
    cataloguer-handoff superset stays in lock-step with the per-stage
    `_errors.jsonl`. P-31 Phase B wire-in for M2.
    """
    append_source_row(
        bib_id=row.helmet_bib_id or row.filename,
        stage="m2",
        severity="blocking",
        details=row.message,
    )


def _emit_errors_tsv(path: Path, errors: list[ConversionErrorRow]) -> None:
    """Cataloguer-facing TSV companion to ``bibframe/_errors.jsonl``.

    Three columns the cataloguer can open in Excel / Sheets / Numbers
    and act on without parsing JSON:

    - ``helmet_bib_id`` — derived from the source filename's stem so
      it's populated even when the XSD parse failed before we could
      extract the 001 control field (the JSONL leaves
      ``helmet_bib_id=null`` in that case).
    - ``error_type`` — one of ``marcxml-xsd-validation``,
      ``marcxml-content-minimum``, ``bibframe-shape``,
      ``bibframe-conversion``. Filterable.
    - ``message`` — single-line, tab + newline + control char
      sanitised; truncated to keep the spreadsheet readable.

    Always emitted — even on a clean run a header-only TSV is
    written. Workflows wired to the artifact path don't need a
    missing-file guard.

    Sorted by (``helmet_bib_id``, ``error_type``) for stable diffs
    across re-runs. Atomic write via ``.tmp`` + ``replace``.
    """
    header = "helmet_bib_id\terror_type\tmessage\n"
    rows: list[tuple[str, str, str]] = []
    for row in errors:
        bib_id = row.helmet_bib_id or Path(row.filename).stem
        message_clean = " ".join(row.message.replace("\t", " ").split())
        if len(message_clean) > _ERRORS_TSV_MESSAGE_MAX:
            message_clean = message_clean[: _ERRORS_TSV_MESSAGE_MAX - 1] + "…"
        rows.append((bib_id, row.error_type, message_clean))
    rows.sort()
    body = "".join(f"{bib_id}\t{etype}\t{msg}\n" for bib_id, etype, msg in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)


#: Truncate over-long error messages in the TSV so a spreadsheet
#: stays readable. XSD validation errors carry the offending value
#: and a regex pattern — easily 400+ chars. The full message lives
#: in the JSONL for forensic lookup; the TSV is for triage.
_ERRORS_TSV_MESSAGE_MAX: Final[int] = 240


def _dedupe_helmet_map(path: Path) -> None:
    """Last-write-wins dedup on ``helmet_bib_id``. Rewrites atomically."""
    if not path.exists():
        return
    seen: dict[str, dict[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        seen[row["helmet_bib_id"]] = row
    rewritten = "\n".join(json.dumps(row, ensure_ascii=False) for row in seen.values()) + "\n"
    _atomic_write_bytes(path, rewritten.encode("utf-8"))


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
