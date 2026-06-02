"""M2 per-record conversion pipeline.

Glues the M2 sub-modules together for one input file:

1. Validate the MARCXML (Boundary 1).
2. Apply pre-XSLT byte-level repairs (``marcxml_repair``).
3. Run marc2bibframe2 via the cached XSLT.
4. Parse to an rdflib Graph.
5. ``post_process`` injects Helmet identifier + provenance Activity +
   AdminMetadata blocks (``provenance``).
6. SHACL-validate the BIBFRAME (Boundary 2).
7. Serialise to ``<output_dir>/bibframe/<helmet_id>.rdf`` atomically.

Returns a :class:`HelmetMapRow` for the success path or a typed
exception for the caller (``run()``) to route to ``_errors.jsonl``.

P-38 Phase D: extracted from m2/runner.py to keep the runner focused
on the multi-record driver loop. No logic change — moves only.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from lxml import etree
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDFS

from bffi_pipeline.config import get_settings
from bffi_pipeline.export_synthesis import append_synthesis_row
from bffi_pipeline.provenance import logger as P
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m2.marcxml_repair import (
    _sanitize_language_tags,
    _sanitize_subfield_separators,
)
from bffi_pipeline.stages.m2.provenance import (
    _BASEURI,
    _add_admin_metadata_block,
    _add_helmet_identifier,
    _add_marc_conversion_activity,
    _find_root_resources,
    _utc_now,
)
from bffi_pipeline.stages.m2.salvage import (
    SalvageOutcome,
    try_salvage_minimum_content,
)
from bffi_pipeline.stages.m2.schemas import HelmetMapRow
from bffi_pipeline.stages.m2.sidecars import _atomic_write_bytes
from bffi_pipeline.stages.m2.xslt import _xslt, marc2bibframe2_version
from bffi_pipeline.validation.bibframe import assert_conforms
from bffi_pipeline.validation.marcxml import (
    MarcXmlValidationError,
    ValidatedMarcXml,
    helmet_bib_id_from_filename,
    parse_xml,
    validate,
    validate_filename,
    validate_minimum_content,
    validate_utf8,
    validate_xsd,
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


def post_process(
    g: Graph,
    *,
    helmet_id: str,
    source_file: Path,
    converted_at: str | None = None,
    salvage_outcome: SalvageOutcome | None = None,
) -> tuple[URIRef, URIRef]:
    """Add Helmet identifiers, conversion provenance, and AdminMetadata blocks.

    Returns ``(work_uri, instance_uri)`` for the side-effect graph. Mutates
    ``g`` in place.

    P-41 Phase B.5 + C.2: when ``salvage_outcome`` is non-None, also
    writes one ``bffi-prov:Synthesis`` Activity per synthesis record to
    the graph (linked to the MarcConversion Activity via ``prov:used``)
    AND appends one row per synthesis record to the per-run export-
    synthesis TSV. The Synthesis Activity and the TSV row come from
    the same source-of-truth :class:`SynthesisRecord`, so the
    retrospective ``bffi-pipeline export-synthesis-report`` CLI can
    rebuild the TSV from the provenance graph byte-identically.
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
    if salvage_outcome is not None:
        _emit_synthesis_audit_trail(
            g,
            outcome=salvage_outcome,
            used_activity=activity,
            generated=work,
        )
    g.bind("bf", V.BF)
    g.bind("bffi", V.BFFI)
    g.bind("bffi-prov", V.BFFI_PROV)
    g.bind("bib", V.BIB)
    g.bind("prov", V.PROV)
    g.bind("rdfs", RDFS)
    return work, instance


def _emit_synthesis_audit_trail(
    g: Graph,
    *,
    outcome: SalvageOutcome,
    used_activity: URIRef,
    generated: URIRef,
) -> None:
    """P-41 Phase B.5 + C.2 — emit the Synthesis Activity (in
    ``g``) and the per-run TSV row for every SynthesisRecord in
    ``outcome.records``.

    The MarcConversion Activity (passed as ``used_activity``) is the
    ``prov:used`` link — traversing it recovers the run context.
    ``generated`` is the raw Work URI; the synthesised contribution's
    blank-node URI is not stable until M3 expands the BIBFRAME, so we
    point ``prov:generated`` at the Work for now (the link is at the
    coarser granularity of "this Work has synthesised contributions",
    not "this specific contribution blank node was synthesised"). A
    future plan that needs the finer link can reify per-contribution
    URIs at M3 and add them as additional ``prov:generated`` edges
    on the same Activity.
    """
    for record in outcome.records:
        synthesis_activity = P.log_synthesis(
            g,
            used_activity=used_activity,
            generated=generated,
            synthetic_field=record.field,
            synthetic_method=record.method,
            synthetic_tier=record.tier,
            synthetic_confidence=record.confidence,
            synthetic_value=record.synthesised_value,
            synthetic_marc_source=record.marc_source,
        )
        append_synthesis_row(
            bib_id=record.bib_id,
            field=record.field,
            marc_source=record.marc_source,
            synthesised_value=record.synthesised_value,
            tier=record.tier,
            method=record.method,
            confidence=record.confidence,
            activity_uri=str(synthesis_activity),
        )
        # P-41 Phase B.6: tag the B3 sentinel agent in the BFFI graph
        # so downstream stages (and P-39's future M9 walker) can
        # filter it via :func:`bffi_pipeline.provenance.vocab.is_synthetic_sentinel`.
        # B1 / B2 records synthesise real-name agents that downstream
        # stages should keep reading; only B3 carries the sentinel
        # flag.
        if record.tier == "B3":
            sentinel_uri = URIRef(record.synthesised_value)
            g.add(
                (
                    sentinel_uri,
                    V.syntheticSentinel,
                    Literal(True, datatype=V.XSD.boolean),
                )
            )


def _is_output_fresh(input_path: Path, output_path: Path) -> bool:
    return output_path.exists() and output_path.stat().st_mtime >= input_path.stat().st_mtime


def _output_path_for(output_dir: Path, helmet_id: str) -> Path:
    return output_dir / "bibframe" / f"{helmet_id}.rdf"


def _iter_xml_files(input_dir: Path) -> Iterator[Path]:
    yield from sorted(input_dir.glob("*.xml"))


def _validate_with_salvage(
    input_path: Path,
) -> tuple[ValidatedMarcXml, SalvageOutcome | None]:
    """P-41 Phase B.7 — run the Boundary-1 validation chain; if
    ``validate_minimum_content`` raises for the missing-creator case,
    dispatch the salvage layer and re-validate.

    Returns ``(ValidatedMarcXml, SalvageOutcome | None)``. The outcome
    is non-None when salvage fired; downstream stages (provenance
    writer, TSV writer) consume it. Other Boundary-1 failure modes
    (filename, encoding, XML syntax, XSD, minimum-content failures
    that salvage can't address) re-raise the original
    :class:`MarcXmlValidationError` untouched.
    """
    try:
        return validate(input_path), None
    except MarcXmlValidationError as exc:
        if exc.error_type != "marcxml-content-minimum":
            raise
        # The four structural validations (filename, utf8, xml-syntax,
        # xsd) already passed before validate_minimum_content raised,
        # so re-parsing is safe. The minimum-content checker doesn't
        # mutate the tree, so we could re-use validate()'s tree if it
        # returned one on failure — it doesn't, so re-parse.
        validate_filename(input_path)
        raw = validate_utf8(input_path)
        tree = parse_xml(input_path, raw)
        validate_xsd(input_path, tree)
        bib_id = helmet_bib_id_from_filename(input_path)
        outcome = try_salvage_minimum_content(tree, bib_id=bib_id, settings=get_settings())
        if outcome is None:
            raise
        # Salvage mutated the tree; re-run minimum-content to confirm
        # the synthesised datafield(s) cleared the bar. If the record
        # was missing creator AND something else (e.g. 245), the
        # re-run raises the typed error per the existing contract.
        validate_minimum_content(input_path, tree)
        return ValidatedMarcXml(helmet_bib_id=bib_id, tree=tree), outcome


def _convert_one(
    input_path: Path,
    output_dir: Path,
    *,
    force: bool,
) -> tuple[HelmetMapRow | None, str]:
    """Convert one record. Returns ``(map_row, status)`` where status is one of
    ``"ok"``, ``"skipped"``; raises typed errors on failure.

    The caller catches errors and routes them to ``_errors.jsonl``.

    P-41 Phase B.7 + B.5 + C.2: validation is ``_validate_with_salvage``
    — records missing 1XX/7XX (creator) that previously raised
    ``marcxml-content-minimum`` go through the creator-salvage layer
    instead and emerge with a synthesised 100/700/710 datafield. The
    salvage outcome is forwarded into :func:`post_process` so the
    Synthesis Activity lands in the provenance graph alongside the
    MarcConversion Activity, and the per-run TSV row lands at
    ``<BFFI_DATA_DIR>/export-synthesis-<run_uuid>.tsv``.
    """
    validated, salvage_outcome = _validate_with_salvage(input_path)
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
        salvage_outcome=salvage_outcome,
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
