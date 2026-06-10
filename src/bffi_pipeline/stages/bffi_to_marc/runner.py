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

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

from lxml import etree
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import RDFS

from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.provenance.vocab import BFFI
from bffi_pipeline.rdf_utils import local_name

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


# --- BFFI → MARC mapping registry (doc-generation metadata) ---------------


@dataclass(frozen=True)
class MarcEmitMeta:
    """Documentation metadata for one MARC field family this module emits.

    Pure metadata — not consulted at conversion time. The diagnostic
    auto-generator (``bffi_pipeline.diagnostic.marc_mapping``) walks
    :data:`MARC_EMIT_REGISTRY` to produce the BFFI → MARC mapping table
    in ``docs/bffi_to_marc_mapping.md``.

    Each entry declares:

      - ``tag``: 3-character MARC field tag (``"245"``) or pseudo-tag
        for record-level constructs (``"leader"``).
      - ``indicators``: tuple of two strings ``("ind1", "ind2")`` —
        ``" "`` represents the MARC blank indicator. Empty tuple for
        control fields and leader.
      - ``subfields``: ordered tuple of ``(code, description)`` pairs.
        Empty for control fields / leader / catch-all emits.
      - ``source``: human-readable description of the BFFI walk that
        drives the emit (e.g. ``"bffi:title / bffi:Title / bffi:mainTitle"``).
      - ``notes``: optional caveat or limitation.
    """

    tag: str
    indicators: tuple[str, ...]
    subfields: tuple[tuple[str, str], ...]
    source: str
    notes: str = ""


#: MARC fields the BFFI → MARC reverse converter currently emits.
#: Populated at import time by :func:`marc_emit`-decorated extract
#: functions (plus the standalone ``leader`` entry below). The
#: registry is the single source of truth — the doc generator
#: (:mod:`bffi_pipeline.diagnostic.marc_mapping`) walks it and
#: produces the BFFI → MARC mapping doc.
MARC_EMIT_REGISTRY: list[MarcEmitMeta] = []


F = TypeVar("F", bound=Callable[..., Any])


def marc_emit(*entries: MarcEmitMeta) -> Callable[[F], F]:
    """Attach :class:`MarcEmitMeta` entries to an extract function and
    register them in :data:`MARC_EMIT_REGISTRY`.

    Multiple entries per decorator support extractors that contribute
    to several MARC tags (e.g. :func:`_extract_identifier_datafields`
    handles both 020 ISBN and 022 ISSN — both are declared on the
    same function).

    Adding a new MARC family now costs one edit: the ``@marc_emit(...)``
    decorator on the extract function. The doc generator picks it up
    at the next regeneration; no parallel registry to keep in sync.
    """

    def decorator(func: F) -> F:
        for entry in entries:
            MARC_EMIT_REGISTRY.append(entry)
        func._marc_emit_meta = entries  # type: ignore[attr-defined]
        return func

    return decorator


# The leader has no extract function — it's a static placeholder built
# directly in _build_marc_record. Register its metadata directly so it
# still appears in the auto-table at position 0.
MARC_EMIT_REGISTRY.append(
    MarcEmitMeta(
        tag="leader",
        indicators=(),
        subfields=(),
        source="static placeholder",
        notes="See known limitations below.",
    )
)


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


@marc_emit(
    MarcEmitMeta(
        tag="001",
        indicators=(),
        subfields=(),
        source=(
            "?m bffi:identifiedBy [a bffi:Local ; rdf:value ?bib_id] "
            "(fallback: parse from the Manifestation URI fragment)"
        ),
    )
)
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


@dataclass(frozen=True)
class _TitleParts:
    """The 245-field-worth of content extracted from one bffi:Title block."""

    main: str
    subtitle: str | None = None


@marc_emit(
    MarcEmitMeta(
        tag="245",
        indicators=("0", "0"),
        subfields=(
            ("a", "main title"),
            ("b", "subtitle"),
            ("c", "statement of responsibility"),
        ),
        source=(
            "?m bffi:title / bffi:Title / bffi:mainTitle (mandatory) + "
            "bffi:subtitle (optional); responsibility comes from "
            "?m bffi:responsibilityStatement"
        ),
        notes=("First bffi:title block wins. See known limitations below."),
    )
)
def _extract_main_title_parts(graph: Graph, manifestation: URIRef) -> _TitleParts | None:
    """Walk the first ``?m bffi:title / bffi:Title`` block and pull
    ``bffi:mainTitle`` (mandatory) + ``bffi:subtitle`` (optional).

    Returns ``None`` when no title block has a ``bffi:mainTitle``. v0
    picks the first block; primary-vs-variant discrimination (by
    ``bffi:marcKey``) lands in a follow-on.
    """
    for title_block in graph.objects(manifestation, BFFI.title):
        main = next(graph.objects(title_block, BFFI.mainTitle), None)
        if not isinstance(main, Literal):
            continue
        subtitle = next(graph.objects(title_block, BFFI.subtitle), None)
        return _TitleParts(
            main=str(main),
            subtitle=str(subtitle) if isinstance(subtitle, Literal) else None,
        )
    return None


def _extract_responsibility_statement(graph: Graph, manifestation: URIRef) -> str | None:
    """Return the first ``bffi:responsibilityStatement`` literal on the
    Manifestation, or ``None`` if absent. Maps directly to MARC 245 $c."""
    value = next(graph.objects(manifestation, BFFI.responsibilityStatement), None)
    return str(value) if isinstance(value, Literal) else None


#: Identifier-scheme URI → MARC datafield tag. Each ``bffi:identifiedBy``
#: block on a Manifestation carries a ``bffi:source`` URI naming the
#: LoC identifier scheme; this dispatch table converts those URIs into
#: the right MARC tag for the round-trip emit.
_IDENTIFIER_SCHEME_TO_MARC_TAG: Final[dict[URIRef, str]] = {
    URIRef("http://id.loc.gov/vocabulary/identifiers/isbn"): "020",
    URIRef("http://id.loc.gov/vocabulary/identifiers/issn"): "022",
}


@dataclass(frozen=True)
class _IdentifierEmit:
    """One MARC identifier datafield's worth of content (tag + value)."""

    tag: str
    value: str


@dataclass(frozen=True)
class _PhysicalDescription:
    """MARC 300 components: extent (\\$a) and dimensions (\\$c)."""

    extent: str | None
    dimensions: str | None


@marc_emit(
    MarcEmitMeta(
        tag="300",
        indicators=(" ", " "),
        subfields=(
            ("a", "extent"),
            ("c", "dimensions"),
        ),
        source=(
            "?m bffi:extent / bffi:Extent / rdfs:label (for $a) and "
            "?m bffi:dimensions literal (for $c)"
        ),
        notes="First-extent-wins for multi-extent records (rare).",
    )
)
def _extract_physical_description(
    graph: Graph, manifestation: URIRef
) -> _PhysicalDescription | None:
    """Walk ``?m bffi:extent / bffi:Extent / rdfs:label`` for the extent
    literal and ``?m bffi:dimensions`` for the dimensions literal.

    Returns ``None`` when neither is present (no MARC 300 to emit).
    First extent and first dimensions value win; multi-extent records
    are a follow-on (rare in the corpus)."""
    extent_label: str | None = None
    for extent_block in graph.objects(manifestation, BFFI.extent):
        label = next(graph.objects(extent_block, RDFS.label), None)
        if isinstance(label, Literal):
            extent_label = str(label)
            break
    dim_value = next(graph.objects(manifestation, BFFI.dimensions), None)
    dimensions = str(dim_value) if isinstance(dim_value, Literal) else None
    if extent_label is None and dimensions is None:
        return None
    return _PhysicalDescription(extent=extent_label, dimensions=dimensions)


@marc_emit(
    MarcEmitMeta(
        tag="041",
        indicators=(" ", " "),
        subfields=(("a", "3-letter language code (one per language)"),),
        source=(
            "?m bffi:language <http://id.loc.gov/vocabulary/languages/{code}> "
            "— the last URI segment is the MARC code"
        ),
    )
)
def _extract_language_codes(graph: Graph, manifestation: URIRef) -> list[str]:
    """Walk every ``?m bffi:language`` object — typically a LoC language
    vocabulary URI like ``<http://id.loc.gov/vocabulary/languages/eng>``.
    Returns the 3-letter MARC language codes (the URI's local name).

    Deduped, deterministic ordering (sorted). Languages live on the
    Manifestation per marc2bibframe2's MARC 008 / 041 emit pattern.
    Maps to MARC 041 \\$a (one per language).
    """
    codes = {
        local_name(obj)
        for obj in graph.objects(manifestation, BFFI.language)
        if isinstance(obj, URIRef)
    }
    return sorted(codes)


#: Matches ``#<Type><tag>-<n>`` in subject-node URI fragments emitted
#: by marc2bibframe2 (e.g. ``#Agent600-28`` / ``#Topic650-12`` /
#: ``#Place651-30`` / ``#Temporal648-29``). Capture group 1 is the
#: 3-digit MARC tag the source subject came from.
_SUBJECT_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"#[A-Za-z]+(\d{3})-")

#: MARC 6XX subject tags this routing recognises. Other tag values
#: produced by marc2bibframe2 (e.g. 730 uniform titles via #Work730)
#: are not subjects and get dispatched separately.
_SUBJECT_MARC_TAGS: Final[frozenset[str]] = frozenset(
    {"600", "610", "611", "630", "648", "650", "651", "655"}
)


@dataclass(frozen=True)
class _SubjectEmit:
    """One MARC 6XX subject datafield's worth of content."""

    tag: str
    label: str


def _find_work_for_manifestation(graph: Graph, manifestation: URIRef) -> URIRef | None:
    """Return the Work URI this Manifestation manifests, or ``None``.

    Walks ``manifestation bffi:workManifested → Work``. The Work URI
    is the anchor for subject / classification / contribution triples
    (which are properties of the abstract Work in BFFI's FRBR-axis
    split, not the Manifestation).
    """
    work = next(graph.objects(manifestation, BFFI.workManifested), None)
    return work if isinstance(work, URIRef) else None


#: BFFI source description prefix shared by every 6XX subject row.
#: Subjects live on the Work (FRBR-axis: subjects describe the
#: abstract Work, not a particular Manifestation), so every row's
#: source begins with the same walk.
_SUBJECT_SOURCE_PREFIX: Final[str] = (
    "?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label"
)


@marc_emit(
    MarcEmitMeta(
        tag="600",
        indicators=(" ", " "),
        subfields=(("a", "personal name subject heading"),),
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Person`",
    ),
    MarcEmitMeta(
        tag="610",
        indicators=(" ", " "),
        subfields=(("a", "corporate name subject heading"),),
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Organization`",
    ),
    MarcEmitMeta(
        tag="611",
        indicators=(" ", " "),
        subfields=(("a", "meeting / conference subject heading"),),
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Meeting`",
    ),
    MarcEmitMeta(
        tag="630",
        indicators=(" ", " "),
        subfields=(("a", "uniform title subject heading"),),
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Title`",
    ),
    MarcEmitMeta(
        tag="648",
        indicators=(" ", " "),
        subfields=(("a", "chronological term subject heading"),),
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Temporal`",
    ),
    MarcEmitMeta(
        tag="650",
        indicators=(" ", " "),
        subfields=(("a", "topical term subject heading"),),
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Topic`",
    ),
    MarcEmitMeta(
        tag="651",
        indicators=(" ", " "),
        subfields=(("a", "geographic name subject heading"),),
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Place`",
    ),
    MarcEmitMeta(
        tag="655",
        indicators=(" ", " "),
        subfields=(("a", "genre / form term"),),
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:GenreForm`",
    ),
)
def _extract_subject_datafields(graph: Graph, manifestation: URIRef) -> list[_SubjectEmit]:
    """Walk ``?work bffi:subject ?subject_node`` for each subject anchor.

    The subject node's URI fragment carries the source MARC tag
    (``#Agent600-28`` → ``600``). Subject nodes are typed with one of
    ``bffi:Person`` / ``bffi:Organization`` / ``bffi:Meeting`` /
    ``bffi:Topic`` / ``bffi:Place`` / ``bffi:Temporal`` / ``bffi:GenreForm``
    and carry an ``rdfs:label`` for the heading text.

    Returns a list of (tag, label) emits — one per subject. Only
    recognised 6XX tags (:data:`_SUBJECT_MARC_TAGS`) are emitted;
    others are skipped (they get their own routing).

    Deterministic ordering: sorted by (tag, label) so multi-subject
    records round-trip predictably.
    """
    work = _find_work_for_manifestation(graph, manifestation)
    if work is None:
        return []
    emits: list[_SubjectEmit] = []
    for subj_node in graph.objects(work, BFFI.subject):
        if not isinstance(subj_node, URIRef):
            continue
        match = _SUBJECT_TAG_PATTERN.search(str(subj_node))
        if match is None:
            continue
        tag = match.group(1)
        if tag not in _SUBJECT_MARC_TAGS:
            continue
        label = next(graph.objects(subj_node, RDFS.label), None)
        if not isinstance(label, Literal):
            continue
        emits.append(_SubjectEmit(tag=tag, label=str(label)))
    return sorted(emits, key=lambda e: (e.tag, e.label))


@marc_emit(
    MarcEmitMeta(
        tag="020",
        indicators=(" ", " "),
        subfields=(("a", "ISBN value"),),
        source=(
            "?m bffi:identifiedBy [a bffi:Identifier ; "
            "bffi:source <http://id.loc.gov/vocabulary/identifiers/isbn> ; "
            "rdf:value ?isbn]"
        ),
    ),
    MarcEmitMeta(
        tag="022",
        indicators=(" ", " "),
        subfields=(("a", "ISSN value"),),
        source=(
            "?m bffi:identifiedBy [a bffi:Identifier ; "
            "bffi:source <http://id.loc.gov/vocabulary/identifiers/issn> ; "
            "rdf:value ?issn]"
        ),
    ),
)
def _extract_identifier_datafields(graph: Graph, manifestation: URIRef) -> list[_IdentifierEmit]:
    """Walk every ``bffi:identifiedBy`` block and convert to a MARC
    datafield emit when its ``bffi:source`` URI is in the dispatch table.

    Local IDs (with no ``bffi:source`` or a source not in the table)
    are skipped — they're either the 001-bound bib ID (handled
    separately) or an identifier scheme we don't yet emit. Each
    additional scheme lands as its own follow-on commit by extending
    :data:`_IDENTIFIER_SCHEME_TO_MARC_TAG`.
    """
    emits: list[_IdentifierEmit] = []
    for ident in graph.objects(manifestation, BFFI.identifiedBy):
        source = next(graph.objects(ident, BFFI.source), None)
        if not isinstance(source, URIRef):
            continue
        tag = _IDENTIFIER_SCHEME_TO_MARC_TAG.get(source)
        if tag is None:
            continue
        value = next(graph.objects(ident, RDF.value), None)
        if not isinstance(value, Literal):
            continue
        emits.append(_IdentifierEmit(tag=tag, value=str(value)))
    return emits


def _build_marc_record(
    *,
    bib_id: str,
    title_parts: _TitleParts | None,
    responsibility: str | None,
    identifiers: list[_IdentifierEmit],
    language_codes: list[str],
    physical: _PhysicalDescription | None,
    subjects: list[_SubjectEmit],
) -> etree._Element:
    """Build one MARCXML ``<record>`` element with the v0+ field set."""
    record = etree.Element(f"{_MARC}record")
    leader = etree.SubElement(record, f"{_MARC}leader")
    leader.text = _LEADER_PLACEHOLDER

    cf001 = etree.SubElement(record, f"{_MARC}controlfield", tag="001")
    cf001.text = bib_id

    # 020 ISBN / 022 ISSN come before 041 / 245 / 300 in MARC tag order.
    for ident in identifiers:
        df = etree.SubElement(record, f"{_MARC}datafield", tag=ident.tag, ind1=" ", ind2=" ")
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = ident.value

    if language_codes:
        df041 = etree.SubElement(record, f"{_MARC}datafield", tag="041", ind1=" ", ind2=" ")
        for code in language_codes:
            sf_a = etree.SubElement(df041, f"{_MARC}subfield", code="a")
            sf_a.text = code

    if title_parts is not None:
        df245 = etree.SubElement(record, f"{_MARC}datafield", tag="245", ind1="0", ind2="0")
        sf_a = etree.SubElement(df245, f"{_MARC}subfield", code="a")
        sf_a.text = title_parts.main
        if title_parts.subtitle is not None:
            sf_b = etree.SubElement(df245, f"{_MARC}subfield", code="b")
            sf_b.text = title_parts.subtitle
        if responsibility is not None:
            sf_c = etree.SubElement(df245, f"{_MARC}subfield", code="c")
            sf_c.text = responsibility

    if physical is not None:
        df300 = etree.SubElement(record, f"{_MARC}datafield", tag="300", ind1=" ", ind2=" ")
        if physical.extent is not None:
            sf_a = etree.SubElement(df300, f"{_MARC}subfield", code="a")
            sf_a.text = physical.extent
        if physical.dimensions is not None:
            sf_c = etree.SubElement(df300, f"{_MARC}subfield", code="c")
            sf_c.text = physical.dimensions

    # 6XX subjects come after the bibliographic-description block.
    for subj in subjects:
        df = etree.SubElement(record, f"{_MARC}datafield", tag=subj.tag, ind1=" ", ind2=" ")
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = subj.label

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
    title_parts = _extract_main_title_parts(graph, manifestation)
    responsibility = _extract_responsibility_statement(graph, manifestation)
    identifiers = _extract_identifier_datafields(graph, manifestation)
    language_codes = _extract_language_codes(graph, manifestation)
    physical = _extract_physical_description(graph, manifestation)
    subjects = _extract_subject_datafields(graph, manifestation)
    record = _build_marc_record(
        bib_id=bib_id,
        title_parts=title_parts,
        responsibility=responsibility,
        identifiers=identifiers,
        language_codes=language_codes,
        physical=physical,
        subjects=subjects,
    )
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
