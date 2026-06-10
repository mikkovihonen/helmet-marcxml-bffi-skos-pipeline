"""Pillar 4 orchestrator: BFFI canonical Turtle -> reconstructed MARCXML.

Step 4 of P-057 — v0 emit. Reads a BFFI graph and emits a MARCXML record
per Manifestation. Cardinal rule (see ``docs/bffi_limitations.md``): the
reverse converter MUST NOT consult ``bffi-prov:`` (pipeline-internal
provenance) for bibliographic content. Pipeline-internal data is fair
for UI / pairing machinery, never for emit content. The closed-namespace
discipline test (``tests/unit/stages/bffi_to_marc/test_bffi_prov_discipline.py``)
parses this module's source and fails the build if a ``bffi-prov:``
reference creeps in.

v0 scope: emit the minimum-viable MARCXML that lets the round-trip diff
harness (step 5) compare against the source. Concretely:

  - leader   placeholder (24 chars; positions populated in a later step)
  - 001      Helmet bib ID, read from a ``bffi:identifiedBy [ a bffi:Local ;
             rdf:value ?bib_id ]`` block. Fallback: parse from the
             Manifestation URI fragment (``http://…/<bib_id>#Instance``,
             marc2bibframe2's emit shape with our ``baseuri`` parameter).
  - 245 $a   main title, walked via ``?m bffi:title / bffi:mainTitle``.

Anything else — contributors, identifier schemes (ISBN / ISSN), subjects,
provision activity, notes, language, content type — is deliberately
deferred. Each MARC family lands in its own follow-on commit so the diff
harness gives a clean per-family verification signal.

Stage label for observability sidecar events: ``bffi2marc``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from lxml import etree
from rdflib import RDF, Graph, Literal, URIRef

from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.provenance.vocab import BFFI

STAGE: Final[str] = "bffi2marc"

#: MARCXML namespace per the LoC MARC21 slim schema.
MARC21_NS: Final[str] = "http://www.loc.gov/MARC21/slim"
_MARC: Final[str] = f"{{{MARC21_NS}}}"

#: How often to emit a ``progress`` event during corpus conversion.
PROGRESS_CADENCE: Final[int] = 100

#: Placeholder MARC leader. The 24 positions encode record-status / type /
#: bibliographic-level / control-type / character-coding / indicator-count
#: / subfield-code-length / base-address-of-data / encoding-level /
#: descriptive-cataloguing-form / multipart-resource-record-level. v0 emits
#: a placeholder; populating each position from BFFI state lands in a
#: follow-on commit alongside the rest of the field families.
_LEADER_PLACEHOLDER: Final[str] = "00000nam a2200000 a 4500"


@dataclass(frozen=True)
class ConversionOptions:
    """Configuration for one corpus-conversion run."""

    input_dir: Path
    output_dir: Path


@dataclass
class ConversionSummary:
    """Aggregate counts after corpus conversion ends."""

    total: int = 0
    converted: int = 0
    failed: int = 0
    #: Inputs that produced zero ``bffi:Manifestation`` entities. Indicates
    #: either a malformed BFFI Turtle or a BIBFRAME-stage output we haven't
    #: routed yet. v0 treats these as failures.
    no_manifestation: int = 0
    failures: list[tuple[Path, str]] = field(default_factory=list)


class BffiToMarcError(RuntimeError):
    """A single-record conversion failed."""


def _extract_bib_id_from_local(graph: Graph, manifestation: URIRef) -> str | None:
    """Walk ``manifestation bffi:identifiedBy [a bffi:Local; rdf:value ?id]``.

    Returns the first Local identifier value found, or ``None`` if no
    Local block is present on this Manifestation.
    """
    for ident in graph.objects(manifestation, BFFI.identifiedBy):
        if (ident, RDF.type, BFFI.Local) in graph:
            value = next(graph.objects(ident, RDF.value), None)
            if isinstance(value, Literal):
                return str(value)
    return None


def _extract_bib_id_from_uri(manifestation: URIRef) -> str | None:
    """Fallback bib-ID extractor for the marc2bibframe2 emit shape.

    With ``baseuri=<…>`` and ``idfield=001``, marc2bibframe2 concatenates
    ``baseuri + bib_id + "#Instance"``. The bib ID is whatever sits
    between the rightmost path/namespace delimiter and the fragment —
    so we take the substring after the rightmost ``/`` *or* ``:`` (URN
    paths like ``http://urn.fi/URN:NBN:fi:bib:<id>`` use ``:`` as the
    final separator before the ID).
    """
    uri_str = str(manifestation)
    fragment_idx = uri_str.find("#")
    base = uri_str[:fragment_idx] if fragment_idx > 0 else uri_str
    cut = max(base.rfind("/"), base.rfind(":"))
    if cut < 0:
        return None
    tail = base[cut + 1 :]
    return tail or None


def _extract_main_title(graph: Graph, manifestation: URIRef) -> str | None:
    """Walk ``?m bffi:title / bffi:mainTitle`` and return the first match."""
    for title_block in graph.objects(manifestation, BFFI.title):
        main = next(graph.objects(title_block, BFFI.mainTitle), None)
        if isinstance(main, Literal):
            return str(main)
    return None


def _build_marc_record(
    *,
    bib_id: str,
    main_title: str | None,
) -> etree._Element:
    """Build one MARCXML ``<record>`` element with the v0 field set."""
    record = etree.Element(f"{_MARC}record")
    leader = etree.SubElement(record, f"{_MARC}leader")
    leader.text = _LEADER_PLACEHOLDER

    cf001 = etree.SubElement(record, f"{_MARC}controlfield", tag="001")
    cf001.text = bib_id

    if main_title is not None:
        df245 = etree.SubElement(record, f"{_MARC}datafield", tag="245", ind1="0", ind2="0")
        sf_a = etree.SubElement(df245, f"{_MARC}subfield", code="a")
        sf_a.text = main_title

    return record


def emit_marcxml(graph: Graph, *, manifestation: URIRef) -> bytes:
    """Build a MARCXML document (root: ``<record>``) for one Manifestation.

    Returns the serialised bytes, pretty-printed, with UTF-8 declaration.
    Raises :exc:`BffiToMarcError` if the bib ID can't be determined (no
    Local block + URI fragment fallback also fails).
    """
    bib_id = _extract_bib_id_from_local(graph, manifestation) or _extract_bib_id_from_uri(
        manifestation
    )
    if bib_id is None:
        raise BffiToMarcError(f"no bib ID found for manifestation {manifestation}")
    main_title = _extract_main_title(graph, manifestation)
    record = _build_marc_record(bib_id=bib_id, main_title=main_title)
    return etree.tostring(
        record,
        pretty_print=True,
        xml_declaration=True,
        encoding="utf-8",
    )


def convert_one(bffi_path: Path, *, options: ConversionOptions) -> Path:
    """Convert one BFFI Turtle to MARCXML.

    Writes ``<output_dir>/<stem>.marcxml`` and returns the path.
    Raises :exc:`BffiToMarcError` on parse failure or when no
    Manifestation is present in the input graph.
    """
    output_path = options.output_dir / f"{bffi_path.stem.removesuffix('.bffi')}.marcxml"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    graph = Graph()
    try:
        graph.parse(bffi_path, format="turtle")
    except Exception as exc:
        raise BffiToMarcError(f"rdflib parse failed for {bffi_path}: {exc}") from exc

    manifestations = [
        m for m in graph.subjects(RDF.type, BFFI.Manifestation) if isinstance(m, URIRef)
    ]
    if not manifestations:
        raise BffiToMarcError(f"no bffi:Manifestation entity in {bffi_path} — nothing to emit")

    # v0: one MARCXML record for the first Manifestation. Multi-Manifestation
    # graphs (marc2bibframe2's preprocess-splitter output) are a follow-on
    # concern.
    marcxml_bytes = emit_marcxml(graph, manifestation=manifestations[0])
    output_path.write_bytes(marcxml_bytes)
    return output_path


def convert_corpus(*, options: ConversionOptions) -> ConversionSummary:
    """Walk ``options.input_dir`` and convert every ``*.bffi.ttl`` to MARCXML.

    Emits observability events through the active emitter (if any):

      - ``start``    once at entry, with ``entities_total``
      - ``progress`` every ``PROGRESS_CADENCE`` records
      - ``failed``   per record that raised :exc:`BffiToMarcError`
      - ``end``      once at exit, with success / failed bucket counts

    Returns the aggregate :class:`ConversionSummary`.
    """
    bffi_files = sorted(options.input_dir.glob("*.bffi.ttl"))
    total = len(bffi_files)

    emit_if_active(
        stage=STAGE,
        event="start",
        counters={"entities_total": total},
    )

    summary = ConversionSummary(total=total)

    for idx, path in enumerate(bffi_files, start=1):
        try:
            convert_one(path, options=options)
            summary.converted += 1
        except BffiToMarcError as exc:
            summary.failed += 1
            message = str(exc)
            if "no bffi:Manifestation" in message:
                summary.no_manifestation += 1
            summary.failures.append((path, message))
            emit_if_active(
                stage=STAGE,
                event="failed",
                extra={"path": str(path), "error": message[:240]},
            )

        if idx % PROGRESS_CADENCE == 0 or idx == total:
            emit_if_active(
                stage=STAGE,
                event="progress",
                counters={"entities_processed": idx},
            )

    emit_if_active(
        stage=STAGE,
        event="end",
        counters={
            "success": summary.converted,
            "failed": summary.failed,
            "no_manifestation": summary.no_manifestation,
        },
    )

    return summary
