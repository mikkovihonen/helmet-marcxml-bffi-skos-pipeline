"""Pillar 4 orchestrator: BFFI canonical Turtle -> reconstructed MARCXML.

Step 4 of P-057 — v0 emit. Reads a BFFI graph and emits a MARCXML record
per Manifestation. Cardinal rule: the reverse converter MUST NOT consult
``bffi-prov:`` (pipeline-internal provenance) for bibliographic content.
Pipeline-internal data is fair for UI / pairing machinery, never for emit
content. The closed-namespace discipline test
(``tests/unit/stages/bffi_to_marc/test_bffi_prov_discipline.py``) parses
this module's source and fails the build if a ``bffi-prov:`` reference
creeps in.

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

from lxml import etree
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import RDFS
from rdflib.term import Node

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

#: LoC relator URI prefix — the namespace for ``$4`` relator-code URIs
#: that marc2bibframe2 emits when source MARC carried ``$4 <code>``.
_LOC_RELATOR_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/relators/"


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


@marc_emit(
    MarcEmitMeta(
        tag="005",
        indicators=(),
        subfields=(),
        source="?m bffi:adminMetadata [a bffi:AdminMetadata ; bffi:changeDate ?date]",
    )
)
def _extract_change_date(graph: Graph, manifestation: URIRef) -> str | None:
    """Return the ``bffi:changeDate`` literal from the Manifestation's
    AdminMetadata block. Maps directly to MARC 005."""
    for admin in graph.objects(manifestation, BFFI.adminMetadata):
        date = next(graph.objects(admin, BFFI.changeDate), None)
        if isinstance(date, Literal):
            return str(date)
    return None


@dataclass(frozen=True)
class _PublicationEmit:
    """One MARC 260 datafield's content, split into structured subfields.

    Any combination of place / agent / date can be absent (or all three —
    in which case the fallback ``statement`` carries the flat transcribed
    string from ``bffi:publicationStatement``). ISBD trailing punctuation
    is applied at emit time, not stored here."""

    place: str | None
    agent: str | None
    date: str | None
    statement: str | None


@marc_emit(
    MarcEmitMeta(
        tag="260",
        indicators=(" ", " "),
        subfields=(
            ("a", "place of publication / distribution"),
            ("b", "publisher / distributor name"),
            ("c", "date of publication / distribution"),
        ),
        source=(
            "?m bffi:provisionActivity ?pa . ?pa a bffi:Publication ; "
            "bffi:simplePlace ?place ; bffi:simpleAgent ?agent ; "
            "bffi:simpleDate ?date . "
            "Fallback: ?m bffi:publicationStatement ?text — emits in $a "
            "as a single flat string when the structured parts are absent."
        ),
        notes=(
            'ISBD trailing punctuation (" :" before $b, "," before $c) is '
            "added at emit time. If no Publication-typed provisionActivity "
            "carries the structured parts, the flat bffi:publicationStatement "
            "is the fallback — whole transcribed string in $a."
        ),
    )
)
def _extract_publication(graph: Graph, manifestation: URIRef) -> _PublicationEmit | None:
    """Walk the Manifestation's ``bffi:provisionActivity`` blocks for the
    first Publication-typed activity and return its structured place /
    agent / date. Falls back to ``bffi:publicationStatement`` when no
    structured parts are present."""
    place: str | None = None
    agent: str | None = None
    date: str | None = None
    for pa in graph.objects(manifestation, BFFI.provisionActivity):
        if (pa, RDF.type, BFFI.Publication) not in graph:
            continue
        if place is None:
            place = _first_literal(graph, pa, BFFI.simplePlace)
        if agent is None:
            agent = _first_literal(graph, pa, BFFI.simpleAgent)
        if date is None:
            date = _first_literal(graph, pa, BFFI.simpleDate)
        if place and agent and date:
            break
    statement: str | None = None
    if place is None and agent is None and date is None:
        value = next(graph.objects(manifestation, BFFI.publicationStatement), None)
        if isinstance(value, Literal):
            statement = str(value)
    if place is None and agent is None and date is None and statement is None:
        return None
    return _PublicationEmit(place=place, agent=agent, date=date, statement=statement)


def _first_literal(graph: Graph, subject: Node, predicate: URIRef) -> str | None:
    """Return the first literal value of ``predicate`` on ``subject``,
    or ``None`` if absent / not a literal."""
    value = next(graph.objects(subject, predicate), None)
    return str(value) if isinstance(value, Literal) else None


@dataclass(frozen=True)
class _RdaDescriptors:
    """RDA content/media/carrier codes for MARC 336/337/338 emit."""

    content_codes: tuple[str, ...]
    media_codes: tuple[str, ...]
    carrier_codes: tuple[str, ...]


@marc_emit(
    MarcEmitMeta(
        tag="336",
        indicators=(" ", " "),
        subfields=(("a", "RDA content type code"),),
        source=(
            "?m bffi:workManifested ?work . "
            "?work bffi:content <http://id.loc.gov/vocabulary/contentTypes/{code}>"
        ),
    ),
    MarcEmitMeta(
        tag="337",
        indicators=(" ", " "),
        subfields=(("a", "RDA media type code"),),
        source="?m bffi:media <http://id.loc.gov/vocabulary/mediaTypes/{code}>",
    ),
    MarcEmitMeta(
        tag="338",
        indicators=(" ", " "),
        subfields=(("a", "RDA carrier type code"),),
        source="?m bffi:carrier <http://id.loc.gov/vocabulary/carriers/{code}>",
    ),
)
def _extract_rda_descriptors(graph: Graph, manifestation: URIRef) -> _RdaDescriptors:
    """Walk the RDA content / media / carrier predicates and return
    each as its 3-letter MARC code (the URI's local name, taken from
    the LoC vocabulary URIs marc2bibframe2 emits).

    Content lives on the Expression / Work (FRBR-axis: the work
    *contains* text vs music vs cartographic material); media and
    carrier live on the Manifestation (physical-format properties).
    Multiple values per predicate produce multiple datafields, sorted
    for determinism.
    """
    work = _find_work_for_manifestation(graph, manifestation)
    content = sorted(
        local_name(obj)
        for obj in (graph.objects(work, BFFI.content) if work is not None else ())
        if isinstance(obj, URIRef)
    )
    media = sorted(
        local_name(obj)
        for obj in graph.objects(manifestation, BFFI.media)
        if isinstance(obj, URIRef)
    )
    carrier = sorted(
        local_name(obj)
        for obj in graph.objects(manifestation, BFFI.carrier)
        if isinstance(obj, URIRef)
    )
    return _RdaDescriptors(
        content_codes=tuple(content),
        media_codes=tuple(media),
        carrier_codes=tuple(carrier),
    )


@dataclass(frozen=True)
class _TitleParts:
    """The 245-field-worth of content extracted from one bffi:Title block."""

    main: str
    subtitle: str | None = None


#: Maps a BFFI agent class to a ``(primary_tag, added_tag)`` MARC pair.
#: Primary contributions (``bffi:PrimaryContribution``-typed) emit as
#: MARC 1XX; all others emit as MARC 7XX of the matching agent type.
#: ``bffi:Jurisdiction`` is corporate-like in MARC.
_AGENT_TYPE_TO_MARC_TAG_PAIR: Final[dict[URIRef, tuple[str, str]]] = {
    BFFI.Person: ("100", "700"),
    BFFI.Organization: ("110", "710"),
    BFFI.Jurisdiction: ("110", "710"),
    BFFI.Meeting: ("111", "711"),
}


@dataclass(frozen=True)
class _ContributorEmit:
    """One MARC contributor datafield (100/110/111/700/710/711).

    ``relator`` is the LoC relator code (the URI's last segment, e.g.
    ``"aut"``) used for ``$4``; ``relator_term`` is the cataloguer's
    free-text term (e.g. Finnish ``"näyttelijä"``) used for ``$e``.
    Either, both, or neither may be present depending on what the
    source MARC carried."""

    tag: str
    label: str
    relator: str | None
    relator_term: str | None


def _agent_marc_tag(graph: Graph, agent: URIRef, *, is_primary: bool) -> str | None:
    """Pick the MARC tag for a contribution from the agent's class type
    + primary/added flag. Returns ``None`` if no type signal matches."""
    for agent_type, (primary_tag, added_tag) in _AGENT_TYPE_TO_MARC_TAG_PAIR.items():
        if (agent, RDF.type, agent_type) in graph:
            return primary_tag if is_primary else added_tag
    return None


_CONTRIBUTOR_SUBFIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("a", "personal / corporate / meeting name"),
    ("e", "relator term (cataloguer's free-text role, e.g. 'näyttelijä')"),
    ("4", "LoC relator code (e.g. 'aut')"),
)


@marc_emit(
    MarcEmitMeta(
        tag="100",
        indicators=(" ", " "),
        subfields=_CONTRIBUTOR_SUBFIELDS,
        source=(
            "?m bffi:workManifested ?work . "
            "?work bffi:contribution [a bffi:PrimaryContribution ; "
            "bffi:agent ?agent ; bffi:role ?role] . "
            "?agent a bffi:Person ; rdfs:label ?name . "
            "$4 = local-name of ?role when ?role is a LoC relator URI; "
            "$e = rdfs:label of ?role when ?role is a bnode with a label."
        ),
    ),
    MarcEmitMeta(
        tag="110",
        indicators=(" ", " "),
        subfields=_CONTRIBUTOR_SUBFIELDS,
        source=(
            "Same as 100, but with ?agent a bffi:Organization "
            "(or bffi:Jurisdiction) on a primary contribution"
        ),
    ),
    MarcEmitMeta(
        tag="111",
        indicators=(" ", " "),
        subfields=_CONTRIBUTOR_SUBFIELDS,
        source="Same as 100, but with ?agent a bffi:Meeting on a primary contribution",
    ),
    MarcEmitMeta(
        tag="700",
        indicators=(" ", " "),
        subfields=_CONTRIBUTOR_SUBFIELDS,
        source=(
            "Same chain as 100, but the contribution is NOT typed "
            "bffi:PrimaryContribution — added-entry contributors land in 7XX"
        ),
    ),
    MarcEmitMeta(
        tag="710",
        indicators=(" ", " "),
        subfields=_CONTRIBUTOR_SUBFIELDS,
        source="Same as 700, but with ?agent a bffi:Organization or bffi:Jurisdiction",
    ),
    MarcEmitMeta(
        tag="711",
        indicators=(" ", " "),
        subfields=_CONTRIBUTOR_SUBFIELDS,
        source="Same as 700, but with ?agent a bffi:Meeting",
    ),
)
def _extract_contributors(graph: Graph, manifestation: URIRef) -> list[_ContributorEmit]:
    """Walk ``?work bffi:contribution`` blocks and emit MARC 1XX (for
    ``bffi:PrimaryContribution``) or 7XX (for added entries), picking
    the digit-pair from the agent's class type (Person → X00, Corporate
    / Jurisdiction → X10, Meeting → X11).

    Emits ``$a`` from ``rdfs:label`` on the agent, ``$e`` (relator term)
    from the ``rdfs:label`` of the role bnode, and ``$4`` (LoC relator
    code) from the local-name of ``bffi:role`` when it's a URI. The
    Helmet corpus uses free-text ``$e`` heavily (~10x more often than
    ``$4``); both shapes are emitted when their respective signals are
    present in BFFI.
    """
    work = _find_work_for_manifestation(graph, manifestation)
    if work is None:
        return []
    emits: list[_ContributorEmit] = []
    for contrib in graph.objects(work, BFFI.contribution):
        is_primary = (contrib, RDF.type, BFFI.PrimaryContribution) in graph
        agent = next(graph.objects(contrib, BFFI.agent), None)
        if not isinstance(agent, URIRef):
            continue
        label = next(graph.objects(agent, RDFS.label), None)
        if not isinstance(label, Literal):
            continue
        tag = _agent_marc_tag(graph, agent, is_primary=is_primary)
        if tag is None:
            continue
        relator, relator_term = _extract_role_codes(graph, contrib)
        emits.append(
            _ContributorEmit(
                tag=tag,
                label=str(label),
                relator=relator,
                relator_term=relator_term,
            )
        )
    return sorted(emits, key=lambda e: (e.tag, e.label, e.relator_term or "", e.relator or ""))


def _extract_role_codes(graph: Graph, contrib: Node) -> tuple[str | None, str | None]:
    """Return ``(relator_code, relator_term)`` for one ``bffi:contribution``.

    A contribution can carry ``bffi:role`` as either a LoC relator URI
    (drives ``$4``) or as a node with ``rdfs:label`` (drives ``$e``).
    The two signals are independent — both can be present on the same
    contribution if marc2bibframe2 emitted both shapes for a source with
    ``$e <term> $4 <code>``.
    """
    relator: str | None = None
    relator_term: str | None = None
    for role in graph.objects(contrib, BFFI.role):
        if (
            isinstance(role, URIRef)
            and str(role).startswith(_LOC_RELATOR_PREFIX)
            and relator is None
        ):
            relator = local_name(role)
        label = next(graph.objects(role, RDFS.label), None)
        if isinstance(label, Literal) and relator_term is None:
            relator_term = str(label)
    return relator, relator_term


@dataclass(frozen=True)
class _AddedTitleEmit:
    """One MARC 730/740 added-title datafield, parsed verbatim from
    ``bffi:marcKey``: tag, indicators, and the ordered subfield list."""

    tag: str
    ind1: str
    ind2: str
    subfields: tuple[tuple[str, str], ...]


def _parse_marc_key(key: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]] | None:
    """Parse a BFLC ``marcKey`` literal.

    Format is ``<tag-3-chars><ind1-1-char><ind2-1-char><subfields>`` where
    ``<subfields>`` is ``$<code><value>$<code><value>...`` — no separator
    between the indicators and the first ``$``. Blank indicators render
    as ASCII space.

    Returns ``(tag, ind1, ind2, subfields)`` or ``None`` if the key is
    too short or malformed (no leading ``$`` at position 5).
    """
    min_len_for_subfields = 6
    sf_start = 5
    if len(key) < min_len_for_subfields or key[sf_start] != "$":
        return None
    tag = key[:3]
    ind1 = key[3]
    ind2 = key[4]
    subfields: list[tuple[str, str]] = []
    # key[5:] starts with "$"; split on "$" yields ["", "<code><val>", ...].
    for part in key[sf_start:].split("$")[1:]:
        if not part:
            continue
        subfields.append((part[0], part[1:]))
    return tag, ind1, ind2, tuple(subfields)


@marc_emit(
    MarcEmitMeta(
        tag="730",
        indicators=("0", " "),
        subfields=(
            ("a", "uniform title heading"),
            ("g", "miscellaneous information"),
            ("l", "language of a work"),
            ("n", "number of part / section of a work"),
            ("o", "arrangement statement for music"),
            ("p", "name of part / section of a work"),
        ),
        source=(
            "?m bffi:relation [bffi:associatedResource ?target] . "
            "?target bffi:marcKey ?key (where ?key begins with '730') . "
            "All subfields and indicators are read verbatim from ?key."
        ),
        notes=(
            "Indicators and every subfield are reconstructed from "
            "bffi:marcKey verbatim. The auto-table lists the common "
            "subfields seen in the corpus ($a $g $l $n $o $p); any "
            "additional subfield codes carried in bffi:marcKey are "
            "emitted in the order they appear."
        ),
    ),
    MarcEmitMeta(
        tag="740",
        indicators=("0", " "),
        subfields=(
            ("a", "added analytical title"),
            ("n", "number of part / section"),
            ("p", "name of part / section"),
        ),
        source=("Same chain as 730 but with bffi:marcKey beginning with '740'"),
        notes=(
            "Indicators (including nonfiling-character counts in ind1) "
            "and every subfield are reconstructed from bffi:marcKey "
            "verbatim — same shape as 730."
        ),
    ),
)
def _extract_added_titles(graph: Graph, manifestation: URIRef) -> list[_AddedTitleEmit]:
    """Walk the ``bffi:relation`` chain on both the Manifestation and
    its Work, finding every related resource whose ``bffi:marcKey``
    begins with ``"730"`` or ``"740"``. Parse marcKey verbatim for
    indicators and subfields — the structural BFFI walk on
    ``bffi:title / bffi:mainTitle`` would only recover ``$a`` and lose
    ``$g`` / ``$o`` / ``$l`` / ``$n`` / ``$p``, which BFFI has no
    structured predicate for.

    The BFFI ontology declares ``bffi:relation`` over a union of
    {Work, Expression, Manifestation, Item}; marc2bibframe2 attaches
    the related-work links to the Work in practice, so the walk has
    to traverse both sides."""
    anchors: list[URIRef] = [manifestation]
    work = _find_work_for_manifestation(graph, manifestation)
    if work is not None:
        anchors.append(work)

    emits: list[_AddedTitleEmit] = []
    for anchor in anchors:
        for rel in graph.objects(anchor, BFFI.relation):
            for target in graph.objects(rel, BFFI.associatedResource):
                if not isinstance(target, URIRef):
                    continue
                marc_key = next(graph.objects(target, BFFI.marcKey), None)
                if not isinstance(marc_key, Literal):
                    continue
                parsed = _parse_marc_key(str(marc_key))
                if parsed is None:
                    continue
                tag, ind1, ind2, subfields = parsed
                if tag not in ("730", "740"):
                    continue
                if not subfields:
                    continue
                emits.append(_AddedTitleEmit(tag=tag, ind1=ind1, ind2=ind2, subfields=subfields))
    return sorted(emits, key=lambda e: (e.tag, e.subfields))


#: marc2bibframe2 attaches an ``rdf:type <mnotetype/<tail>>`` discriminator
#: on ``bf:Note`` bnodes when the source MARC came from a 5XX with a
#: specific subtype. Map each known tail to its target MARC tag; notes
#: without a recognised tail fall through to 500 (general note).
_MNOTETYPE_TO_MARC_TAG: Final[dict[URIRef, str]] = {
    URIRef("http://id.loc.gov/vocabulary/mnotetype/lang"): "546",
}


@dataclass(frozen=True)
class _NoteEmit:
    """One generic-note datafield (MARC 500 / 546 today)."""

    tag: str
    text: str


@marc_emit(
    MarcEmitMeta(
        tag="500",
        indicators=(" ", " "),
        subfields=(("a", "general note text"),),
        source=(
            "?m bffi:note [a bffi:Note ; rdfs:label ?text] — note bnode "
            "with NO mnotetype rdf:type (the catch-all 5XX)."
        ),
        notes=(
            "Notes typed with a specific mnotetype dispatch to their own "
            "MARC tag (e.g. mnotetype/lang → 546). Others fall through to "
            "500. Per-tail expansion (504 bibliography / 511 participants "
            "/ 520 summary / etc.) is a follow-on."
        ),
    ),
    MarcEmitMeta(
        tag="546",
        indicators=(" ", " "),
        subfields=(("a", "language note text"),),
        source=(
            "?m bffi:note [a bffi:Note, <…/mnotetype/lang> ; "
            "rdfs:label ?text] — note typed with the language tail."
        ),
    ),
)
def _extract_notes(graph: Graph, manifestation: URIRef) -> list[_NoteEmit]:
    """Walk every ``?m bffi:note ?n . ?n rdfs:label ?text`` and dispatch
    to a MARC tag based on the note's mnotetype rdf:type (or 500 by
    default).
    """
    emits: list[_NoteEmit] = []
    for note in graph.objects(manifestation, BFFI.note):
        label = next(graph.objects(note, RDFS.label), None)
        if not isinstance(label, Literal):
            continue
        tag = _note_marc_tag(graph, note)
        emits.append(_NoteEmit(tag=tag, text=str(label)))
    return sorted(emits, key=lambda e: (e.tag, e.text))


def _note_marc_tag(graph: Graph, note: Node) -> str:
    """Return the MARC tag for a ``bffi:Note`` bnode based on its
    mnotetype rdf:type. Falls back to ``"500"`` (general note) when no
    recognised tail is present."""
    for note_type, tag in _MNOTETYPE_TO_MARC_TAG.items():
        if (note, RDF.type, note_type) in graph:
            return tag
    return "500"


@marc_emit(
    MarcEmitMeta(
        tag="505",
        indicators=("0", " "),
        subfields=(("a", "formatted contents note"),),
        source=("?m bffi:tableOfContents [a bffi:TableOfContents ; rdfs:label ?text]"),
    )
)
def _extract_table_of_contents(graph: Graph, manifestation: URIRef) -> list[str]:
    """Return every ``bffi:tableOfContents`` block's ``rdfs:label`` —
    each emits as a MARC 505 datafield carrying the formatted contents
    note text in ``$a``."""
    texts: list[str] = []
    for toc in graph.objects(manifestation, BFFI.tableOfContents):
        label = next(graph.objects(toc, RDFS.label), None)
        if isinstance(label, Literal):
            texts.append(str(label))
    return sorted(texts)


@dataclass(frozen=True)
class _ClassificationEmit:
    """One MARC 084 datafield: portion + optional scheme code."""

    portion: str
    code: str | None


@marc_emit(
    MarcEmitMeta(
        tag="084",
        indicators=(" ", " "),
        subfields=(
            ("a", "classification number"),
            ("2", "scheme code (e.g. 'ykl')"),
        ),
        source=(
            "?m bffi:workManifested ?work . "
            "?work bffi:classification [a bffi:Classification ; "
            "bffi:classificationPortion ?number ; "
            "bffi:source [a bffi:Source ; bffi:code ?code]]"
        ),
        notes=(
            "$2 emitted when bffi:source / bffi:code is present (e.g. 'ykl'); "
            "omitted otherwise. Helmet-local 09X (091/092/094/095/097) "
            "classifications are lost upstream of BFFI (marc2bibframe2 "
            "drops them) — see the Known limitations section below. "
            "Standard 050/080/082 dispatching by source is a follow-on."
        ),
    )
)
def _extract_classifications(graph: Graph, manifestation: URIRef) -> list[_ClassificationEmit]:
    """Walk classification blocks on the Work and return each
    ``bffi:classificationPortion`` paired with its optional scheme code.
    Used for MARC 084 emit."""
    work = _find_work_for_manifestation(graph, manifestation)
    if work is None:
        return []
    emits: list[_ClassificationEmit] = []
    for cls_block in graph.objects(work, BFFI.classification):
        portion = next(graph.objects(cls_block, BFFI.classificationPortion), None)
        if not isinstance(portion, Literal):
            continue
        code: str | None = None
        source_block = next(graph.objects(cls_block, BFFI.source), None)
        if source_block is not None:
            code_lit = next(graph.objects(source_block, BFFI.code), None)
            if isinstance(code_lit, Literal):
                code = str(code_lit)
        emits.append(_ClassificationEmit(portion=str(portion), code=code))
    return sorted(emits, key=lambda e: (e.portion, e.code or ""))


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
@dataclass(frozen=True)
class _IdentifierScheme:
    """MARC datafield shape for one ``bffi:source`` identifier scheme.

    The pair ``(ind1, ind2)`` is fixed per scheme — e.g. EAN is always
    MARC 024 ind1=3 — even though the source-MARC tag (020 / 022 / 024 /
    028) groups several distinct schemes under one numeric tag with the
    indicator picking the kind.
    """

    tag: str
    ind1: str
    ind2: str


_IDENTIFIER_SCHEME_TO_MARC: Final[dict[URIRef, _IdentifierScheme]] = {
    URIRef("http://id.loc.gov/vocabulary/identifiers/isbn"): _IdentifierScheme("020", " ", " "),
    URIRef("http://id.loc.gov/vocabulary/identifiers/issn"): _IdentifierScheme("022", " ", " "),
    # MARC 024 — Other Standard Identifier. ind1 picks the scheme:
    # 1 = UPC, 2 = ISMN, 3 = EAN.
    URIRef("http://id.loc.gov/vocabulary/identifiers/upc"): _IdentifierScheme("024", "1", " "),
    URIRef("http://id.loc.gov/vocabulary/identifiers/ismn"): _IdentifierScheme("024", "2", " "),
    URIRef("http://id.loc.gov/vocabulary/identifiers/ean"): _IdentifierScheme("024", "3", " "),
    # MARC 028 — Publisher / Distributor Number. ind1 picks the kind:
    # 0 = Issue number (audio); 1 = Matrix; 2 = Plate; 3 = Other music;
    # 4 = Videorecording; 5 = Publisher; 6 = Distributor.
    URIRef("http://id.loc.gov/vocabulary/identifiers/audio-issue-number"): _IdentifierScheme(
        "028", "0", "1"
    ),
}


@dataclass(frozen=True)
class _IdentifierEmit:
    """One MARC identifier datafield's worth of content.

    ``assigner`` carries the issuing body's name (the ``rdfs:label`` of
    a ``bffi:Organization`` referenced by ``bffi:assigner``) and emits
    as MARC ``$b`` on schemes where the source field carries it
    (notably 028).
    """

    tag: str
    ind1: str
    ind2: str
    value: str
    assigner: str | None


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

#: Fallback mapping from BFFI subject-node class to MARC tag, used when
#: the subject is an external authority URI (e.g. ``yso/p12148``) and
#: there's no ``#<Type>NNN-N`` URI fragment to extract the tag from.
_SUBJECT_TYPE_TO_MARC_TAG: Final[dict[URIRef, str]] = {
    BFFI.Person: "600",
    BFFI.Organization: "610",
    BFFI.Jurisdiction: "610",
    BFFI.Meeting: "611",
    BFFI.Title: "630",
    BFFI.Temporal: "648",
    BFFI.Topic: "650",
    BFFI.Place: "651",
    BFFI.GenreForm: "655",
}


@dataclass(frozen=True)
class _SubjectEmit:
    """One MARC 6XX subject datafield's worth of content.

    ``vocab_code`` carries the ``$2`` source-vocabulary code (e.g. ``"yso"``,
    ``"ysa"``, ``"slm"``); ``authority_uri`` carries the ``$0`` authority
    URI when the subject in BFFI is anchored on an external concept (e.g.
    ``http://www.yso.fi/onto/yso/p12148``). Either, both, or neither can be
    present — local bib-mint subjects with no ``bffi:source`` will emit
    just ``$a``.
    """

    tag: str
    label: str
    vocab_code: str | None
    authority_uri: str | None


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
    "?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label . "
    "$2 = local-name of ?subject's bffi:source URI when present; "
    "$0 = ?subject URI itself when it's not a bib-internal mint"
)

_SUBJECT_SUBFIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("a", "subject heading / term"),
    ("0", "authority URI for the subject heading"),
    ("2", "source vocabulary code (e.g. 'yso', 'ysa', 'slm')"),
)


@marc_emit(
    MarcEmitMeta(
        tag="600",
        indicators=(" ", " "),
        subfields=_SUBJECT_SUBFIELDS,
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Person`",
    ),
    MarcEmitMeta(
        tag="610",
        indicators=(" ", " "),
        subfields=_SUBJECT_SUBFIELDS,
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Organization`",
    ),
    MarcEmitMeta(
        tag="611",
        indicators=(" ", " "),
        subfields=_SUBJECT_SUBFIELDS,
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Meeting`",
    ),
    MarcEmitMeta(
        tag="630",
        indicators=(" ", " "),
        subfields=_SUBJECT_SUBFIELDS,
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Title`",
    ),
    MarcEmitMeta(
        tag="648",
        indicators=(" ", " "),
        subfields=_SUBJECT_SUBFIELDS,
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Temporal`",
    ),
    MarcEmitMeta(
        tag="650",
        indicators=(" ", " "),
        subfields=_SUBJECT_SUBFIELDS,
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Topic`",
    ),
    MarcEmitMeta(
        tag="651",
        indicators=(" ", " "),
        subfields=_SUBJECT_SUBFIELDS,
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:Place`",
    ),
    MarcEmitMeta(
        tag="655",
        indicators=(" ", " "),
        subfields=_SUBJECT_SUBFIELDS,
        source=f"{_SUBJECT_SOURCE_PREFIX} — `?subject` is typed `bffi:GenreForm`",
    ),
)
def _extract_subject_datafields(graph: Graph, manifestation: URIRef) -> list[_SubjectEmit]:
    """Walk ``?work bffi:subject ?subject_node`` and emit one MARC 6XX
    datafield per subject.

    Tag dispatch: when the subject URI is a bib-internal mint (e.g.
    ``#Agent600-28`` / ``#Topic650-12`` / ``#Place651-30``), the URI
    fragment carries the source MARC tag verbatim. When it's an external
    authority URI (e.g. ``http://www.yso.fi/onto/yso/p12148``), the tag
    is derived from the subject's ``rdf:type``: ``bffi:Topic`` → 650,
    ``bffi:Place`` → 651, etc.

    Subfield emit:
      * ``$a`` from ``rdfs:label`` (mandatory)
      * ``$2`` from the local name of ``bffi:source`` (the LoC scheme URI;
        e.g. ``vocabulary/subjectSchemes/yso`` → ``"yso"``)
      * ``$0`` from the subject URI itself when it's an external authority
        URI rather than a bib-internal mint

    Returns a list of emits — one per subject. Only recognised 6XX tags
    (:data:`_SUBJECT_MARC_TAGS`) are emitted; others are skipped (they
    get their own routing).
    """
    work = _find_work_for_manifestation(graph, manifestation)
    if work is None:
        return []
    emits: list[_SubjectEmit] = []
    for subj_node in graph.objects(work, BFFI.subject):
        if not isinstance(subj_node, URIRef):
            continue
        emit = _build_subject_emit(graph, subj_node)
        if emit is not None:
            emits.append(emit)
    return sorted(emits, key=lambda e: (e.tag, e.label, e.vocab_code or "", e.authority_uri or ""))


def _build_subject_emit(graph: Graph, subj_node: URIRef) -> _SubjectEmit | None:
    """Return the ``_SubjectEmit`` for one ``bffi:subject`` URI, or ``None``
    if it can't be mapped to a 6XX tag or lacks an ``rdfs:label``."""
    tag = _subject_marc_tag(graph, subj_node)
    if tag is None:
        return None
    label = next(graph.objects(subj_node, RDFS.label), None)
    if not isinstance(label, Literal):
        return None
    vocab_code: str | None = None
    source = next(graph.objects(subj_node, BFFI.source), None)
    if isinstance(source, URIRef):
        vocab_code = local_name(source)
    authority_uri: str | None = (
        str(subj_node) if _SUBJECT_TAG_PATTERN.search(str(subj_node)) is None else None
    )
    return _SubjectEmit(
        tag=tag,
        label=str(label),
        vocab_code=vocab_code,
        authority_uri=authority_uri,
    )


def _subject_marc_tag(graph: Graph, subj_node: URIRef) -> str | None:
    """Pick the MARC 6XX tag for a subject URI.

    Prefers the bib-internal URI fragment (``#Topic650-12``) — that's the
    source-MARC tag preserved verbatim by marc2bibframe2. Falls back to
    the subject's ``rdf:type`` when no fragment match exists (the
    external-authority-URI case).
    """
    match = _SUBJECT_TAG_PATTERN.search(str(subj_node))
    if match is not None:
        tag = match.group(1)
        return tag if tag in _SUBJECT_MARC_TAGS else None
    for type_uri, tag in _SUBJECT_TYPE_TO_MARC_TAG.items():
        if (subj_node, RDF.type, type_uri) in graph:
            return tag
    return None


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
    MarcEmitMeta(
        tag="024",
        indicators=("0-3", " "),
        subfields=(("a", "EAN / UPC / ISMN value"),),
        source=(
            "?m bffi:identifiedBy [a bffi:Identifier ; bffi:source <…/identifiers/upc|ismn|ean> ; "
            "rdf:value ?value] — ind1 selects the scheme (1=UPC, 2=ISMN, 3=EAN)."
        ),
    ),
    MarcEmitMeta(
        tag="028",
        indicators=("0-6", "0-3"),
        subfields=(
            ("a", "publisher / distributor number value"),
            ("b", "issuing publisher / distributor name"),
        ),
        source=(
            "?m bffi:identifiedBy [a bffi:Identifier ; "
            "bffi:source <…/identifiers/audio-issue-number> ; rdf:value ?value ; "
            "bffi:assigner [a bffi:Organization ; rdfs:label ?name]] — "
            "ind1=0 for audio issue numbers; ind2=1 = note maker / no added "
            "entry (the Helmet default)."
        ),
    ),
)
def _extract_identifier_datafields(graph: Graph, manifestation: URIRef) -> list[_IdentifierEmit]:
    """Walk every ``bffi:identifiedBy`` block and convert to a MARC
    datafield emit when its ``bffi:source`` URI is in the dispatch table.

    Local IDs (with no ``bffi:source`` or a source not in the table) are
    skipped — they're either the 001-bound bib ID (handled separately)
    or an identifier scheme we don't yet emit. Each additional scheme
    lands by extending :data:`_IDENTIFIER_SCHEME_TO_MARC`.
    """
    emits: list[_IdentifierEmit] = []
    for ident in graph.objects(manifestation, BFFI.identifiedBy):
        source = next(graph.objects(ident, BFFI.source), None)
        if not isinstance(source, URIRef):
            continue
        scheme = _IDENTIFIER_SCHEME_TO_MARC.get(source)
        if scheme is None:
            continue
        value = next(graph.objects(ident, RDF.value), None)
        if not isinstance(value, Literal):
            continue
        emits.append(
            _IdentifierEmit(
                tag=scheme.tag,
                ind1=scheme.ind1,
                ind2=scheme.ind2,
                value=str(value),
                assigner=_extract_assigner_label(graph, ident),
            )
        )
    return emits


def _extract_assigner_label(graph: Graph, ident: Node) -> str | None:
    """Return the ``rdfs:label`` of the identifier's ``bffi:assigner``
    organisation (used for MARC 028 ``$b``), or ``None`` when absent."""
    assigner = next(graph.objects(ident, BFFI.assigner), None)
    if assigner is None:
        return None
    label = next(graph.objects(assigner, RDFS.label), None)
    return str(label) if isinstance(label, Literal) else None


def _append_simple_a_datafields(record: etree._Element, tag: str, values: tuple[str, ...]) -> None:
    """Append one MARC datafield per value, each with a single ``$a``
    subfield carrying the value and blank indicators. Used for MARC
    families whose entire emit shape is a list of bare ``$a`` rows
    (336 / 337 / 338 RDA descriptors today; potentially others)."""
    for value in values:
        df = etree.SubElement(record, f"{_MARC}datafield", tag=tag, ind1=" ", ind2=" ")
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = value


def _append_contributor_datafields(
    record: etree._Element, contributors: Iterable[_ContributorEmit]
) -> None:
    """Append one MARC contributor datafield per emit. Subfield order
    follows the MARC spec: ``$a`` (name) → ``$e`` (relator term, free
    text) → ``$4`` (LoC relator code). Each is optional except ``$a``.
    Indicators are blank — the smart name-type / role indicator
    population is a follow-on."""
    for c in contributors:
        df = etree.SubElement(record, f"{_MARC}datafield", tag=c.tag, ind1=" ", ind2=" ")
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = c.label
        if c.relator_term:
            sf_e = etree.SubElement(df, f"{_MARC}subfield", code="e")
            sf_e.text = c.relator_term
        if c.relator:
            sf_4 = etree.SubElement(df, f"{_MARC}subfield", code="4")
            sf_4.text = c.relator


def _append_identifier_datafields(
    record: etree._Element, identifiers: list[_IdentifierEmit]
) -> None:
    """Append one MARC datafield per identifier emit.

    ``$a`` carries the identifier value; ``$b`` carries the assigner /
    issuing publisher label when present (e.g. MARC 028 ``$b MGM DVD``).
    Indicators come from the per-scheme dispatch table.
    """
    for ident in identifiers:
        df = etree.SubElement(
            record, f"{_MARC}datafield", tag=ident.tag, ind1=ident.ind1, ind2=ident.ind2
        )
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = ident.value
        if ident.assigner is not None:
            sf_b = etree.SubElement(df, f"{_MARC}subfield", code="b")
            sf_b.text = ident.assigner


def _append_note_datafields(record: etree._Element, notes: list[_NoteEmit]) -> None:
    """Append one MARC 5XX-style datafield per note emit (blank indicators)."""
    for note in notes:
        df = etree.SubElement(record, f"{_MARC}datafield", tag=note.tag, ind1=" ", ind2=" ")
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = note.text


def _append_table_of_contents_datafields(
    record: etree._Element, table_of_contents: list[str]
) -> None:
    """Append one MARC 505 datafield per table-of-contents text.

    ind1=0 = "Contents" (the default per the MARC 21 spec).
    """
    for text in table_of_contents:
        df = etree.SubElement(record, f"{_MARC}datafield", tag="505", ind1="0", ind2=" ")
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = text


def _append_publication_datafield(record: etree._Element, publication: _PublicationEmit) -> None:
    """Append the MARC 260 datafield with structured ``$a`` / ``$b`` / ``$c``
    when ``bffi:simplePlace`` / ``bffi:simpleAgent`` / ``bffi:simpleDate``
    are present on the Publication-typed provisionActivity. Falls back to
    a single ``$a`` carrying the flat ``bffi:publicationStatement`` literal
    when the structured parts are absent.

    ISBD trailing punctuation is added per the MARC 260 convention:
    ``$a "Place :"`` precedes ``$b``; ``$b "Publisher,"`` precedes ``$c``.
    No trailing punctuation on the last present subfield.
    """
    df = etree.SubElement(record, f"{_MARC}datafield", tag="260", ind1=" ", ind2=" ")
    if publication.place is None and publication.agent is None and publication.date is None:
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = publication.statement
        return
    if publication.place is not None:
        place_text = publication.place
        if publication.agent is not None:
            place_text += " :"
        elif publication.date is not None:
            place_text += ","
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = place_text
    if publication.agent is not None:
        agent_text = publication.agent
        if publication.date is not None:
            agent_text += ","
        sf_b = etree.SubElement(df, f"{_MARC}subfield", code="b")
        sf_b.text = agent_text
    if publication.date is not None:
        sf_c = etree.SubElement(df, f"{_MARC}subfield", code="c")
        sf_c.text = publication.date


def _append_subject_datafields(record: etree._Element, subjects: list[_SubjectEmit]) -> None:
    """Append one MARC 6XX datafield per subject emit.

    ``$a`` carries the heading text. ``$0`` (authority URI) and ``$2``
    (source-vocabulary code) emit when their respective signals are
    present in BFFI. When ``$2`` is emitted, ``ind2`` is set to ``"7"``
    per the MARC convention ("source specified in subfield $2");
    otherwise ``ind2`` is blank.
    """
    for subj in subjects:
        ind2 = "7" if subj.vocab_code else " "
        df = etree.SubElement(record, f"{_MARC}datafield", tag=subj.tag, ind1=" ", ind2=ind2)
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = subj.label
        if subj.authority_uri:
            sf_0 = etree.SubElement(df, f"{_MARC}subfield", code="0")
            sf_0.text = subj.authority_uri
        if subj.vocab_code:
            sf_2 = etree.SubElement(df, f"{_MARC}subfield", code="2")
            sf_2.text = subj.vocab_code


def _append_classification_datafields(
    record: etree._Element, classifications: list[_ClassificationEmit]
) -> None:
    """Append MARC 084 datafields with ``$a`` portion and optional ``$2`` scheme."""
    for cls in classifications:
        df = etree.SubElement(record, f"{_MARC}datafield", tag="084", ind1=" ", ind2=" ")
        sf_a = etree.SubElement(df, f"{_MARC}subfield", code="a")
        sf_a.text = cls.portion
        if cls.code is not None:
            sf_2 = etree.SubElement(df, f"{_MARC}subfield", code="2")
            sf_2.text = cls.code


def _append_added_title_datafields(
    record: etree._Element, added_titles: list[_AddedTitleEmit]
) -> None:
    """Append 730 / 740 added-title datafields after the 7XX contributor block.

    Indicators and every subfield are taken verbatim from the parsed
    ``bffi:marcKey`` — preserves nonfiling-character ind1 plus $a/$g/$l/$n/$o/$p
    and any other code the source carried."""
    for added in added_titles:
        df = etree.SubElement(
            record, f"{_MARC}datafield", tag=added.tag, ind1=added.ind1, ind2=added.ind2
        )
        for code, value in added.subfields:
            sf = etree.SubElement(df, f"{_MARC}subfield", code=code)
            sf.text = value


def _build_marc_record(
    *,
    bib_id: str,
    change_date: str | None,
    title_parts: _TitleParts | None,
    responsibility: str | None,
    publication: _PublicationEmit | None,
    identifiers: list[_IdentifierEmit],
    language_codes: list[str],
    physical: _PhysicalDescription | None,
    rda: _RdaDescriptors,
    classifications: list[_ClassificationEmit],
    contributors: list[_ContributorEmit],
    subjects: list[_SubjectEmit],
    notes: list[_NoteEmit],
    table_of_contents: list[str],
    added_titles: list[_AddedTitleEmit],
) -> etree._Element:
    """Build one MARCXML ``<record>`` element with the v0+ field set."""
    record = etree.Element(f"{_MARC}record")
    leader = etree.SubElement(record, f"{_MARC}leader")
    leader.text = _LEADER_PLACEHOLDER

    cf001 = etree.SubElement(record, f"{_MARC}controlfield", tag="001")
    cf001.text = bib_id

    if change_date is not None:
        cf005 = etree.SubElement(record, f"{_MARC}controlfield", tag="005")
        cf005.text = change_date

    # 020 / 022 / 024 / 028 identifiers come before 041 / 245 / 300 in
    # MARC tag order. Indicators and the optional $b assigner come from
    # the per-emit fields populated by the scheme dispatcher.
    _append_identifier_datafields(record, identifiers)

    _append_classification_datafields(record, classifications)

    if language_codes:
        df041 = etree.SubElement(record, f"{_MARC}datafield", tag="041", ind1=" ", ind2=" ")
        for code in language_codes:
            sf_a = etree.SubElement(df041, f"{_MARC}subfield", code="a")
            sf_a.text = code

    # Primary contributors (MARC 100/110/111) come before 245 in MARC
    # tag order.
    _append_contributor_datafields(record, (c for c in contributors if c.tag.startswith("1")))

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

    if publication is not None:
        _append_publication_datafield(record, publication)

    if physical is not None:
        df300 = etree.SubElement(record, f"{_MARC}datafield", tag="300", ind1=" ", ind2=" ")
        if physical.extent is not None:
            sf_a = etree.SubElement(df300, f"{_MARC}subfield", code="a")
            sf_a.text = physical.extent
        if physical.dimensions is not None:
            sf_c = etree.SubElement(df300, f"{_MARC}subfield", code="c")
            sf_c.text = physical.dimensions

    # 336/337/338 RDA descriptors. One datafield per code (multiple values
    # on a single predicate produce repeated datafields per MARC convention).
    _append_simple_a_datafields(record, "336", rda.content_codes)
    _append_simple_a_datafields(record, "337", rda.media_codes)
    _append_simple_a_datafields(record, "338", rda.carrier_codes)

    # 500 / 546 notes come after the bibliographic-description block,
    # before 505 (which precedes 6XX subjects per MARC tag order).
    _append_note_datafields(record, notes)
    _append_table_of_contents_datafields(record, table_of_contents)

    # 6XX subjects come after the bibliographic-description block.
    _append_subject_datafields(record, subjects)

    # Added contributors (MARC 700/710/711) come after 6XX subjects.
    _append_contributor_datafields(record, (c for c in contributors if c.tag.startswith("7")))

    _append_added_title_datafields(record, added_titles)

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
    change_date = _extract_change_date(graph, manifestation)
    title_parts = _extract_main_title_parts(graph, manifestation)
    responsibility = _extract_responsibility_statement(graph, manifestation)
    publication = _extract_publication(graph, manifestation)
    identifiers = _extract_identifier_datafields(graph, manifestation)
    language_codes = _extract_language_codes(graph, manifestation)
    physical = _extract_physical_description(graph, manifestation)
    rda = _extract_rda_descriptors(graph, manifestation)
    classifications = _extract_classifications(graph, manifestation)
    contributors = _extract_contributors(graph, manifestation)
    subjects = _extract_subject_datafields(graph, manifestation)
    notes = _extract_notes(graph, manifestation)
    table_of_contents = _extract_table_of_contents(graph, manifestation)
    added_titles = _extract_added_titles(graph, manifestation)
    record = _build_marc_record(
        bib_id=bib_id,
        change_date=change_date,
        title_parts=title_parts,
        responsibility=responsibility,
        publication=publication,
        identifiers=identifiers,
        language_codes=language_codes,
        physical=physical,
        rda=rda,
        classifications=classifications,
        contributors=contributors,
        subjects=subjects,
        notes=notes,
        table_of_contents=table_of_contents,
        added_titles=added_titles,
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
