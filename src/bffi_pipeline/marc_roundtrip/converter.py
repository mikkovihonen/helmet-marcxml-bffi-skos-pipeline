"""BFFI → MARCXML reconstruction.

Given a BFFI canonical graph and a ``bffi:Manifestation`` URI, produce a
MARCXML record that represents what the BFFI graph carries about that
one Helmet bib. Cataloguers compare this against the originating MARC
record to see what the pipeline preserves, loses, transforms, or adds.

Not a production export — the reconstruction is intentionally lossy in
documented ways (MARC indicators, 008 fixed-length subfields, 880
alternate scripts, subject-subfield subdivisions, ...) so the diff
itself becomes the diagnostic that tells cataloguers what BFFI's
representation does to their data.

The converter walks the Manifestation outward:

    Manifestation ── bffi:expressionManifested ──→ Expression
                                                      │
                                              bffi:expressionOf
                                                      ↓
                                                    Work

and emits each MARC field from the appropriate node — title from the
Work's ``skos:prefLabel``, publication statement parsed out of the
Manifestation's prefLabel, media / carrier / digital characteristics
from the Manifestation, primary contribution from the Work's
``bffi:contribution → bffi:PrimaryContribution``, etc.

All emitted ``<datafield>`` blocks carry a ``$5 FI-HELME/bffi-roundtrip``
subfield so cataloguers can tell at-a-glance what was reconstructed by
this module vs what survived the original MARC verbatim. The marker
also lets the diff comparator ignore round-trip-introduced subfields
without false-flagging them as cataloguer-side changes.

P-48 Phase A — **lineage subfield ($9)**. Datafields whose source MARC
field can be traced via M3's raw URI positional fragments
(``#Topic650-N``, ``#Place651-N``, ``#Agent700-N``, etc.) also carry a
``$9 src=<tag>-<ordinal>`` subfield (e.g. ``$9 src=650-20``). The diff
comparator pairs reconstructed fields to source fields by this token
first, falling back to today's tag+$a+position heuristic only for the
lineage-absent residue. ``$9`` is MARC's "local processing" subfield;
lineage values always start with ``src=`` so cataloguer-supplied ``$9``
subfields with other content survive the diff strip untouched. Phase A
covers 6XX + 655 + 7XX (raw URI fragments present); flat Instance-side
fields (020 / 028 / 250 / 490 / 500 / 505 / 264) are unstamped pending
Phase B's ``bffi-prov:fromSourceField`` triples.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final
from xml.etree.ElementTree import Element, SubElement, tostring

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, SKOS
from rdflib.term import Node

from bffi_pipeline.provenance import vocab as V

#: MARCXML namespace per the LoC schema.
MARC_NAMESPACE: Final[str] = "http://www.loc.gov/MARC21/slim"

#: Subfield $5 marker emitted on every reconstructed datafield so the
#: HTML diff (and any downstream consumer) can distinguish a
#: reconstructed-by-BFFI field from an unchanged-from-source field.
ROUNDTRIP_MARKER: Final[str] = "FI-HELME/bffi-roundtrip"

#: Map BFFI language URIs to MARC 008/35-37 (and 041) three-letter codes.
#: LoC publishes this vocabulary at ``id.loc.gov/vocabulary/languages``;
#: the URI's tail IS the MARC code. Kept here so the converter doesn't
#: need network to translate.
_LANGUAGE_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"

#: Indicator codes the converter emits when the original MARC's
#: indicator value is unrecoverable from BFFI. ``#`` is the MARC
#: convention for "indicator undefined / blank".
_INDICATOR_BLANK: Final[str] = " "

#: M3 mints raw URIs under this prefix for cataloguer-typed subjects /
#: genre-forms that have no ``$0`` in the source MARC. M9 then emits
#: ``<raw> skos:exactMatch <authority>`` when it binds the raw URI to
#: a Finto-vocab concept. The converter follows that link so MARC ``$0``
#: carries the authority URI cataloguers care about, not pipeline-
#: internal scaffolding.
_RAW_BIB_URI_PREFIX: Final[str] = "http://urn.fi/URN:NBN:fi:bib:raw/"

#: BIBFRAME ``bf:issuance`` URI tail → MARC LDR/07 character.
#: Covers the dominant Helmet bibliographic-level codes; unknown tails
#: fall back to ``'m'`` (monograph) in :meth:`_bibliographic_level_code`.
_BF_ISSUANCE_TO_LDR07: Final[dict[str, str]] = {
    "mono": "m",  # monograph / single item
    "serial": "s",  # serial
    "integrating": "i",  # integrating resource
    "single": "a",  # monographic component part
    "multi": "m",  # multipart monograph maps to 'm' too
}

#: LoC ``organizations`` URI prefix. The URI tail reduces the MARC
#: organization code by lower-casing and removing hyphens — the
#: ``_ORG_URI_TO_MARC_CODE`` table reverses the dominant Helmet cases.
_ORG_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/organizations/"

#: Reverse lookup for the LoC organizations URI tail → cataloguer
#: MARC code. Covers the dominant Helmet cases. Unknown tails fall
#: back to ``tail.upper()`` (surfaces in the round-trip diff so
#: cataloguers see the heuristic's reach).
#: LoC mnotetype URI tail → MARC 5XX tag mapping. marc2bibframe2
#: emits ``rdf:type <mnotetype/<tail>>`` on each bf:Note to
#: categorise it; ``_marc_5xx_tag_for_note`` uses this table to route
#: notes to the right MARC 5XX field. Tails not in this table fall
#: through to MARC 500 (general note). ``physical`` / ``accmat`` are
#: excluded because they route to MARC 300 $b / $e from
#: ``_emit_extent_and_dimensions``.
_MNOTETYPE_TO_MARC_5XX: Final[dict[str, str]] = {
    "biblio": "504",  # bibliography note
    "thesis": "502",  # dissertation note
    "creditNote": "508",  # creation / production credits
    "participants": "511",  # participants / performers
    "typeOfReport": "513",  # type of report / period
    "summary": "520",  # summary / abstract
    "language": "546",  # language note
    "provenance": "561",  # provenance / immediate source
    "awards": "586",  # awards
    "accessRestrict": "506",  # restrictions on access
    "orig": "534",  # original version note (P-54 Phase 3)
    "descsource": "588",  # source of description note (P-54 Phase 3)
}


_ORG_URI_TO_MARC_CODE: Final[dict[str, str]] = {
    "fimelinda": "FI-MELINDA",
    "fihelme": "FI-HELME",
    "fibtj": "FI-BTJ",
    "finl": "FI-NL",
    "finlfennica": "FI-NL-fennica",
    "fiyle": "FI-YLE",
    "dlc": "DLC",
    "uk": "UK",
    "ukobi": "UkObi",
}

#: Subject / genre-form URI namespace → cataloguer MARC ``$2`` code.
#: Language suffix dropped (``$2 kauno`` not ``$2 kauno/fin``) per
#: modern Finto / RDA convention — older Helmet records used the
#: ``/fin``/``/swe`` long form but the cataloguer-team prefers the
#: unsuffixed code going forward. The URI namespace already encodes
#: the conventional language for language-specific vocabs (kauno =
#: Finnish, bella = Swedish counterpart).
#:
#: Order matters: longer prefixes (``yso-paikat`` / ``yso-aika``)
#: are checked before the bare ``yso`` prefix. Python dict ordering
#: is insertion-order, so the iteration in
#: ``_marc_source_from_uri_namespace`` honours this.
_URI_NAMESPACE_TO_MARC_SOURCE: Final[dict[str, str]] = {
    "http://www.yso.fi/onto/yso-paikat/": "yso",
    "http://www.yso.fi/onto/yso-aika/": "yso",
    "http://www.yso.fi/onto/kauno/": "kauno",
    "http://www.yso.fi/onto/musa/": "musa",
    "http://www.yso.fi/onto/bella/": "bella",
    "http://www.yso.fi/onto/cilla/": "cilla",
    "http://www.yso.fi/onto/allars/": "allars",
    "http://www.yso.fi/onto/kaunokki/": "kaunokki",
    "http://www.yso.fi/onto/yso/": "yso",
    "http://urn.fi/URN:NBN:fi:au:slm:": "slm",
    "http://urn.fi/URN:NBN:fi:au:slm-swe:": "slm",
    "http://id.loc.gov/authorities/subjects/": "lcsh",
    "http://id.loc.gov/authorities/names/": "lcnaf",
    "http://id.loc.gov/vocabulary/genreFormSchemes/lcgft/": "lcgft",
}


#: BFFI subject-class ``rdf:type`` → MARC 6XX tag. Used by the
#: round-trip converter's ``_subject_marc_tag`` to route cataloguer-
#: typed ``$0`` URIs whose namespace alone doesn't reveal the
#: subject kind (the plain ``yso/`` URI case — ``bffi:Place
#: rdf:about`` typing is the discriminator).
#:
#: The classes are ``bffi:*`` after P-53 Family 1 (the
#: BFFI-aliased-terms migration). ``docs/lkd.rdf`` declares each
#: ``owl:equivalentClass`` of its BIBFRAME counterpart.
_SUBJECT_TYPE_TO_MARC_6XX_TAG: Final[dict[URIRef, str]] = {
    V.BFFI.Person: "600",
    V.BFFI.Organization: "610",
    V.BFFI.Meeting: "611",
    V.BFFI.Temporal: "648",
    V.BFFI.Place: "651",
    V.BFFI.Topic: "650",
}

#: BIBFRAME bf:ProvisionActivity subclass tail → MARC 264 ind2.
#: ind2=1 (Publication) is the dominant fallback.
#: Unicode block ranges → MARC 880 ``$6 …/<code>`` script subtag.
#: Used by ``_detect_marc_script_code`` to reconstruct the ``$6``
#: script-code suffix when emitting 880 rows for vernacular
#: literals. The MARC code conventions follow LC's documentation:
#: ``(B`` = Latin, ``(N`` = Cyrillic, ``(3`` = Arabic,
#: ``(2`` = Hebrew, ``(S`` = Greek, ``$1`` = CJK Unified
#: Ideographs. P-54 Phase 7.
_UNICODE_SCRIPT_RANGES_TO_MARC_CODE: Final[tuple[tuple[int, int, str], ...]] = (
    (0x0400, 0x04FF, "(N"),  # Cyrillic
    (0x0500, 0x052F, "(N"),  # Cyrillic supplement
    (0x0590, 0x05FF, "(2"),  # Hebrew
    (0x0600, 0x06FF, "(3"),  # Arabic
    (0x0750, 0x077F, "(3"),  # Arabic supplement
    (0x0370, 0x03FF, "(S"),  # Greek and Coptic
    (0x4E00, 0x9FFF, "$1"),  # CJK Unified Ideographs
    (0x3040, 0x309F, "$1"),  # Hiragana
    (0x30A0, 0x30FF, "$1"),  # Katakana
    (0xAC00, 0xD7AF, "$1"),  # Hangul Syllables
)


def _detect_marc_script_code(text: str) -> str:
    """Return the MARC ``$6 …/<code>`` script subtag for ``text``.

    Scans the literal for the first character in a known non-Latin
    Unicode block and returns the corresponding MARC code; falls
    back to ``(B`` (Latin) when no non-Latin character is found.
    Used to reconstruct the ``$6`` script-code suffix on 880 rows
    emitted from language-tagged vernacular literals (P-54 Phase 7).
    """
    for ch in text:
        cp = ord(ch)
        for low, high, code in _UNICODE_SCRIPT_RANGES_TO_MARC_CODE:
            if low <= cp <= high:
                return code
    return "(B"


#: Source-vocab code → MARC classification tag. Driven by what
#: marc2bibframe2 emits as ``bf:code`` on ``bf:Source`` in the
#: classification chain (P-54 Phase 1A). Helmet-local 09X codes will
#: be added when the M2-post synthesis pass ships (sub-phase 1B).
_CLASSIFICATION_SOURCE_CODE_TO_MARC_TAG: Final[dict[str, str]] = {
    "ykl": "084",
    "udc": "080",
    "dewey": "082",
    "ddc": "082",
    "lcc": "050",
}

_PROVISION_TYPE_TO_IND2: Final[dict[str, str]] = {
    "Production": "0",
    "Publication": "1",
    "Distribution": "2",
    "Manufacture": "3",
    "Copyright": "4",
}


def _marc_source_from_uri_namespace(uri_str: str) -> str | None:
    """Look up the cataloguer MARC ``$2`` source-vocab code from the
    URI namespace. Returns ``None`` when no entry matches."""
    for prefix, code in _URI_NAMESPACE_TO_MARC_SOURCE.items():
        if uri_str.startswith(prefix):
            return code
    return None


#: MARC subfield code used for the round-trip lineage token (P-48 Phase A).
#: ``$9`` is the MARC "local processing" subfield — distinct from the
#: existing ``$5 FI-HELME/bffi-roundtrip`` marker so the two can be
#: inspected independently and cataloguer-supplied ``$9`` subfields with
#: other content survive untouched (the diff comparator strips only
#: ``$9`` values that begin with ``src=``).
LINEAGE_SUBFIELD: Final[str] = "9"

#: Value-prefix marker. Diff-side strip + cataloguer-supplied-$9
#: pass-through both key on this prefix.
LINEAGE_VALUE_PREFIX: Final[str] = "src="

#: P-49 Phase A — round-trip-only sentinel marking that this
#: reconstructed field's subfields were built by parsing
#: ``bflc:marcKey`` rather than from BFFI structured properties.
#: Such rows are flagged ``marckey_bypass`` in the diff regardless
#: of byte-equality with the original — they "pass" only because
#: the cataloguer's original MARC string is smuggled through the
#: BFFI graph and reparsed, not because BFFI faithfully models the
#: bibliographic data. Emitted as a sentinel ``$9`` value alongside
#: the lineage ``$9 src=…`` subfield; both are stripped by the diff
#: parser. Cataloguer-supplied ``$9`` with other content passes
#: through unchanged.
MARCKEY_BYPASS_VALUE: Final[str] = "marckey-bypass"

#: M3's raw-URI positional fragment grammar. Three named groups:
#:   - ``kind``: the M3 routing prefix (Topic / Place / Agent / Hub / ...)
#:   - ``tag``:  the source MARC datafield tag (650 / 651 / 700 / ...)
#:   - ``ord``:  M3's per-record entity counter — monotonic in source
#:               MARC encounter order but **NOT** 1-indexed-within-tag.
#:               A record with 4x 650 + 1x 655 + 9x 700 mints
#:               ``#Topic650-22`` … ``#Topic650-25``, then ``#Agent700-27``
#:               … ``#Agent700-35`` (position 26 = the 655 entity). The
#:               converter normalises this to within-tag rank in the
#:               lineage token it emits, so the diff comparator can
#:               pair against ``$9 src=650-1`` / ``src=700-3`` style
#:               1-indexed ranks (matching its own source-side counter).
_LINEAGE_FRAGMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"#(?P<kind>Topic|Place|Agent|Hub|Work|MusicMedium|IntendedAudience|"
    r"CreatorCharacteristic)(?P<tag>\d{3})-(?P<ord>\d+)$"
)


def _parse_lineage_fragment(source: Node | None) -> tuple[str, int] | None:
    """Parse an M3-minted raw URI fragment into ``(tag, m3_ord)``.

    Returns ``None`` for non-URI inputs, URIs outside the raw-bib
    namespace, and URIs whose fragment doesn't match the M3 positional
    convention. The integer ``m3_ord`` is the per-record entity
    counter — call :meth:`_Reconstructor._lineage_token` to translate
    it into the 1-indexed-within-tag rank the diff comparator pairs on.
    """
    if not isinstance(source, URIRef):
        return None
    s = str(source)
    if not s.startswith(_RAW_BIB_URI_PREFIX):
        return None
    m = _LINEAGE_FRAGMENT_RE.search(s)
    if m is None:
        return None
    return m.group("tag"), int(m.group("ord"))


@dataclass(frozen=True)
class ReconstructedRecord:
    """Output of :func:`reconstruct_marc`. The ``element`` is the in-memory
    ``<record>`` tree; :func:`serialize_marc` writes it to bytes."""

    bib_id: str
    element: Element
    #: Set of MARC tags the converter explicitly chose to skip because
    #: they aren't recoverable from BFFI (e.g. ``"852"`` for holdings).
    #: Surfaced in the diff so cataloguers see which fields the
    #: round-trip can't reproduce vs which it tried-but-failed.
    skipped_tags: tuple[str, ...] = ()


def reconstruct_marc(
    graph: Graph,
    manifestation_uri: URIRef,
    *,
    bib_id: str | None = None,
    lineage_rank_map: dict[str, str] | None = None,
) -> ReconstructedRecord:
    """Reconstruct a MARCXML record from the BFFI graph rooted at the
    given Manifestation. See module docstring for the field coverage.

    ``lineage_rank_map`` (optional) is a pre-built map from raw-bib
    URI string → ``<tag>-<rank>`` lineage token. Pass it in when
    processing many manifestations from the same graph to avoid
    paying the O(graph) build cost per record — see
    :func:`build_lineage_rank_map`. When ``None``, falls back to
    a per-record build (correct but slow on multi-million-triple
    graphs).
    """
    return _Reconstructor(
        graph,
        manifestation_uri,
        bib_id=bib_id,
        _lineage_rank_map=lineage_rank_map or {},
        _lineage_rank_map_was_provided=lineage_rank_map is not None,
    ).build()


def build_lineage_rank_map(graph: Graph) -> dict[str, str]:
    """Build a per-record lineage rank map for every Manifestation in
    ``graph`` in a single pass.

    Returns ``{raw_uri_string: "<tag>-<rank>"}`` where the rank is
    1-indexed-within-tag-bucket per source record (keyed by bib_id
    derived from the raw URI's path segment). Designed for the
    runner to call ONCE after loading canonical + Finto dumps,
    avoiding the O(graph) cost per-record that
    :meth:`_Reconstructor._build_lineage_rank_map` would otherwise
    pay 500x on a 500-record corpus.

    Implementation: iterate the graph's distinct subjects once,
    cheap prefix-reject everything not in the raw-bib namespace,
    parse the matching ones, group by (bib_id, tag), sort by M3
    ordinal, assign ranks.
    """
    by_record_tag: dict[tuple[str, str], list[tuple[int, str]]] = {}
    seen: set[URIRef] = set()
    iterables = (graph.subjects(), graph.objects())
    for source in iterables:
        for node in source:
            if not isinstance(node, URIRef) or node in seen:
                continue
            seen.add(node)
            s_str = str(node)
            if not s_str.startswith(_RAW_BIB_URI_PREFIX):
                continue
            suffix = s_str[len(_RAW_BIB_URI_PREFIX) :]
            if "#" not in suffix:
                continue
            bib_id, fragment = suffix.split("#", 1)
            m = _LINEAGE_FRAGMENT_RE.match("#" + fragment)
            if m is None:
                continue
            by_record_tag.setdefault((bib_id, m.group("tag")), []).append(
                (int(m.group("ord")), s_str)
            )
    out: dict[str, str] = {}
    for (_bib_id, tag), items in by_record_tag.items():
        for rank, (_, uri_str) in enumerate(sorted(items), start=1):
            out[uri_str] = f"{tag}-{rank}"
    return out


def serialize_marc(record: ReconstructedRecord) -> bytes:
    """Serialise the reconstructed record as MARCXML bytes (UTF-8,
    pretty-printed). Suitable for writing to disk + diffing against
    the original."""
    _indent(record.element)
    blob: bytes = tostring(record.element, encoding="utf-8", xml_declaration=True)
    return blob


# --- Internal -----------------------------------------------------------


@dataclass
class _Reconstructor:
    graph: Graph
    manifestation: URIRef
    bib_id: str | None = None
    skipped: list[str] = field(default_factory=list)
    #: Pre-computed translation from M3 raw URI (full string) to the
    #: 1-indexed-within-tag lineage token (e.g. ``"700-3"``). When
    #: callers pass a pre-built map (via :func:`reconstruct_marc`'s
    #: ``lineage_rank_map`` keyword), the per-record build below is
    #: skipped — critical for performance on multi-million-triple
    #: graphs where iterating the full subject/object index per
    #: record would cost ~30 s x N records.
    _lineage_rank_map: dict[str, str] = field(default_factory=dict)
    _lineage_rank_map_was_provided: bool = False

    def build(self) -> ReconstructedRecord:
        if not self._lineage_rank_map_was_provided:
            self._lineage_rank_map = self._build_lineage_rank_map()
        bib_id = self.bib_id or self._discover_bib_id()
        record = Element(f"{{{MARC_NAMESPACE}}}record")

        leader = self._make_leader()
        SubElement(record, f"{{{MARC_NAMESPACE}}}leader").text = leader

        if bib_id:
            self._emit_controlfield(record, "001", bib_id)
            self._emit_controlfield(record, "003", "FI-HELME")
        cf008 = self._make_008()
        if cf008:
            self._emit_controlfield(record, "008", cf008)

        self._emit_isbns(record)
        self._emit_issns(record)  # 022
        self._emit_other_std_identifiers(record)  # 024
        self._emit_classifications(record)  # 080 / 082 / 084 (+ later 091..097)
        self._emit_publisher_numbers(record)  # 028
        self._emit_system_control_numbers(record)  # 035
        self._emit_helmet_source_marker(record)  # 040 synth marker
        self._emit_languages(record)  # 041
        self._emit_primary_contribution(record)  # 100
        self._emit_uniform_title(record)  # 240
        self._emit_title(record)  # 245
        self._emit_variant_title(record)  # 246
        self._emit_edition_statement(record)  # 250
        self._emit_publication_statement(record)  # 260
        self._emit_extent_and_dimensions(record)  # 300
        self._emit_content_type(record)  # 336
        self._emit_media_type(record)  # 337
        self._emit_carrier_type(record)  # 338
        self._emit_digital_characteristic(record)  # 347
        self._emit_sound_characteristic(record)  # 344
        self._emit_color_content(record)  # 346
        self._emit_series_statement(record)  # 490
        self._emit_notes(record)  # 500
        self._emit_table_of_contents(record)  # 505
        self._emit_subjects(record)  # 6XX
        self._emit_genre_forms(record)  # 655
        self._emit_added_entries(record)  # 700/710 etc.
        self._emit_aggregated_component_analytical_entries(record)  # 700 ind2=2
        self._emit_related_uniform_titles(record)  # 730
        self._emit_vernacular_880(record)  # 880 — P-54 Phase 7
        self._emit_bib_id_local(record, bib_id)  # 907 Helmet display form

        # Holdings (852) explicitly skipped — BFFI doesn't model Items
        # in v1; future P-46 may. Surface in the diff.
        self.skipped.append("852")

        return ReconstructedRecord(
            bib_id=bib_id or "",
            element=record,
            skipped_tags=tuple(self.skipped),
        )

    # --- Source walks ---------------------------------------------------

    @property
    def expression(self) -> URIRef | None:
        for e in self.graph.objects(self.manifestation, V.BFFI.expressionManifested):
            if isinstance(e, URIRef):
                return e
        return None

    @property
    def work(self) -> URIRef | None:
        expr = self.expression
        if expr is None:
            return None
        for w in self.graph.objects(expr, V.BFFI.expressionOf):
            if isinstance(w, URIRef):
                return w
        return None

    def _discover_bib_id(self) -> str:
        for lit in self.graph.objects(self.manifestation, DCTERMS.identifier):
            if isinstance(lit, Literal):
                return str(lit)
        return ""

    # --- Field emitters -------------------------------------------------

    def _make_leader(self) -> str:
        """Build a 24-char MARC bib leader.

        Reconstructable positions:

          05  record status — derived from ``bf:status`` URI on the
              AdminMetadata block whose ``bf:date`` is the source
              005 timestamp (the source-side AdminMetadata).
              Defaults to ``'n'`` (new) when no source signal.
          06  type of record (LDR/06) — derived from the canonical
              ``bffi:Work``'s BIBFRAME secondary ``rdf:type``
              (``bf:MusicAudio`` → 'j', ``bf:Cartography`` → 'e',
              etc.). Defaults to ``'a'`` (language material).
          07  bibliographic level — derived from the Manifestation's
              ``bf:issuance`` URI tail (``mono`` → 'm', ``serial``
              → 's', etc.). Defaults to ``'m'`` (monograph).
          08  type of control — ``'a'`` (archival) when any of the
              AdminMetadata's ``bffi:descriptionConventions`` URIs is
              the LoC DACS vocabulary entry; else blank.
          09  character coding scheme — fixed at ``'a'`` (UTF-8).
              MARCXML is always UTF-8 in this pipeline; the legacy
              MARC-8 form (blank) doesn't apply.
          17  encoding level — derived from the AdminMetadata's
              ``bffi:encodingLevel`` triple pointing at
              ``id.loc.gov/vocabulary/menclvl/<code>``; the URI tail
              is the LDR/17 character verbatim. Defaults to blank
              (full level).
          10-11, 20-23 — fixed (``22``, ``4500``); MARC structural.

        Positions left blank (no reliable BIBFRAME signal):

          18  descriptive cataloguing form (marc2bibframe2 emits
              only the pipeline's own conventions, not the source's)
          19  multipart resource record level — would require a
              multipart-structure model (bf:Item + bf:hasPart). The
              BFFI Item class is intentionally deferred under P-46;
              LDR/19 round-trip ships with it. See
              ``docs/plans/proposed/p-46-bffi-item-class.md``.

        Length placeholder positions 0-4 + base-address 12-16 stay
        zero-padded; serialisers usually rewrite these.
        """
        status_char = self._record_status_code()
        type_char = self._record_type_code()
        bib_level_char = self._bibliographic_level_code()
        control_char = self._type_of_control_code()
        enc_char = self._encoding_level_code()
        # MARC bib leader layout (24 chars total):
        # 00-04 record length placeholder ``00000``
        # 05    record status (this method computes)
        # 06    type of record (this method computes)
        # 07    bibliographic level (this method computes)
        # 08    type of control (this method computes)
        # 09    character coding scheme (``a`` = UTF-8, fixed)
        # 10-11 ``22`` (indicator + subfield-code counts)
        # 12-16 base-address placeholder ``00000``
        # 17    encoding level (this method computes)
        # 18-19 spaces (descriptive cataloging form + multipart)
        # 20-23 ``4500`` (MARC structural fixed suffix)
        return (
            f"00000{status_char}{type_char}{bib_level_char}{control_char}a2200000{enc_char}  4500"
        )

    #: URI for the DACS (Describing Archives: A Content Standard)
    #: descriptionConventions vocabulary entry. Presence in any of the
    #: AdminMetadata's ``bffi:descriptionConventions`` triples is the
    #: cataloguer's signal that this record describes archival
    #: material — translates to MARC LDR/08 = 'a'.
    _DACS_URI: Final[str] = "http://id.loc.gov/vocabulary/descriptionConventions/dacs"

    def _type_of_control_code(self) -> str:
        """Return the MARC LDR/08 character (type of control).

        ``'a'`` (archival) when any of the Manifestation's
        AdminMetadata blocks carries
        ``bffi:descriptionConventions <…/descriptionConventions/dacs>``.
        Defaults to blank — the dominant Helmet case (and per MARC
        spec, "no specified type" means non-archival).

        The DACS URI is the cataloguer's authoritative signal for
        archival cataloguing — typed as MARC 040 $e ``dacs`` which
        marc2bibframe2 routes into ``bf:descriptionConventions``.
        Records can carry multiple convention URIs (e.g. RDA + DACS
        when the cataloguer says both apply); we look for DACS in
        any of them.
        """
        dacs = URIRef(self._DACS_URI)
        for admin in self.graph.objects(self.manifestation, V.BFFI.adminMetadata):
            for conv in self.graph.objects(admin, V.BFFI.descriptionConventions):
                if conv == dacs:
                    return "a"
        return " "

    def _record_status_code(self) -> str:
        """Return the MARC LDR/05 character (record status).

        Walks the Manifestation's AdminMetadata blocks looking for
        one with a ``bf:status`` URI from the LoC ``mstatus``
        vocabulary — the URI tail IS the LDR/05 character. Defaults
        to ``'n'`` (new) when no source signal.

        M3 routes ``bf:status`` only from the source-side
        AdminMetadata (the block with ``bf:date``), so this walk
        won't pick up marc2bibframe2's own conversion-event status.
        """
        mstatus_prefix = "http://id.loc.gov/vocabulary/mstatus/"
        for admin in self.graph.objects(self.manifestation, V.BFFI.adminMetadata):
            for status in self.graph.objects(admin, V.BFFI.status):
                if not isinstance(status, URIRef):
                    continue
                s = str(status)
                if s.startswith(mstatus_prefix):
                    tail = s[len(mstatus_prefix) :]
                    if len(tail) == 1:
                        return tail
        return "n"

    def _bibliographic_level_code(self) -> str:
        """Return the MARC LDR/07 character (bibliographic level).

        Walks the Manifestation's ``bf:issuance`` URIs; takes the
        URI tail and maps it via :data:`_BF_ISSUANCE_TO_LDR07`.
        Defaults to ``'m'`` (monograph) when no source signal —
        Helmet's dominant pattern.
        """
        issuance_prefix = "http://id.loc.gov/vocabulary/issuance/"
        for issuance in self.graph.objects(self.manifestation, V.BF.issuance):
            if not isinstance(issuance, URIRef):
                continue
            s = str(issuance)
            if s.startswith(issuance_prefix):
                tail = s[len(issuance_prefix) :]
                code = _BF_ISSUANCE_TO_LDR07.get(tail)
                if code is not None:
                    return code
        return "m"

    #: BIBFRAME secondary ``rdf:type`` → MARC LDR/06 character.
    #: Priority is the iteration order: more specific types win when a
    #: Work carries multiple typing triples (e.g. ``bf:MusicAudio``
    #: beats ``bf:Audio`` so a CD reads 'j' not 'i').
    _BF_TYPE_TO_LDR06: Final[tuple[tuple[str, str], ...]] = (
        ("MusicAudio", "j"),  # musical sound recording
        ("NotatedMusic", "c"),  # notated music
        ("NotatedMovement", "c"),  # notated movement (also code 'c')
        ("Cartography", "e"),  # cartographic material
        ("MovingImage", "g"),  # projected medium
        ("StillImage", "k"),  # 2-D nonprojectable graphic
        ("Audio", "i"),  # nonmusical sound recording
        ("Multimedia", "m"),  # computer file
        ("Dataset", "m"),  # computer file (data-only)
        ("Object", "r"),  # 3-D artifact
        ("MixedMaterial", "p"),  # mixed material
        ("Text", "a"),  # language material (text)
    )

    def _record_type_code(self) -> str:
        """Return the MARC LDR/06 character for this record's Work.

        Walks the canonical Work's ``rdf:type`` triples and picks the
        most specific BIBFRAME type that maps to a known LDR/06 code.
        Defaults to ``'a'`` (language material) — Helmet's dominant
        pattern when no specific type is recorded.
        """
        work = self.work
        if work is None:
            return "a"
        present: set[str] = set()
        bf_prefix = str(V.BF)
        for t in self.graph.objects(work, RDF.type):
            if isinstance(t, URIRef):
                s = str(t)
                if s.startswith(bf_prefix):
                    present.add(s[len(bf_prefix) :])
        for kind, code in self._BF_TYPE_TO_LDR06:
            if kind in present:
                return code
        return "a"

    def _encoding_level_code(self) -> str:
        """Return the MARC LDR/17 character.

        Walks the Manifestation's AdminMetadata blocks for any
        ``bffi:encodingLevel`` triple pointing at
        ``id.loc.gov/vocabulary/menclvl/<code>``; the URI tail IS the
        LDR/17 character. Coexists with our pipeline's
        ``enc-level/auto`` marker (a separate ``bffi:EncodingLevel``
        URI in the ``bib:`` namespace) — we ignore non-``menclvl``
        URIs since only the LoC menclvl vocab values are valid
        LDR/17 characters.

        Returns ``' '`` (blank — "full level") when no source value is
        available; this matches the cataloguing default for records
        without an explicit encoding-level mark.
        """
        menclvl_prefix = "http://id.loc.gov/vocabulary/menclvl/"
        for admin in self.graph.objects(self.manifestation, V.BFFI.adminMetadata):
            for lvl in self.graph.objects(admin, V.BFFI.encodingLevel):
                if not isinstance(lvl, URIRef):
                    continue
                s = str(lvl)
                if s.startswith(menclvl_prefix):
                    tail = s[len(menclvl_prefix) :]
                    if len(tail) == 1:
                        return tail
        return " "

    def _make_008(self) -> str:
        """MARC 008 — 40-char fixed-length data elements. Positions we
        reconstruct:

          00-05 Date entered on file (YYMMDD) — from
                ``bffi:transactionDate`` (the source 005 timestamp
                that marc2bibframe2 mirrors onto bf:adminMetadata's
                ``bf:date`` on the ``status=n`` block).
          06    Type of date — default ``s`` (single date). BFFI
                doesn't carry a 1:1 mapping; ``s`` matches the
                dominant Helmet case.
          07-10 Date1 (publication year, 4-char) — from the first
                ``bffi:provisionActivity`` bnode's ``bf:date`` typed
                value (clean 4-char) with fallback to digits scraped
                from ``bflc:simpleDate`` (``"c1997"`` → ``"1997"``).
          15-17 Place of publication (3-char MARC country code) —
                URI tail of the first ``bf:place`` on a
                ``bffi:provisionActivity`` bnode (``…/countries/xxk``
                → ``"xxk"``). Pad to 3 chars when shorter.
          35-37 Primary language (3-char) — already lifted from
                ``bffi:language`` URI tail.

        Other positions (audience, form, content type, literary
        form, biography, modified, source) require BIBFRAME → MARC
        mappings that aren't 1:1; left blank, the diff surfaces
        each gap so cataloguers see what BFFI represents elsewhere.
        """
        s = list(" " * 40)
        entered = self._transaction_date_yymmdd()
        for i, c in enumerate(entered):
            s[i] = c
        s[6] = "s"
        date1 = self._publication_date1()
        for i, c in enumerate(date1[:4]):
            s[7 + i] = c
        country = self._publication_country_code()
        for i, c in enumerate(country[:3]):
            s[15 + i] = c
        lang_code = self._primary_language_code() or "   "
        for i, c in enumerate(lang_code[:3]):
            s[35 + i] = c
        return "".join(s)

    #: Minimum literal length for an ISO date that carries YYYY-MM-DD
    #: at positions 0-9. Used to validate transactionDate input.
    _ISO_DATE_MIN_LEN: Final[int] = 10
    #: Year is 4 chars in MARC 008 Date1 (pos 07-10).
    _YEAR_LEN: Final[int] = 4

    def _transaction_date_yymmdd(self) -> str:
        """Format the source description's last-change date into the
        6-char MARC 008 pos 00-05 representation. The date lives on
        the AdminMetadata block as ``bffi:changeDate`` (lkd.rdf
        canonical name), routed from the source MARC 005 transaction
        date at M3. Returns blank when no source date is available.
        """
        for admin in self.graph.objects(self.manifestation, V.BFFI.adminMetadata):
            for d in self.graph.objects(admin, V.BFFI.changeDate):
                if not isinstance(d, Literal):
                    continue
                s = str(d)
                # Accept either "YYYY-MM-DDThh:mm:ss" or "YYYY-MM-DD"
                if len(s) < self._ISO_DATE_MIN_LEN or s[4] != "-" or s[7] != "-":
                    continue
                return f"{s[2:4]}{s[5:7]}{s[8:10]}"
        return "      "

    def _publication_date1(self) -> str:
        """Date1 (4-char publication year) for 008 pos 07-10. Prefer
        the typed ``bf:date`` on a ``bffi:provisionActivity`` bnode;
        fall back to extracting 4 consecutive digits from
        ``bflc:simpleDate`` ("c1997" → "1997")."""
        for prov in self.graph.objects(self.manifestation, V.BFFI.provisionActivity):
            for d in self.graph.objects(prov, V.BF.date):
                if isinstance(d, Literal):
                    val = str(d).strip()
                    if len(val) >= self._YEAR_LEN and val[: self._YEAR_LEN].isdigit():
                        return val[: self._YEAR_LEN]
        for prov in self.graph.objects(self.manifestation, V.BFFI.provisionActivity):
            for d in self.graph.objects(prov, V.BFLC.simpleDate):
                if isinstance(d, Literal):
                    digits = "".join(c for c in str(d) if c.isdigit())
                    if len(digits) >= self._YEAR_LEN:
                        return digits[: self._YEAR_LEN]
        return "    "

    _COUNTRIES_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/countries/"

    def _publication_country_code(self) -> str:
        """Country code for 008 pos 15-17 — URI tail of a
        ``bffi:provisionActivity → bf:place`` LoC countries URI."""
        for prov in self.graph.objects(self.manifestation, V.BFFI.provisionActivity):
            for p in self.graph.objects(prov, V.BF.place):
                if isinstance(p, URIRef) and str(p).startswith(self._COUNTRIES_URI_PREFIX):
                    tail = str(p)[len(self._COUNTRIES_URI_PREFIX) :]
                    return f"{tail:<3}"[:3]
        return "   "

    def _primary_language_code(self) -> str | None:
        expr = self.expression
        if expr is None:
            return None
        for lang in self.graph.objects(expr, V.BFFI.language):
            s = str(lang)
            if s.startswith(_LANGUAGE_URI_PREFIX):
                return s.removeprefix(_LANGUAGE_URI_PREFIX)
        return None

    def _emit_controlfield(self, record: Element, tag: str, value: str) -> None:
        # ``tag`` is both an XML attribute name in MARC and the positional
        # element-name parameter of ``SubElement`` — pass it via the
        # attrib dict to avoid the keyword collision.
        cf = SubElement(record, f"{{{MARC_NAMESPACE}}}controlfield", attrib={"tag": tag})
        cf.text = value

    def _emit_datafield(
        self,
        record: Element,
        tag: str,
        *subfields: tuple[str, str],
        ind1: str = _INDICATOR_BLANK,
        ind2: str = _INDICATOR_BLANK,
        add_marker: bool = True,
        lineage: str | None = None,
        marckey_bypass: bool = False,
    ) -> None:
        df = SubElement(
            record,
            f"{{{MARC_NAMESPACE}}}datafield",
            attrib={"tag": tag, "ind1": ind1, "ind2": ind2},
        )
        for code, value in subfields:
            if value is None or value == "":
                continue
            sf = SubElement(df, f"{{{MARC_NAMESPACE}}}subfield", attrib={"code": code})
            sf.text = value
        # P-48 Phase A: lineage token immediately precedes the marker,
        # so the two round-trip-only subfields stay grouped at the
        # tail of the datafield for easy visual + programmatic
        # inspection.
        if lineage is not None:
            sf = SubElement(
                df,
                f"{{{MARC_NAMESPACE}}}subfield",
                attrib={"code": LINEAGE_SUBFIELD},
            )
            sf.text = f"{LINEAGE_VALUE_PREFIX}{lineage}"
        # P-49 Phase A: marcKey-bypass sentinel — only emitted when the
        # converter built this row's subfields from bflc:marcKey rather
        # than from BFFI structured properties. The diff flags such
        # rows as ``marckey_bypass`` so the audit is visible.
        if marckey_bypass:
            sf = SubElement(
                df,
                f"{{{MARC_NAMESPACE}}}subfield",
                attrib={"code": LINEAGE_SUBFIELD},
            )
            sf.text = MARCKEY_BYPASS_VALUE
        if add_marker:
            sf = SubElement(df, f"{{{MARC_NAMESPACE}}}subfield", attrib={"code": "5"})
            sf.text = ROUNDTRIP_MARKER

    #: Subfields routed by separate helpers (``_collect_role_subs``,
    #: ``_collect_agent_id_subs``, ``_first_source``). When parsing
    #: ``bflc:marcKey`` for name-component subfields on 6XX subjects
    #: and 7XX added entries, we filter these out so they don't
    #: double-emit alongside the dedicated routing.
    _MARC_KEY_NON_NAME_SUBFIELDS: Final[frozenset[str]] = frozenset({"e", "4", "0", "2"})

    def _name_subfields_from_marc_key(self, node: Node) -> list[tuple[str, str]] | None:
        """Parse ``bflc:marcKey`` on ``node`` and return the
        name-component subfields in source-MARC order, excluding
        subfields that route through dedicated helpers (``$e`` /
        ``$4`` role, ``$0`` identifier, ``$2`` source).

        Returns ``None`` when no marcKey is present so callers can
        fall back to the label-only path. Use to recover ``$c``,
        ``$d``, ``$q``, ``$t``, ``$n``, ``$l`` etc. that marc2bibframe2
        collapses into a single ``rdfs:label`` on agent / subject
        nodes — e.g. source ``600 $aMikki Hiiri $c(fiktiivinen hahmo)``
        becomes ``rdfs:label "Mikki Hiiri (fiktiivinen hahmo)"`` +
        ``bflc:marcKey "60004$aMikki Hiiri$c(fiktiivinen hahmo)"``,
        and only marcKey preserves the $a/$c boundary.
        """
        for mk in self.graph.objects(node, V.BFLC.marcKey):
            if not isinstance(mk, Literal):
                continue
            parsed = _parse_marc_key_subfields(str(mk))
            if not parsed:
                continue
            return [(c, v) for c, v in parsed if c not in self._MARC_KEY_NON_NAME_SUBFIELDS]
        return None

    def _emit_isbns(self, record: Element) -> None:
        # bf:identifiedBy → bf:Isbn → rdf:value on the Manifestation.
        # Plus ``bf:qualifier`` (MARC 020 $q, "kovakantinen" /
        # "nidottu" / "pehmeäkantinen") when the cataloguer typed one.
        for ident in self.graph.objects(self.manifestation, V.BFFI.identifiedBy):
            types = set(self.graph.objects(ident, RDF.type))
            if V.BF.Isbn not in types:
                continue
            for value in self.graph.objects(ident, RDF.value):
                if not isinstance(value, Literal):
                    continue
                subs: list[tuple[str, str]] = [("a", str(value))]
                for qual in self.graph.objects(ident, V.BFFI.qualifier):
                    if isinstance(qual, Literal):
                        subs.append(("q", str(qual)))
                self._emit_datafield(record, "020", *subs, lineage=self._lineage_token(ident))

    def _emit_classifications(self, record: Element) -> None:
        """MARC 080 / 082 / 084 — classification numbers, plus
        (later) Helmet-local 091 / 092 / 093 / 094 / 095 / 097.

        ``bf:Work → bffi:classification → bffi:Classification`` with
        ``bffi:classificationPortion <number>`` and ``bf:source →
        bffi:Source → bffi:code <vocab>``. The source-code value
        decides the MARC tag:

          - ``ykl`` → 084 (Finnish library classification, 98.49 %
            of corpus)
          - ``udc`` → 080 (Universal Decimal Classification)
          - ``dewey`` → 082 (Dewey Decimal Classification)
          - ``lcc`` → 050 (Library of Congress Classification)
          - Helmet-local source URIs (091/092/093/094/095/097) →
            the corresponding 09X tag

        Notes on coverage gap: marc2bibframe2's
        ``ConvSpec-050-088.xsl`` only handles 084 with `$2`; the
        Helmet-local 09X classifications without ``$2`` are dropped
        at the BIBFRAME boundary. A M2-post pass that mints
        ``bf:Classification`` nodes from the source MARC will fill
        the 09X gap; until that lands, this emitter handles only
        the codes marc2bibframe2 captures.
        """
        work = self.work
        if work is None:
            return
        for class_node in self.graph.objects(work, V.BFFI.classification):
            portion = next(
                (
                    str(p)
                    for p in self.graph.objects(class_node, V.BFFI.classificationPortion)
                    if isinstance(p, Literal)
                ),
                None,
            )
            if portion is None:
                continue
            source_code: str | None = None
            for source_node in self.graph.objects(class_node, V.BF.source):
                for c in self.graph.objects(source_node, V.BFFI.code):
                    if isinstance(c, Literal):
                        source_code = str(c).strip().lower()
                        break
                if source_code:
                    break
            tag = _CLASSIFICATION_SOURCE_CODE_TO_MARC_TAG.get(source_code or "")
            if tag is None:
                continue
            subs: list[tuple[str, str]] = [("a", portion)]
            if source_code:
                subs.append(("2", source_code))
            self._emit_datafield(record, tag, *subs, lineage=self._lineage_token(class_node))

    def _emit_issns(self, record: Element) -> None:
        """MARC 022 — ISSN (serials). ``bf:identifiedBy → bf:Issn →
        rdf:value`` on the Manifestation. P-54 Phase 2.
        """
        for ident in self.graph.objects(self.manifestation, V.BFFI.identifiedBy):
            types = set(self.graph.objects(ident, RDF.type))
            if V.BF.Issn not in types:
                continue
            for value in self.graph.objects(ident, RDF.value):
                if not isinstance(value, Literal):
                    continue
                self._emit_datafield(
                    record, "022", ("a", str(value)), lineage=self._lineage_token(ident)
                )

    def _emit_other_std_identifiers(self, record: Element) -> None:
        """MARC 024 — Other Standard Identifier (EANs on commercial
        physical media + various other standard ids). marc2bibframe2
        types these as ``bf:Ean`` (ind1=3) or ``bf:OtherIdentifier``
        (other ind1 values); the round-trip emits both as MARC 024
        with ind1 derived from the typing. P-54 Phase 2.
        """
        for ident in self.graph.objects(self.manifestation, V.BFFI.identifiedBy):
            types = set(self.graph.objects(ident, RDF.type))
            if V.BF.Ean in types:
                ind1 = "3"
            elif V.BF.OtherIdentifier in types:
                ind1 = "8"  # default to "Unspecified type" when ind1 wasn't preserved
            else:
                continue
            for value in self.graph.objects(ident, RDF.value):
                if not isinstance(value, Literal):
                    continue
                self._emit_datafield(
                    record,
                    "024",
                    ("a", str(value)),
                    ind1=ind1,
                    lineage=self._lineage_token(ident),
                )

    def _emit_publisher_numbers(self, record: Element) -> None:
        # MARC 028 publisher number / catalog number (music + video).
        # marc2bibframe2 emits ``bf:identifiedBy [a bf:AudioIssueNumber;
        # rdf:value "AM950224"]`` on bf:Instance — same shape as bf:Isbn,
        # different type. ind1=0 (issue number) is the dominant
        # cataloguer choice for audio; ind2=1 ("number, no note") is
        # the common Helmet choice but we drop to blank since we don't
        # carry that flag through.
        for ident in self.graph.objects(self.manifestation, V.BFFI.identifiedBy):
            types = set(self.graph.objects(ident, RDF.type))
            if V.BF.AudioIssueNumber in types:
                for value in self.graph.objects(ident, RDF.value):
                    if isinstance(value, Literal):
                        self._emit_datafield(
                            record,
                            "028",
                            ("a", str(value)),
                            ind1="0",
                            lineage=self._lineage_token(ident),
                        )

    def _assigner_marc_code(self, assigner: Node) -> str | None:
        """Resolve a ``bf:assigner`` to the cataloguer-typed
        organization code (the bit inside ``$a (CODE)VALUE``).

        Two shapes:
          - URI form: LoC organizations URI (`<…/organizations/fimelinda>`).
            Looks up the curated table; falls back to upper-cased tail.
          - Blank-node ``bf:Agent`` form: read ``bf:code`` directly.
        """
        if isinstance(assigner, URIRef):
            s = str(assigner)
            if s.startswith(_ORG_URI_PREFIX):
                tail = s[len(_ORG_URI_PREFIX) :]
                return _ORG_URI_TO_MARC_CODE.get(tail, tail.upper())
            return None
        for code in self.graph.objects(assigner, V.BFFI.code):
            if isinstance(code, Literal):
                return str(code)
        return None

    def _emit_system_control_numbers(self, record: Element) -> None:
        """MARC 035 — system control numbers. ``bf:Instance →
        bf:identifiedBy → bf:Local`` with ``bf:assigner`` (URI or
        Agent bnode with ``bf:code``). Format: ``$a (CODE)VALUE``.

        The Helmet bib_id (also a bf:Local on bf:Instance) is
        excluded by requiring ``bf:assigner`` — Helmet uses
        ``bf:source <…/source:helmet>`` instead, with no assigner.
        """
        for ident in self.graph.objects(self.manifestation, V.BFFI.identifiedBy):
            types = set(self.graph.objects(ident, RDF.type))
            if V.BFFI.Local not in types:
                continue
            assigner = next(iter(self.graph.objects(ident, V.BFFI.assigner)), None)
            if assigner is None:
                continue
            code = self._assigner_marc_code(assigner)
            value = next(
                (str(v) for v in self.graph.objects(ident, RDF.value) if isinstance(v, Literal)),
                None,
            )
            if value is None:
                continue
            formatted = f"({code}){value}" if code else value
            self._emit_datafield(
                record, "035", ("a", formatted), lineage=self._lineage_token(ident)
            )

    def _emit_edition_statement(self, record: Element) -> None:
        # MARC 250 edition statement. marc2bibframe2 emits a flat
        # literal ``bf:editionStatement`` on bf:Instance, which M3's
        # Manifestation pass forwards onto bffi:editionStatement.
        # Single $a per row; Helmet rarely splits edition + responsibility
        # into 250 $a / $b so we don't either.
        for stmt in self.graph.objects(self.manifestation, V.BFFI.editionStatement):
            if isinstance(stmt, Literal):
                self._emit_datafield(record, "250", ("a", str(stmt)))

    def _emit_series_statement(self, record: Element) -> None:
        """MARC 490 (series statement, untraced) OR MARC 830 (series
        added entry, uniform title). The series-node URI segment
        decides: ``#Hub830-N`` → MARC 830 (ind1=0 ind2=blank);
        anything else (typically a bnode emitted from a 490 source
        row) → MARC 490 (ind1=0 ind2=blank).

        M3 routes the marc2bibframe2 chain
        ``bf:Instance → bf:relation → bf:Relation →
        bf:associatedResource → bf:Series → bf:title → bf:Title →
        bf:mainTitle`` down to a flat ``bf:hasSeries`` link from
        the Manifestation to a ``bf:Series`` node carrying
        ``rdfs:label``. P-54 Phase 6 added the Hub830 routing.
        ``bf:seriesEnumeration`` (MARC 830 ``$v`` volume number)
        is captured by marc2bibframe2 on the Relation node but not
        yet on the Manifestation-side Series; ``$v`` emit is
        deferred.
        """
        for series in self.graph.objects(self.manifestation, V.BF.hasSeries):
            label = self._first_label(series)
            if not label:
                continue
            # P-54 Phase 6 — 490 vs 830 routing by source-Hub URI.
            # marc2bibframe2 mints ``#Hub830-N`` for source-830
            # rows; 490 rows lack the Hub URI and arrive as
            # bnodes. Default to 490 for the bnode case.
            if isinstance(series, URIRef) and "#Hub830-" in str(series):
                tag, ind1 = "830", "0"
            else:
                tag, ind1 = "490", "0"
            self._emit_datafield(
                record,
                tag,
                ("a", label),
                ind1=ind1,
                lineage=self._lineage_token(series),
            )

    _LANGUAGES_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"
    _DESCRIPTION_CONVENTIONS_URI_PREFIX: Final[str] = (
        "http://id.loc.gov/vocabulary/descriptionConventions/"
    )

    def _emit_helmet_source_marker(self, record: Element) -> None:
        # MARC 040 cataloging source. The source 040 $a / $b / $e land
        # in BIBFRAME's bf:adminMetadata blocks (cataloging agency
        # code, description language URI, description conventions URI
        # respectively). M3's Manifestation pass aggregates them onto
        # a single ``bffi:adminMetadata → bffi:AdminMetadata`` block;
        # we read those four predicates here. ``$d`` always carries
        # the ``FI-HELME/bffi-roundtrip`` marker so cataloguers see at
        # a glance that this row was reconstructed by the round-trip.
        subs: list[tuple[str, str]] = []
        for admin in self.graph.objects(self.manifestation, V.BFFI.adminMetadata):
            for agent in self.graph.objects(admin, V.BF.agent):
                for code in self.graph.objects(agent, V.BFFI.code):
                    if isinstance(code, Literal):
                        subs.append(("a", str(code)))
                        break
                if any(c == "a" for c, _ in subs):
                    break
            for lang_uri in self.graph.objects(admin, V.BFFI.descriptionLanguage):
                if isinstance(lang_uri, URIRef) and str(lang_uri).startswith(
                    self._LANGUAGES_URI_PREFIX
                ):
                    subs.append(("b", str(lang_uri)[len(self._LANGUAGES_URI_PREFIX) :]))
                    break
            for conv_uri in self.graph.objects(admin, V.BFFI.descriptionConventions):
                if isinstance(conv_uri, URIRef) and str(conv_uri).startswith(
                    self._DESCRIPTION_CONVENTIONS_URI_PREFIX
                ):
                    subs.append(
                        (
                            "e",
                            str(conv_uri)[len(self._DESCRIPTION_CONVENTIONS_URI_PREFIX) :],
                        )
                    )
                    break
            break
        # Fall back: synth ``$a FI-HELME`` when the source carried no
        # 040 (older records). The synth marker in $d is always
        # present so cataloguers can tell what's reconstructed.
        if not any(c == "a" for c, _ in subs):
            subs.append(("a", "FI-HELME"))
        subs.append(("d", "FI-HELME/bffi-roundtrip"))
        self._emit_datafield(
            record,
            "040",
            *subs,
            add_marker=False,  # this row IS the marker (via $d)
        )

    def _emit_languages(self, record: Element) -> None:
        expr = self.expression
        if expr is None:
            return
        codes: list[str] = []
        for lang in self.graph.objects(expr, V.BFFI.language):
            s = str(lang)
            if s.startswith(_LANGUAGE_URI_PREFIX):
                codes.append(s.removeprefix(_LANGUAGE_URI_PREFIX))
        if not codes:
            return
        subs: list[tuple[str, str]] = [("a", codes[0])]
        # MARC 041 $h carries original-language codes for translations;
        # BFFI doesn't separately track "original language" for an
        # Expression (a follow-up plan could surface it from MARC 041$h
        # on the source side). For now we emit only $a.
        self._emit_datafield(record, "041", *subs)

    def _emit_primary_contribution(self, record: Element) -> None:
        work = self.work
        if work is None:
            return
        for contrib in self.graph.objects(work, V.BFFI.contribution):
            types = set(self.graph.objects(contrib, RDF.type))
            if V.BFFI.PrimaryContribution not in types:
                continue
            for agent in self.graph.objects(contrib, V.BFFI.agent):
                name_subs = self._name_subfields_from_marc_key(agent)
                used_marckey = name_subs is not None
                if name_subs is None:
                    label = self._first_label(agent)
                    if not label:
                        continue
                    name_subs = [("a", label)]
                role_subs = self._collect_role_subs(contrib)
                id_subs = self._collect_agent_id_subs(agent)
                # 100 ind1=1 ("surname"-form name) is the dominant
                # cataloguer choice for Helmet personal names. ind2 is
                # undefined in current MARC ⇒ blank.
                self._emit_datafield(
                    record,
                    "100",
                    *name_subs,
                    *role_subs,
                    *id_subs,
                    ind1="1",
                    marckey_bypass=used_marckey,
                )
                return  # only one primary

    def _collect_agent_id_subs(self, agent: Node) -> list[tuple[str, str]]:
        """Walk an agent's ``bf:identifiedBy`` chain and return ``$0``
        subfield pairs in source-MARC format (``(CODE)VALUE``).

        Shape (from marc2bibframe2):

            <agent> bf:identifiedBy [
                a bf:Identifier ;
                rdf:value "000039084 " ;     # may have trailing space
                bf:source [
                    a bf:Source ;
                    bf:code "FI-ASTERI-N" ;  # the (FI-ASTERI-N) prefix
                ]
            ] .

        Output: ``("0", "(FI-ASTERI-N)000039084 ")`` — matches the
        source MARC ``$0`` verbatim (round-trip preserves trailing
        whitespace too). When the identifier has no ``bf:source``
        code, the ``(CODE)`` prefix is omitted.
        """
        subs: list[tuple[str, str]] = []
        for ident in self.graph.objects(agent, V.BFFI.identifiedBy):
            value: str | None = None
            for v in self.graph.objects(ident, RDF.value):
                if isinstance(v, Literal):
                    value = str(v)
                    break
            if value is None:
                continue
            code: str | None = None
            for source in self.graph.objects(ident, V.BF.source):
                for c in self.graph.objects(source, V.BFFI.code):
                    if isinstance(c, Literal):
                        code = str(c)
                        break
                if code is not None:
                    break
            formatted = f"({code}){value}" if code else value
            subs.append(("0", formatted))
        return subs

    #: Prefix that identifies LoC MARC relator URIs whose last path
    #: segment is the MARC ``$4`` code. The round-trip emits ``$4``
    #: only when the role URI matches this prefix — never for MTS or
    #: any other URI namespace, since their identifiers aren't MARC
    #: relator codes.
    _LOC_RELATOR_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/relators/"

    def _collect_role_subs(self, contrib: Node) -> list[tuple[str, str]]:
        """Walk all ``bffi:role`` triples on ``contrib`` and return the
        ordered ``$4`` / ``$e`` subfield pairs.

        ``$4`` is emitted only for LoC-relator URIs — the rare
        Helmet records (~10 in 473 k per the 2026-06-07 corpus
        inventory) where the cataloguer wrote a relator code in
        source MARC and marc2bibframe2 lifted it to
        ``bf:role <…/relators/CODE>``. M3 propagates that URI
        unchanged onto ``bffi:role`` on the canonical Contribution.

        The previous behaviour — emitting ``$4`` for every
        contribution thanks to the M3 LoC-enrichment pass — was
        removed in the BFFI role-redesign because:

        1. BFFI 1.0.0 designates MTS, not LoC, as the value
           vocabulary for ``bffi:Role`` (see
           ``bffi-meta:relatedValueVocabulary`` on ``docs/lkd.rdf``'s
           ``bffi:Role`` class). After enrichment moved from
           LoC-at-M3 to MTS-at-Skosify, most ``bffi:role`` URIs
           in the graph are MTS concepts (``mts:m552`` etc.) whose
           last path segment is not a MARC relator code.
        2. The enrichment-derived ``$4`` was data the cataloguer
           never wrote — emitting it on the round-trip mutated the
           record shape rather than restoring it.

        See ``docs/bffi_limitations.md`` L-07 for the documented
        behaviour change.

        Role-value shapes that coexist on the same contribution:

          - URIRef role with the LoC-relator prefix — emits ``$4``
            from the last path segment AND a fallback ``$e`` from
            the URI's prefLabel.
          - URIRef role with any other namespace (MTS, etc.) —
            provides a fallback ``$e`` from the URI's prefLabel,
            no ``$4``.
          - BNode role with ``rdfs:label`` — the cataloguer's
            original ``$e`` term; wins over the URI fallback when
            both are present so the round-trip restores exactly
            what was catalogued.
        """
        code: str | None = None
        uri_label: str | None = None
        bnode_label: str | None = None
        for role in self.graph.objects(contrib, V.BFFI.role):
            if isinstance(role, URIRef):
                role_str = str(role)
                if code is None and role_str.startswith(self._LOC_RELATOR_URI_PREFIX):
                    code = role_str[len(self._LOC_RELATOR_URI_PREFIX) :]
                if uri_label is None:
                    uri_label = self._loc_label(role, lang_pref=("fi", "sv", "en"))
            elif bnode_label is None:
                bnode_label = self._first_label(role)
        subs: list[tuple[str, str]] = []
        if code is not None:
            subs.append(("4", code))
        chosen_label = bnode_label or uri_label
        if chosen_label:
            subs.append(("e", chosen_label))
        return subs

    def _first_label(self, node: Node) -> str | None:
        for prop in (V.RDFS.label, SKOS.prefLabel):
            for val in self.graph.objects(node, prop):
                if isinstance(val, Literal):
                    return str(val)
        return None

    #: Display-language priority for authority-URI prefLabel lookups
    #: in the round-trip. Matches CLAUDE.md's
    #: "Display language priority for skos:prefLabel: fi, sv, en".
    _AUTHORITY_LABEL_LANG_PREF: tuple[str, ...] = ("fi", "sv", "en")

    def _authority_label(self, node: Node) -> str | None:
        """Language-aware label lookup for authority URIs (YSO, KANTO,
        SLM, etc.). Walks both ``rdfs:label`` and ``skos:prefLabel``,
        prefers Finnish, then Swedish, then English, then any label.
        Returns ``None`` when nothing's found (e.g. the Finto vocab
        dump isn't loaded for this URI's namespace).
        """
        labels_by_lang: dict[str | None, str] = {}
        for prop in (SKOS.prefLabel, V.RDFS.label):
            for val in self.graph.objects(node, prop):
                if isinstance(val, Literal):
                    labels_by_lang.setdefault(val.language, str(val))
        for pref in self._AUTHORITY_LABEL_LANG_PREF:
            if pref in labels_by_lang:
                return labels_by_lang[pref]
        if labels_by_lang:
            return next(iter(labels_by_lang.values()))
        return None

    def _emit_uniform_title(self, record: Element) -> None:  # noqa: PLR0912 — three-tier shape (BFFI-native mainTitle+language → marcKey-parse → structured-only) on top of the existing Hub-walk; splitting fragments shared state.
        """MARC 240 — uniform title for the work, OR MARC 130 — main
        entry uniform title (no-author works: anthologies, scriptures,
        classics). The Hub URI's tag-segment (``#Hub240-N`` vs
        ``#Hub130-N``) decides which MARC tag the row routes to.

        Source data lives on a per-record ``bf:Hub`` (``#Hub240-N``
        or ``#Hub130-N``) attached to the Expression via
        ``bffi:title``. P-54 Phase 5 added the 130 routing. Two
        title shapes per Hub:

        - **Structured** (P-49 Layer 1): ``bf:title → bf:Title`` with
          ``bf:partNumber`` / ``bf:partName`` for 240 ``$n`` / ``$p``.
          Round-trip uses these directly when present.

        - **bflc:marcKey** (legacy / fallback): the combined 1XX + 240
          source subfield string. Used for $a (title proper),
          $g, and $l (language) — none of which has a dedicated
          structured BFFI predicate yet (P-49 Layer 3 gap). When this
          path runs the row is flagged ``marckey_bypass``.

        ind1=1 (title traced), ind2=0 (no nonfiling characters).
        """
        expr = self.expression
        if expr is None:
            return
        # Walk ``bffi:title`` on the Expression, filtering for Hub-typed
        # targets — the BIBFRAME / BFFI shape for uniform-title hubs
        # (regular main titles target ``bffi:Title``; variant titles
        # ``bf:VariantTitle``; uniform-title hubs ``bf:Hub``).
        for hub in self.graph.objects(expr, V.BFFI.title):
            if (hub, V.RDF.type, V.BF.Hub) not in self.graph:
                continue
            # P-54 Phase 5 — route 130 vs 240 by Hub URI segment.
            # marc2bibframe2 mints ``#Hub130-N`` vs ``#Hub240-N``
            # URIs; the path-segment tag tells which source field
            # the cataloguer wrote. 130 (main-entry uniform title)
            # uses ind1=0 ind2=blank by convention; 240 (uniform
            # title traced) uses ind1=1 ind2=0. When the URI is not
            # a recognised Hub-tag we default to 240 (the more
            # common case, ~152k vs ~19k records).
            if isinstance(hub, URIRef) and "#Hub130-" in str(hub):
                tag, ind1, ind2 = "130", "0", _INDICATOR_BLANK
            else:
                tag, ind1, ind2 = "240", "1", "0"
            structured = self._hub_title_part_subs(hub)
            # P-168 — BFFI-native first when the Hub has no
            # structured ``$n``/``$p``: mainTitle IS the clean ``$a``
            # value (modulo the trailing language-label suffix).
            # When structured ``$n``/``$p`` ARE present, mainTitle is
            # the concatenated form (``"$a $n, $p"``) and we can't
            # recover clean ``$a`` by subtraction — fall through to
            # marcKey for that case.
            if not structured:
                language_label = self._hub_language_label(hub)
                main_title_a = self._hub_main_title_a(hub, language_label)
                bffi_subs: list[tuple[str, str]] = []
                if main_title_a:
                    bffi_subs.append(("a", main_title_a))
                if language_label:
                    bffi_subs.append(("l", language_label))
                if bffi_subs:
                    self._emit_datafield(
                        record,
                        tag,
                        *bffi_subs,
                        ind1=ind1,
                        ind2=ind2,
                        lineage=self._lineage_token(hub),
                    )
                    return
            # Fallback — structured ``$n``/``$p`` present (mainTitle is
            # concatenated, ``$a`` not derivable by subtraction) or no
            # BFFI predicates available at all. Parse marcKey to
            # recover ``$a`` (from ``$t``) and ``$l``. Flagged as
            # marckey_bypass so the audit shows the dependency on the
            # legacy path.
            mk_lit = self._first_marc_key(hub)
            if mk_lit is None:
                continue
            structured_codes = {code for code, _ in structured}
            parsed = _parse_marc_key_subfields(str(mk_lit))
            base: list[tuple[str, str]] = []
            used_marckey = False
            for code, value in parsed:
                if code == "t":
                    base.append(("a", value))
                    used_marckey = True
                elif code == "l":
                    base.append(("l", value))
                    used_marckey = True
                elif code in ("n", "p") and code not in structured_codes:
                    base.append((code, value))
                    used_marckey = True
            subs = _merge_structured_parts(base, structured)
            if subs:
                self._emit_datafield(
                    record,
                    tag,
                    *subs,
                    ind1=ind1,
                    ind2=ind2,
                    lineage=self._lineage_token(hub),
                    marckey_bypass=used_marckey,
                )
                return

    def _hub_title_part_subs(self, hub: Node) -> list[tuple[str, str]]:
        """Read the Hub's structured ``bf:Title`` for ``bf:partNumber``
        / ``bf:partName`` and return them as ``[(n, ...), (p, ...)]``.

        Returns ``[]`` when neither structured predicate is present —
        callers fall back to ``bflc:marcKey``. Used by both 240
        (uniform title) and 730/740 (related uniform titles) since
        both attach a ``bf:Title`` to a ``bf:Hub`` / ``bf:Work``.
        """
        out: list[tuple[str, str]] = []
        for title in self.graph.objects(hub, V.BFFI.title):
            for pn in self.graph.objects(title, V.BFFI.partNumber):
                if isinstance(pn, Literal):
                    out.append(("n", str(pn)))
                    break
            for pname in self.graph.objects(title, V.BFFI.partName):
                if isinstance(pname, Literal):
                    out.append(("p", str(pname)))
                    break
            if out:
                return out
        return out

    def _hub_language_label(self, hub: Node) -> str | None:
        """Read ``hub → bf:language → rdfs:label`` and return the
        human-readable language label that MARC 240 ``$l`` / 730
        ``$l`` carry (``"suomi"``, ``"englanti"``, etc.).

        marc2bibframe2 mints ``bf:language → bf:Language`` blocks on
        Hub240 / Hub730 entities to capture MARC ``$l`` natively in
        BIBFRAME, so this is the BFFI-native path — no ``marcKey``
        parsing needed.
        """
        for lang in self.graph.objects(hub, V.BF.language):
            for lbl in self.graph.objects(lang, V.RDFS.label):
                if isinstance(lbl, Literal):
                    return str(lbl)
        return None

    def _hub_main_title_a(self, hub: Node, language_label: str | None) -> str | None:
        """Extract a clean ``$a`` value from the Hub's structured
        ``bf:title → bf:Title → bf:mainTitle``.

        marc2bibframe2 concatenates source ``$a`` + ``$l`` into the
        mainTitle (e.g. source ``240 $aForward the Foundation,
        $lsuomi`` becomes ``"Forward the Foundation, suomi"``). When
        a language label is provided, strip the trailing ``", <lang>"``
        suffix so the returned ``$a`` matches the source verbatim.
        """
        for title in self.graph.objects(hub, V.BFFI.title):
            for mt in self.graph.objects(title, V.BFFI.mainTitle):
                if not isinstance(mt, Literal):
                    continue
                text = str(mt)
                if language_label and text.endswith(language_label):
                    # Strip the appended " " + language_label.
                    stripped = text[: -len(language_label)].rstrip()
                    if stripped.endswith(","):
                        return stripped
                return text
        return None

    def _first_marc_key(self, node: Node) -> Literal | None:
        for mk in self.graph.objects(node, V.BFLC.marcKey):
            if isinstance(mk, Literal):
                return mk
        return None

    def _emit_variant_title(self, record: Element) -> None:
        """MARC 246 — varying form of title. Routed via the BIBFRAME
        pattern: the Expression has ``bffi:title`` triples pointing
        at typed Title nodes; we filter for ``bf:VariantTitle``
        (regular main titles target ``bffi:Title`` / ``bf:Title``).
        Single ``$a`` per row. ind1=3 (no note, added entry) is
        Helmet's dominant choice; ind2 blank (type of title
        unspecified)."""
        expr = self.expression
        if expr is None:
            return
        for variant in self.graph.objects(expr, V.BFFI.title):
            if (variant, V.RDF.type, V.BF.VariantTitle) not in self.graph:
                continue
            for title in self.graph.objects(variant, V.BFFI.mainTitle):
                if isinstance(title, Literal):
                    self._emit_datafield(
                        record,
                        "246",
                        ("a", str(title)),
                        ind1="3",
                        lineage=self._lineage_token(variant),
                    )
                    break

    def _emit_title(self, record: Element) -> None:
        # 245 $a / $b — prefer the Manifestation's structured
        # ``bffi:title → bffi:Title → bffi:mainTitle / bffi:subtitle``
        # routed from bf:Instance's transcribed bf:Title. marc2bibframe2
        # emits the split form (separate mainTitle + subtitle = 245 $a
        # + $b) on bf:Instance and a concatenated form (one mainTitle
        # = "$a : $b") on bf:Work. Reading from the Manifestation
        # preserves the source subfield boundary; falling back to the
        # Work's prefLabel yields the concatenated form (no $b).
        main_title, subtitle, title_node = self._manifestation_title_parts()
        if main_title is None:
            work = self.work
            main_title = self._first_label(work) if work is not None else None
        if not main_title:
            return
        # 245 ind1=1 ("title added entry") ind2=0 (no non-filing chars
        # to skip). Both are best-effort defaults.
        subs: list[tuple[str, str]] = [("a", main_title)]
        if subtitle:
            subs.append(("b", subtitle))
        # 245 $c — statement of responsibility (lifted by M3 onto the
        # Manifestation as ``bffi:responsibilityStatement``, P-47).
        for stmt in self.graph.objects(self.manifestation, V.BFFI.responsibilityStatement):
            if isinstance(stmt, Literal):
                subs.append(("c", str(stmt)))
                break
        # Lineage from the Title node M2-post tagged (Phase B
        # flat-literal-title matcher). M9 doesn't rebind Title nodes
        # so the token on the source bnode rides through verbatim.
        self._emit_datafield(
            record,
            "245",
            *subs,
            ind1="1",
            ind2="0",
            lineage=self._lineage_token(title_node) if title_node is not None else None,
        )

    def _manifestation_title_parts(self) -> tuple[str | None, str | None, Node | None]:
        """Read the Manifestation's structured title — returns
        ``(main_title, subtitle, title_node)`` with any of the first
        two possibly ``None``. ``title_node`` is the source ``bffi:Title``
        bnode the parts came off (caller uses it for lineage lookup);
        ``None`` when no Title node was found.

        The M3 manifestation CONSTRUCT routes bf:Instance's bf:Title
        bnode to a sha1-minted ``bffi:Title`` node under
        ``bffi:title``, copying its ``bf:mainTitle``/``bf:subtitle``
        as ``bffi:mainTitle``/``bffi:subtitle``. We pick the first of
        each (Helmet records have one transcribed title per 245).
        """
        main_title: str | None = None
        subtitle: str | None = None
        picked_node: Node | None = None
        for title_node in self.graph.objects(self.manifestation, V.BFFI.title):
            for mt in self.graph.objects(title_node, V.BFFI.mainTitle):
                if isinstance(mt, Literal):
                    main_title = str(mt)
                    break
            for st in self.graph.objects(title_node, V.BFFI.subtitle):
                if isinstance(st, Literal):
                    subtitle = str(st)
                    break
            if main_title is not None:
                picked_node = title_node
                break
        return main_title, subtitle, picked_node

    def _emit_publication_statement(self, record: Element) -> None:
        """MARC 264 publication / production / distribution / manufacture.

        Prefers structured ``bffi:provisionActivity`` bnodes — each
        emits one 264 row with:
          - ind2 from the bnode's rdf:type
            (bf:Publication → 1, bf:Production → 0,
             bf:Distribution → 2, bf:Manufacture → 3,
             bf:Copyright → 4; default = 1).
          - $a from bflc:simplePlace,
            $b from bflc:simpleAgent,
            $c from bflc:simpleDate.

        Falls back to the lifted ``bffi:publicationStatement`` literal
        (264 ind2=1, single $c) when no structured data is present.
        Last-resort: parse the prefLabel suffix for older M3 output.
        """
        # Structured path: one 264 row per provisionActivity bnode.
        # ind2 from the ProvisionActivity's bf:Publication /
        # bf:Distribution / bf:Manufacture / bf:Copyright typing.
        # Source MARC 260 records round-trip as 264 ind2=1 because
        # marc2bibframe2 normalises both 260 and 264 to a single
        # ``bf:ProvisionActivity`` shape with no source-MARC-version
        # marker — the BFFI graph alone can't distinguish them, and
        # we deliberately don't consult ``bffi-prov:fromMarcField``
        # here (the round-trip's job is to verify that bffi:
        # predicates alone can reconstruct source MARC as closely as
        # possible; pipeline-internal provenance is out of scope).
        emitted_structured = False
        for prov in self.graph.objects(self.manifestation, V.BFFI.provisionActivity):
            if isinstance(prov, Literal):
                continue
            subs = self._provision_activity_subs(prov)
            if not subs:
                continue
            ind2 = self._provision_activity_ind2(prov)
            self._emit_datafield(record, "264", *subs, ind2=ind2, lineage=self._lineage_token(prov))
            emitted_structured = True
        if emitted_structured:
            return
        for stmt in self.graph.objects(self.manifestation, V.BFFI.publicationStatement):
            if isinstance(stmt, Literal):
                self._emit_datafield(record, "264", ("c", str(stmt)), ind2="1")
                return
        for lit in self.graph.objects(self.manifestation, SKOS.prefLabel):
            text = str(lit)
            if "(" in text and text.endswith(")"):
                pub = text[text.rindex("(") + 1 : -1].strip()
                if pub:
                    self._emit_datafield(record, "264", ("c", pub), ind2="1")
                    return

    def _provision_activity_ind2(self, prov: Node) -> str:
        """Derive MARC 264 ind2 from the ProvisionActivity rdf:type."""
        for t in self.graph.objects(prov, RDF.type):
            if isinstance(t, URIRef) and str(t).startswith(str(V.BF)):
                kind = str(t)[len(str(V.BF)) :]
                if kind in _PROVISION_TYPE_TO_IND2:
                    return _PROVISION_TYPE_TO_IND2[kind]
        return "1"  # Publication is the dominant default

    def _provision_activity_subs(self, prov: Node) -> list[tuple[str, str]]:
        """Walk a bf:ProvisionActivity bnode for the 264 subfields.

        ``bflc:simplePlace`` / ``simpleAgent`` / ``simpleDate`` literals
        carry the source MARC ``$a`` / ``$b`` / ``$c``. Helmet often
        emits multiple translit forms (Cyrillic + Latin) — we take the
        first of each kind, which marc2bibframe2 lists in
        source-MARC encounter order (the Latin form first for
        b26164413-style records). Source 264 ind2's punctuation
        (``Moskva :``, ``AST,``, ``2025.``) is also preserved when
        present.
        """
        subs: list[tuple[str, str]] = []
        place = next(
            (
                str(v)
                for v in self.graph.objects(prov, V.BFLC.simplePlace)
                if isinstance(v, Literal)
            ),
            None,
        )
        if place:
            subs.append(("a", place))
        agent = next(
            (
                str(v)
                for v in self.graph.objects(prov, V.BFLC.simpleAgent)
                if isinstance(v, Literal)
            ),
            None,
        )
        if agent:
            subs.append(("b", agent))
        date = next(
            (str(v) for v in self.graph.objects(prov, V.BFLC.simpleDate) if isinstance(v, Literal)),
            None,
        )
        if date:
            subs.append(("c", date))
        return subs

    def _emit_extent_and_dimensions(self, record: Element) -> None:
        # MARC 300 — physical description. Four subfields routed:
        #   $a extent              → ``bffi:extent`` value or nested
        #                            ``bf:Extent rdfs:label``
        #   $b other physical      → nested
        #     details                ``bf:Extent → bf:note → bf:Note
        #                            (a mnotetype/physical) rdfs:label``
        #   $c dimensions          → ``bffi:dimensions`` value
        #   $e accompanying        → Instance-side
        #     material               ``bffi:note → bf:Note
        #                            (a mnotetype/accmat) rdfs:label``
        # Skip entirely when all four are absent.
        subs: list[tuple[str, str]] = []
        extent_subs, extent_node = self._extent_subs()
        subs.extend(extent_subs)
        subs.extend(self._dimensions_subs())
        subs.extend(self._accmat_subs())
        if subs:
            # 300's source-MARC-field token rides on the ``bf:Extent``
            # bnode (Phase B's flat-literal-extent matcher anchors
            # there). Other 300 subfields ($c dimensions, $e accmat)
            # come from the same source row by convention.
            self._emit_datafield(
                record,
                "300",
                *subs,
                lineage=self._lineage_token(extent_node) if extent_node is not None else None,
            )

    def _extent_subs(self) -> tuple[list[tuple[str, str]], Node | None]:
        """``$a`` + nested ``$b`` from the Manifestation's first
        ``bffi:extent``. Literal extents emit only ``$a``; bf:Extent
        bnodes walk into ``bf:note`` for ``$b`` other-physical.

        Returns ``(subfields, extent_node)`` — ``extent_node`` is the
        ``bf:Extent`` bnode the subfields came off so the caller can
        look up M2-post's lineage token on it. ``None`` when the
        extent was a flat literal (no entity to tag)."""
        out: list[tuple[str, str]] = []
        for ext in self.graph.objects(self.manifestation, V.BFFI.extent):
            if isinstance(ext, Literal):
                out.append(("a", str(ext)))
                return out, None
            lbl = self._first_label(ext)
            if lbl:
                out.append(("a", lbl))
            for note in self.graph.objects(ext, V.BF.note):
                if self._has_marc_note_type(note, "physical"):
                    nlbl = self._first_label(note)
                    if nlbl:
                        out.append(("b", nlbl))
                        return out, ext
            return out, ext
        return out, None

    def _dimensions_subs(self) -> list[tuple[str, str]]:
        """``$c`` from the first ``bffi:dimensions``."""
        for dim in self.graph.objects(self.manifestation, V.BFFI.dimensions):
            if isinstance(dim, Literal):
                return [("c", str(dim))]
            lbl = self._first_label(dim)
            if lbl:
                return [("c", lbl)]
            return []
        return []

    def _accmat_subs(self) -> list[tuple[str, str]]:
        """``$e`` from the first Instance-side ``bffi:note`` typed
        ``rdf:type <mnotetype/accmat>``."""
        for note in self.graph.objects(self.manifestation, V.BFFI.note):
            if self._has_marc_note_type(note, "accmat"):
                nlbl = self._first_label(note)
                if nlbl:
                    return [("e", nlbl)]
        return []

    _MARC_NOTE_TYPE_NS: Final[str] = "http://id.loc.gov/vocabulary/mnotetype/"

    def _has_marc_note_type(self, node: Node, suffix: str) -> bool:
        """True when ``node`` carries
        ``rdf:type <mnotetype/<suffix>>``. The mnotetype vocab
        carries marc2bibframe2's categorical note classification —
        ``physical`` = 300 $b, ``accmat`` = 300 $e, etc."""
        target = URIRef(self._MARC_NOTE_TYPE_NS + suffix)
        return target in set(self.graph.objects(node, RDF.type))

    def _marc_5xx_tag_for_note(self, note: Node) -> str:
        """Pick the MARC 5XX tag a ``bf:Note`` should round-trip into,
        based on its ``rdf:type <mnotetype/<tail>>`` discriminator.
        Defaults to MARC 500 (general note) when no categorical type
        is present (the source 500 ``$a`` case).
        """
        for t in self.graph.objects(note, RDF.type):
            if not isinstance(t, URIRef):
                continue
            s = str(t)
            if s.startswith(self._MARC_NOTE_TYPE_NS):
                tail = s[len(self._MARC_NOTE_TYPE_NS) :]
                tag = _MNOTETYPE_TO_MARC_5XX.get(tail)
                if tag is not None:
                    return tag
        return "500"

    def _emit_content_type(self, record: Element) -> None:
        # 336 content type — lifted from bf:Work via bffi:content (URI
        # in the LoC contentTypes vocab). Emit $a label from the URI's
        # cross-graph prefLabel, $b code from the URI tail, $2 source.
        expr = self.expression
        if expr is None:
            return
        emitted = False
        for ctype in self.graph.objects(expr, V.BFFI.content):
            if not isinstance(ctype, URIRef):
                continue
            code = self._loc_code(ctype, "contentTypes")
            label = self._loc_label(ctype, lang_pref=("fi", "en"))
            self._emit_datafield(
                record,
                "336",
                ("a", label or ""),
                ("b", code or ""),
                ("2", "rdacontent"),
            )
            emitted = True
        if not emitted:
            self.skipped.append("336")

    def _emit_notes(self, record: Element) -> None:
        # 500 general note. M3 emits ``bffi:note`` on both Expression
        # (from bf:Work) and Manifestation (from bf:Instance — physical
        # carrier / accompanying material notes). Each distinct note
        # becomes one 500 row; we walk both sides and dedupe by the
        # extracted text so a note attached to both nodes doesn't
        # double-emit.
        seen: set[str] = set()
        for source in (self.expression, self.manifestation):
            if source is None:
                continue
            for note in self.graph.objects(source, V.BFFI.note):
                # Skip notes whose ``rdf:type`` routes them to a more
                # specific MARC field (``mnotetype/physical`` → 300 $b,
                # ``mnotetype/accmat`` → 300 $e, both emitted by
                # :meth:`_emit_extent_and_dimensions`). Without this
                # skip, "kuvitettu" / "1 CD-äänilevy" would
                # double-emit as 500 rows.
                if not isinstance(note, Literal) and (
                    self._has_marc_note_type(note, "physical")
                    or self._has_marc_note_type(note, "accmat")
                ):
                    continue
                text: str | None = None
                if isinstance(note, Literal):
                    text = str(note)
                else:
                    for val in self.graph.objects(note, V.RDF.value):
                        if isinstance(val, Literal):
                            text = str(val)
                            break
                    if text is None:
                        for val in self.graph.objects(note, V.RDFS.label):
                            if isinstance(val, Literal):
                                text = str(val)
                                break
                if text and text not in seen:
                    seen.add(text)
                    # Lineage from the bf:Note bnode (Phase B
                    # flat-literal-note matcher). Flat-literal note
                    # text has no anchor node → no lineage available.
                    note_lineage = (
                        self._lineage_token(note) if not isinstance(note, Literal) else None
                    )
                    # P-168 — route by mnotetype discriminator. bf:Note
                    # nodes carrying ``rdf:type <mnotetype/biblio>`` /
                    # ``<mnotetype/participants>`` / ``<mnotetype/language>``
                    # etc. land on MARC 504 / 511 / 546 instead of the
                    # default 500. Literal-form notes (no node, no
                    # type) keep the 500 default.
                    tag = "500" if isinstance(note, Literal) else self._marc_5xx_tag_for_note(note)
                    self._emit_datafield(record, tag, ("a", text), lineage=note_lineage)

    def _emit_table_of_contents(self, record: Element) -> None:
        # MARC 505 formatted contents note. M3 hoists
        # ``bf:tableOfContents`` from the source bf:Work onto the
        # canonical bffi:Manifestation, modelled as a
        # bffi:TableOfContents blank node carrying ``rdfs:label`` with
        # the full track listing / chapter list. We emit one 505 row
        # per distinct label, single-$a blob (no per-item splitting).
        # ind1 = 0 ("contents") is the dominant cataloguer choice in
        # Helmet for both complete book TOCs and CD track listings;
        # ind2 = " " (no enhanced/structured form). The marker
        # ``$5 FI-HELME/bffi-roundtrip`` is added by _emit_datafield.
        manif = self.manifestation
        if manif is None:
            return
        for toc in self.graph.objects(manif, V.BFFI.tableOfContents):
            text = self._first_label(toc) if not isinstance(toc, Literal) else str(toc)
            if text:
                # bffi:TableOfContents is a bnode; M2-post's
                # flat-literal-note matcher tags some of these
                # (505 routes through bf:Note in raw BIBFRAME).
                toc_lineage = self._lineage_token(toc) if not isinstance(toc, Literal) else None
                self._emit_datafield(record, "505", ("a", text), ind1="0", lineage=toc_lineage)

    def _emit_media_type(self, record: Element) -> None:
        for media in self.graph.objects(self.manifestation, V.BFFI.media):
            code = self._loc_code(media, "mediaTypes")
            label = self._loc_label(media, lang_pref=("fi", "en"))
            self._emit_datafield(
                record,
                "337",
                ("a", label or ""),
                ("b", code or ""),
                ("2", "rdamedia"),
            )

    def _emit_carrier_type(self, record: Element) -> None:
        for carrier in self.graph.objects(self.manifestation, V.BFFI.carrier):
            code = self._loc_code(carrier, "carriers")
            label = self._loc_label(carrier, lang_pref=("fi", "en"))
            self._emit_datafield(
                record,
                "338",
                ("a", label or ""),
                ("b", code or ""),
                ("2", "rdacarrier"),
            )

    def _emit_digital_characteristic(self, record: Element) -> None:
        for ch in self.graph.objects(self.manifestation, V.BFFI.digitalCharacteristic):
            label = self._loc_label(ch, lang_pref=("en",))
            self._emit_datafield(record, "347", ("b", label or ""))

    def _emit_sound_characteristic(self, record: Element) -> None:
        for ch in self.graph.objects(self.manifestation, V.BFFI.soundCharacteristic):
            label = self._loc_label(ch, lang_pref=("en",))
            # MARC 344 is the sound-characteristic field. Subfield
            # routing depends on which sound facet the URI namespace
            # encodes (mrecmedium → $h, mplayspeed → $e, mplayback →
            # $g, mcapturestorage → $a). Best-effort routing here.
            s = str(ch)
            sub = "a"
            if "/mrecmedium/" in s:
                sub = "h"
            elif "/mplayspeed/" in s:
                sub = "e"
            elif "/mplayback/" in s:
                sub = "g"
            self._emit_datafield(record, "344", (sub, label or ""))

    def _emit_color_content(self, record: Element) -> None:
        for ch in self.graph.objects(self.manifestation, V.BFFI.colorContent):
            label = self._loc_label(ch, lang_pref=("en",))
            self._emit_datafield(record, "346", ("a", label or ""))

    def _loc_code(self, uri: Node, namespace: str) -> str | None:
        s = str(uri)
        marker = f"/{namespace}/"
        if marker in s:
            return s.rsplit(marker, 1)[-1]
        return None

    def _loc_label(self, uri: Node, *, lang_pref: tuple[str, ...]) -> str | None:
        # Look up labels in the requested languages. Walks BOTH
        # ``skos:prefLabel`` (the Finto/LoC SKOS dump shape) and
        # ``rdfs:label`` (the shape marc2bibframe2 attaches directly
        # to LoC URIs in BIBFRAME — propagated to canonical by
        # ``_propagate_loc_vocab_labels``). Returns None if neither
        # carries a label.
        labels: dict[str | None, str] = {}
        for prop in (SKOS.prefLabel, V.RDFS.label):
            for val in self.graph.objects(uri, prop):
                if isinstance(val, Literal):
                    labels.setdefault(val.language, str(val))
        for pref in lang_pref:
            if pref in labels:
                return labels[pref]
        if labels:
            return next(iter(labels.values()))
        return None

    def _raw_work_uris(self, work: URIRef) -> list[URIRef]:
        """Return the M3-raw work URIs the canonical Work was minted
        from. The Statement reifications (P-50 Phase C) carry
        ``rdf:subject`` pointing at one of these raw URIs because
        M2-post runs before M8's canonical-mint reshape. M8 emits
        ``<canonical_work> prov:wasDerivedFrom <raw_work>`` for each
        absorbed raw Work; this method walks that link so consumers
        can find Statements anchored on either the canonical URI or
        any of its raw predecessors.

        Returns ``[canonical_work_uri]`` plus every raw URI reached
        via ``prov:wasDerivedFrom``. The canonical URI is included
        for the case where M2-post happened to emit a Statement with
        the canonical URI directly (rare, but defensive)."""
        result: list[URIRef] = [work]
        for raw in self.graph.objects(work, V.PROV.wasDerivedFrom):
            if isinstance(raw, URIRef):
                result.append(raw)
        return result

    def _emit_subjects(self, record: Element) -> None:  # noqa: PLR0912 — orchestrates Phase C Statement walk + flat-subject fallback + per-anchor / per-tier filters; splitting fragments shared state across helpers.
        work = self.work
        if work is None:
            return
        seen_authorities: set[URIRef] = self._authority_targets(work, V.BFFI.subject)
        raw_origin_hints = self._build_raw_origin_hints(work, V.BFFI.subject)
        # Statement reifications anchor on the M3-raw work URI (M2-post
        # ran before M8's canonical-mint reshape). Walk every raw URI
        # the canonical Work was derived from via prov:wasDerivedFrom.
        statement_anchors = self._raw_work_uris(work)
        # Pre-compute the set of targets that are emitted by
        # :meth:`_emit_genre_forms` so the Statement walk below skips
        # them. M2-post emits Statements with ``rdf:predicate
        # bffi:subject`` for all 6XX tags (including 655 genre-forms)
        # for cross-shape uniformity. Without this skip, a 655
        # Statement's target would land here, route through
        # :meth:`_subject_marc_tag` (which defaults to "650" for
        # ``#GenreForm655-N`` raw URIs and M9-bound kaunokki/SLM
        # authorities), and emit as a 650 — duplicating what
        # ``_emit_genre_forms`` emits at the right tag.
        genre_targets: set[Node] = set(self.graph.objects(work, V.BFFI.genreForm))
        # Include raw-URI back-walks so M9-rebound genres are caught.
        genre_raw_hints = self._build_raw_origin_hints(work, V.BFFI.genreForm)
        for raw_str in genre_raw_hints.values():
            genre_targets.add(URIRef(raw_str))

        # P-50 Phase C — walk via reified ``rdf:Statement`` first. Each
        # statement has ``rdf:subject ?raw-work ; rdf:predicate
        # bffi:subject ; rdf:object ?target`` and carries the
        # per-record provenance token on the statement URI (not on the
        # shared target). One MARC 6XX row per reified statement.
        # ``rdf:subject`` is the M3-raw work URI (M2-post ran before
        # M8's reshape); walk every raw URI the canonical Work was
        # derived from.
        emitted_targets: set[Node] = set()
        for anchor in statement_anchors:
            for stmt in self.graph.subjects(RDF.subject, anchor):
                if (stmt, RDF.type, RDF.Statement) not in self.graph:
                    continue
                if (stmt, RDF.predicate, V.BFFI.subject) not in self.graph:
                    continue
                target = next(self.graph.objects(stmt, RDF.object), None)
                if target is None:
                    continue
                if target in genre_targets:
                    continue
                if target in emitted_targets:
                    continue
                row_result = self._subject_row(target, seen_authorities)
                if row_result is None:
                    continue
                row, used_marckey = row_result
                tag = self._subject_marc_tag(target, raw_origin_hints)
                # Lineage comes off the STATEMENT URI — that's where
                # M2-post stamped the source-MARC-field token. Falling
                # back to the target's own lineage if the statement
                # somehow has none.
                lineage = self._lineage_token(stmt) or self._lineage_for_subject(
                    target, raw_origin_hints
                )
                self._emit_datafield(
                    record,
                    tag,
                    *row,
                    ind2="7",
                    lineage=lineage,
                    marckey_bypass=used_marckey,
                )
                emitted_targets.add(target)

        # Fallback for subjects without a reified statement — records
        # processed before P-50 Phase C shipped, or shapes M2-post's
        # statement minter didn't reach. Walks the flat
        # ``bffi:subject`` predicate as before.
        for subject in self.graph.objects(work, V.BFFI.subject):
            if subject in emitted_targets:
                continue
            row_result = self._subject_row(subject, seen_authorities)
            if row_result is None:
                continue
            row, used_marckey = row_result
            tag = self._subject_marc_tag(subject, raw_origin_hints)
            lineage = self._lineage_for_subject(subject, raw_origin_hints)
            self._emit_datafield(
                record,
                tag,
                *row,
                ind2="7",
                lineage=lineage,
                marckey_bypass=used_marckey,
            )

    def _lineage_for_subject(
        self,
        target: Node,
        raw_origin_hints: dict[URIRef, str],
    ) -> str | None:
        """Lineage token for a subject row. Direct fragment if the
        subject IS a raw bib URI; otherwise the back-walk via
        ``skos:exactMatch`` (built once per emit_subjects pass into
        ``raw_origin_hints``) recovers the originating raw URI's
        fragment."""
        direct = self._lineage_token(target)
        if direct is not None:
            return direct
        if isinstance(target, URIRef):
            raw = raw_origin_hints.get(target)
            if raw:
                return self._lineage_token(URIRef(raw))
        return None

    #: Routes a BFFI subject node to the right MARC 6XX tag by looking
    #: at clues in the URI / labels / source. Coverage:
    #:   - 600 personal name subject (KANTO finaf personal-name URIs;
    #:     marc2bibframe2 mints ``#Agent600-N`` fragments)
    #:   - 610 corporate-name subject (``#Agent610-N``)
    #:   - 611 meeting-name subject  (``#Agent611-N``)
    #:   - 648 chronological subject (yso-aika namespace; ``#Topic648-N``)
    #:   - 651 geographic subject     (yso-paikat; ``#Place651-N`` fragments)
    #:   - 650 topical                (everything else)
    _GEOGRAPHIC_HINTS: tuple[str, ...] = (
        "yso-paikat",
        "/yso-paikat/",
        "#Place651",
        "#Place-",
    )
    _CHRONOLOGICAL_HINTS: tuple[str, ...] = (
        "yso-aika",
        "/yso-aika/",
        "#Topic648",
    )
    _PERSONAL_HINTS: tuple[str, ...] = ("#Agent600", "/finaf/")
    _CORPORATE_HINTS: tuple[str, ...] = ("#Agent610",)
    _MEETING_HINTS: tuple[str, ...] = ("#Agent611",)

    def _subject_marc_tag(
        self,
        target: Node,
        raw_origin_hints: dict[URIRef, str] | None = None,
    ) -> str:
        """Route a BFFI subject node to 600 / 610 / 611 / 648 / 651 /
        650 based on URI hints. The hints are the M3-minted fragment
        IDs (``#Agent600-N``, ``#Place651-N``, etc.) for raw URIs, and
        Finto vocab namespaces (yso-paikat, yso-aika, finaf) for
        M9-bound authority URIs. Default = 650 (topical).

        ``raw_origin_hints`` (built once per work by
        :meth:`_build_raw_origin_hints`) maps each authority URI to the
        raw bib-URI it came from. The raw URI's fragment ID
        (e.g. ``#Place651-21``) is the only routing signal that
        survives M9 — the plain ``yso/p105037`` (Greece) URI alone
        doesn't say it's geographic. The fallback chain checks the
        direct URI first, then the back-walk via the raw URI's
        fragment.
        """
        if not isinstance(target, URIRef):
            return "650"
        # bf:Place / bf:Temporal / bf:Person / bf:Organization /
        # bf:Meeting / bf:Topic rdf:type on the subject URI is the
        # most authoritative signal — comes from BIBFRAME's
        # ``<bf:Place rdf:about="…"/>`` typing on the cataloguer-typed
        # ``$0`` URI. Propagated to canonical by
        # :func:`_propagate_subject_typing`. Checked first because
        # the URI namespace / fragment heuristics fail for the plain
        # ``yso/`` URI case (a yso/p104990 place URI has no
        # yso-paikat or #Place651 hint).
        type_tag = self._marc_tag_from_rdf_type(target)
        if type_tag is not None:
            return type_tag
        s = str(target)
        routes: tuple[tuple[tuple[str, ...], str], ...] = (
            (self._PERSONAL_HINTS, "600"),
            (self._CORPORATE_HINTS, "610"),
            (self._MEETING_HINTS, "611"),
            (self._CHRONOLOGICAL_HINTS, "648"),
            (self._GEOGRAPHIC_HINTS, "651"),
        )
        for hints, tag in routes:
            if any(h in s for h in hints):
                return tag
        if raw_origin_hints is not None:
            raw = raw_origin_hints.get(target)
            if raw:
                for hints, tag in routes:
                    if any(h in raw for h in hints):
                        return tag
        return "650"

    def _marc_tag_from_rdf_type(self, target: URIRef) -> str | None:
        """Read ``rdf:type`` triples on the subject URI and map to a
        MARC 6XX tag. Returns ``None`` when none of the routable
        types are present."""
        for t in self.graph.objects(target, RDF.type):
            if isinstance(t, URIRef) and t in _SUBJECT_TYPE_TO_MARC_6XX_TAG:
                return _SUBJECT_TYPE_TO_MARC_6XX_TAG[t]
        return None

    def _build_lineage_rank_map(self) -> dict[str, str]:
        """Per-record fallback when ``reconstruct_marc`` is invoked
        without a pre-built ``lineage_rank_map``. Delegates to the
        module-level :func:`build_lineage_rank_map` which buckets by
        ``(bib_id, tag)`` — preserves correct per-record ranking
        whether the graph holds one record or 500. The runner always
        passes a pre-built map (the production path); this fallback
        only runs for ad-hoc callers (synthetic-graph unit tests,
        scripts inspecting one record at a time)."""
        return build_lineage_rank_map(self.graph)

    def _lineage_token(self, node: Node | None) -> str | None:
        """Look up the lineage token for an entity.

        Two tiers (P-50 Phase A introduces the first; P-48 Phase A
        retained as fallback):

        1. **``bffi-prov:fromMarcField``** on the entity — the M2-post
           source-MARC-field token. Format
           ``"<bib_id>:<tag>:<within-tag-ordinal>"``. Source-grounded,
           content-independent, stable across the pipeline.

        2. **Rank-normalised M3 fragment** — the legacy
           ``"<tag>-<rank>"`` derived from the raw-bib URI fragment.
           Carried in ``_lineage_rank_map``. Falls back to this when
           the entity has no fromMarcField triple (the URI-keyed
           subject case marc2bibframe2 emits without raw URIs, plus
           records processed before P-50 Phase A shipped).
        """
        if node is None:
            return None
        if isinstance(node, URIRef | BNode):
            for token in self.graph.objects(node, V.fromMarcField):
                if isinstance(token, Literal):
                    return str(token)
        if not isinstance(node, URIRef):
            return None
        return self._lineage_rank_map.get(str(node))

    def _build_raw_origin_hints(self, work: URIRef, predicate: URIRef) -> dict[URIRef, str]:
        """For each authority URI attached to ``<work> predicate``, find
        any raw bib-URI on the same Work whose ``skos:exactMatch``
        points to that authority. Returns ``{auth_uri: raw_uri_str}``
        so :meth:`_subject_marc_tag` can recover the raw URI's
        fragment ID (e.g. ``#Place651-21``) — the only 651/648
        routing signal that survives M9's authority-binding swap.
        """
        out: dict[URIRef, str] = {}
        for raw in self.graph.objects(work, predicate):
            if not (isinstance(raw, URIRef) and str(raw).startswith(_RAW_BIB_URI_PREFIX)):
                continue
            for auth in self.graph.objects(raw, V.SKOS.exactMatch):
                if isinstance(auth, URIRef) and not str(auth).startswith(_RAW_BIB_URI_PREFIX):
                    out[auth] = str(raw)
        return out

    def _find_subject_statement_for_target(  # noqa: PLR0912 — three-tier fallback (direct → raw_origin_hints back-walk → inverse skos:exactMatch); splitting the tiers fragments shared state across helpers.
        self,
        work: URIRef,
        target: Node,
        raw_origin_hints: dict[URIRef, str] | None = None,
    ) -> Node | None:
        """Locate the M2-post-minted ``rdf:Statement`` reification that
        links this ``work`` to ``target`` as a subject occurrence.
        Returns the statement URI (so the caller can read its
        ``bffi-prov:fromMarcField`` token) or ``None`` when no such
        statement exists (e.g. records processed before P-50 Phase C
        shipped or M2-post couldn't correlate the source field).

        Used by ``_emit_genre_forms`` so 655 emits can share the same
        Statement-anchored lineage path that ``_emit_subjects`` uses
        for 600/610/611/648/650/651 — both are subject-shape
        reifications under the same ``rdf:predicate bffi:subject``
        contract.

        Three lookup paths in order:

        1. **Direct match** — ``rdf:object = target``. Hits when the
           target is the same node M2-post tagged (no M9 binding has
           moved the URI).
        2. **Raw-URI back-walk** — M9 rebinds the genre target to its
           authority URI (e.g. ``yso/p1234``); M2-post emitted the
           Statement with the *raw* ``#GenreForm655-N`` as
           ``rdf:object``. ``raw_origin_hints`` maps each authority
           URI to its originating raw URI; we look up the Statement
           by that raw URI instead.
        3. **skos:exactMatch fallback** — if no ``raw_origin_hints``
           entry exists, walk inverse ``skos:exactMatch`` from the
           target to find any raw URI that maps to it, then look
           there.
        """
        # M2-post Statements anchor on the M3-raw work URI, not on the
        # M8-canonical Work URI the converter is walking. Walk every
        # raw URI the canonical Work was derived from via
        # prov:wasDerivedFrom (plus the canonical URI itself for the
        # rare direct-anchor case).
        anchors = self._raw_work_uris(work)
        # Tier 1: direct rdf:object match.
        for anchor in anchors:
            for stmt in self.graph.subjects(RDF.subject, anchor):
                if (stmt, RDF.type, RDF.Statement) not in self.graph:
                    continue
                if (stmt, RDF.object, target) not in self.graph:
                    continue
                return stmt
        if not isinstance(target, URIRef):
            return None
        # Tier 2: back-walk via raw_origin_hints (built once per emit
        # pass; covers the M9-rebound-genre case at zero per-row cost).
        if raw_origin_hints is not None:
            raw = raw_origin_hints.get(target)
            if raw:
                raw_uri = URIRef(raw)
                for anchor in anchors:
                    for stmt in self.graph.subjects(RDF.subject, anchor):
                        if (stmt, RDF.type, RDF.Statement) not in self.graph:
                            continue
                        if (stmt, RDF.object, raw_uri) not in self.graph:
                            continue
                        return stmt
        # Tier 3: inverse skos:exactMatch — slower, only fires when
        # raw_origin_hints lacks an entry (rare; M9 should populate it).
        for raw_node in self.graph.subjects(V.SKOS.exactMatch, target):
            if isinstance(raw_node, URIRef) and str(raw_node).startswith(_RAW_BIB_URI_PREFIX):
                for anchor in anchors:
                    for stmt in self.graph.subjects(RDF.subject, anchor):
                        if (stmt, RDF.type, RDF.Statement) not in self.graph:
                            continue
                        if (stmt, RDF.object, raw_node) not in self.graph:
                            continue
                        return stmt
        return None

    def _emit_genre_forms(self, record: Element) -> None:
        work = self.work
        if work is None:
            return
        seen_authorities: set[URIRef] = self._authority_targets(work, V.BFFI.genreForm)
        raw_origin_hints = self._build_raw_origin_hints(work, V.BFFI.genreForm)
        for genre in self.graph.objects(work, V.BFFI.genreForm):
            row_result = self._subject_row(genre, seen_authorities)
            if row_result is None:
                continue
            row, used_marckey = row_result
            # Prefer Statement-anchored lineage: M2-post emits one
            # rdf:Statement reification per source 655 field with the
            # genre target as ``rdf:object`` and the source-MARC-field
            # token as ``bffi-prov:fromMarcField``. The reified
            # statement's token is per-occurrence; the target URI may
            # be shared (LCGFT / SLM / KAUNO). Fall back to the
            # legacy direct-or-back-walk lineage when no Statement is
            # found (records processed before P-50 Phase C shipped,
            # or shapes M2-post's matcher doesn't cover).
            stmt = self._find_subject_statement_for_target(work, genre, raw_origin_hints)
            lineage = (
                self._lineage_token(stmt) if stmt is not None else None
            ) or self._lineage_for_subject(genre, raw_origin_hints)
            self._emit_datafield(
                record,
                "655",
                *row,
                ind2="7",
                lineage=lineage,
                marckey_bypass=used_marckey,
            )

    def _authority_targets(self, work: URIRef, predicate: URIRef) -> set[URIRef]:
        """Return the set of authority-URI targets of ``<work> predicate``
        — every URI target that's NOT a raw M3-minted bib URI. Used to
        decide whether a raw URI on the same Work has a parallel
        authority binding (and is therefore the M9-bound redundant
        twin to be suppressed in the round-trip)."""
        out: set[URIRef] = set()
        for o in self.graph.objects(work, predicate):
            if isinstance(o, URIRef) and not str(o).startswith(_RAW_BIB_URI_PREFIX):
                out.add(o)
        return out

    def _subject_row(
        self, target: Node, authorities_on_work: set[URIRef]
    ) -> tuple[tuple[tuple[str, str], ...], bool] | None:
        """Build the subfield tuple for one 650/655 row, applying the
        raw-vs-authority dedup + skos:exactMatch redirect logic.

        Returns ``(subfields, used_marckey)`` or ``None``. ``used_marckey``
        is True when any subfield was sourced from ``bflc:marcKey`` —
        the row gets flagged as ``marckey_bypass`` in the diff.
        Returns ``None`` when the row should be suppressed entirely
        (raw URI shadowed by a sibling authority binding on the same
        Work; the authority will emit its own row independently)."""
        if isinstance(target, URIRef) and str(target).startswith(_RAW_BIB_URI_PREFIX):
            return self._raw_subject_row(target, authorities_on_work)
        return self._authority_subject_row(target)

    def _raw_subject_row(
        self, target: URIRef, authorities_on_work: set[URIRef]
    ) -> tuple[tuple[tuple[str, str], ...], bool] | None:
        """Subject-row builder for raw-bib URI targets.

        Follows ``skos:exactMatch`` to suppress when an authority
        twin emits its own row, prefers parsed ``bflc:marcKey`` over
        ``rdfs:label`` for $a / $c / $d subfield structure, falls back
        to single $a from the label. Returns ``(subfields, used_marckey)``
        — see :meth:`_subject_row`."""
        redirected = self._first_authority_redirect(target)
        if redirected is not None:
            return None
        label = self._first_label(target)
        if label is not None and any(
            self._target_label_matches(auth, label) for auth in authorities_on_work
        ):
            return None
        name_subs = self._name_subfields_from_marc_key(target)
        used_marckey = name_subs is not None
        source = self._first_source(target)
        subs: list[tuple[str, str]] = []
        if name_subs:
            subs.extend(name_subs)
        elif label:
            subs.append(("a", label))
        if source:
            subs.append(("2", source))
        return (tuple(subs), used_marckey) if subs else None

    def _authority_subject_row(
        self, target: Node
    ) -> tuple[tuple[tuple[str, str], ...], bool] | None:
        """Subject-row builder for authority-URI / blank-node targets.

        Tries marcKey on the target first; falls back to walking
        inverse ``skos:exactMatch`` to find a raw-bib origin with
        marcKey (so $a/$c survive M9-bound subjects). Otherwise emits
        a single $a from the authority's prefLabel (or, last-resort,
        the raw URI's label). Returns ``(subfields, used_marckey)`` —
        see :meth:`_subject_row`."""
        name_subs = self._name_subfields_from_marc_key(target)
        if name_subs is None and isinstance(target, URIRef):
            name_subs = self._name_subfields_from_raw_origin(target)
        used_marckey = name_subs is not None
        label = self._authority_label(target)
        if label is None and isinstance(target, URIRef):
            label = self._raw_origin_label(target)
        source = self._first_source(target)
        subs: list[tuple[str, str]] = []
        if name_subs:
            subs.extend(name_subs)
        elif label:
            subs.append(("a", label))
        if source:
            subs.append(("2", source))
        if isinstance(target, URIRef):
            subs.append(("0", str(target)))
        return (tuple(subs), used_marckey) if subs else None

    def _name_subfields_from_raw_origin(self, authority: URIRef) -> list[tuple[str, str]] | None:
        """Walk inverse ``skos:exactMatch`` from an authority URI to a
        raw-bib URI and return its marcKey-parsed subfields. Returns
        ``None`` when no raw origin carries marcKey."""
        for raw in self.graph.subjects(V.SKOS.exactMatch, authority):
            if not (isinstance(raw, URIRef) and str(raw).startswith(_RAW_BIB_URI_PREFIX)):
                continue
            name_subs = self._name_subfields_from_marc_key(raw)
            if name_subs is not None:
                return name_subs
        return None

    def _raw_origin_label(self, authority: URIRef) -> str | None:
        """Walk inverse ``skos:exactMatch`` from an authority URI back
        to any raw bib-URI that pointed to it; return that raw URI's
        ``rdfs:label`` (= cataloguer's typed $a). Last-resort fallback
        for authority URIs without a loaded Finto prefLabel."""
        for raw in self.graph.subjects(V.SKOS.exactMatch, authority):
            if isinstance(raw, URIRef) and str(raw).startswith(_RAW_BIB_URI_PREFIX):
                lbl = self._first_label(raw)
                if lbl is not None:
                    return lbl
        return None

    def _first_authority_redirect(self, raw_uri: URIRef) -> URIRef | None:
        """Return the first ``skos:exactMatch`` target of a raw URI
        that points OUT of the raw-bib namespace. None if no such
        redirect exists."""
        for o in self.graph.objects(raw_uri, V.SKOS.exactMatch):
            if isinstance(o, URIRef) and not str(o).startswith(_RAW_BIB_URI_PREFIX):
                return o
        return None

    def _target_label_matches(self, target: Node, label: str) -> bool:
        """True if ``target`` has an ``rdfs:label`` or ``skos:prefLabel``
        equal to ``label`` in some language (case-sensitive — matches the
        M9 binding semantics)."""
        for prop in (V.RDFS.label, SKOS.prefLabel):
            for val in self.graph.objects(target, prop):
                if isinstance(val, Literal) and str(val) == label:
                    return True
        return False

    def _first_source(self, node: Node) -> str | None:
        """Derive the cataloguer-typed MARC ``$2`` source-vocab code
        for a subject / genre-form target.

        Three strategies, applied in order:

        1. **URI-namespace lookup**: if the target URI's namespace
           matches a Finto / LoC vocab pattern (``…/onto/kauno/``,
           ``…/onto/yso-paikat/``, ``…/au:slm:``, etc.), return the
           full MARC code WITH the language suffix
           ("kauno/fin", "yso/fin", "slm/fin", "bella/swe"). This is
           the most reliable strategy because marc2bibframe2 strips
           the ``/fin`` language suffix when normalising MARC ``$2 …/fin``
           to ``bf:source <…/subjectSchemes/kauno>``, but the
           subject URI namespace IS the authoritative source of truth.

        2. **bf:source literal**: when M3 routed the cataloguer's
           ``$2`` verbatim as a literal (the raw / un-reconciled
           subject case), return it as-is.

        3. **bf:source URI tail**: legacy fallback — strip the URI
           prefix and return the tail (loses ``/fin`` but keeps the
           data flowing).
        """
        if isinstance(node, URIRef):
            ns_code = _marc_source_from_uri_namespace(str(node))
            if ns_code is not None:
                return ns_code
        for val in self.graph.objects(node, V.BF.source):
            if isinstance(val, Literal):
                return str(val)
            if isinstance(val, URIRef):
                s = str(val)
                return s.rsplit("/", 1)[-1] if "/" in s else s
        return None

    def _emit_added_entries(self, record: Element) -> None:
        # Non-primary contributions on the Expression — routed to 700
        # (personal name), 710 (corporate body), or 711 (meeting) based
        # on the agent URI's M3-minted fragment ID:
        #   .../#Agent700-N → 700 (personal)
        #   .../#Agent710-N → 710 (corporate)
        #   .../#Agent711-N → 711 (meeting)
        # KANTO finaf URIs default to 700 (the dominant case); a richer
        # heuristic could check the KANTO concept type but the URI
        # fragment is the most reliable signal for unresolved agents.
        expr = self.expression
        if expr is None:
            return
        for contrib in self.graph.objects(expr, V.BFFI.contribution):
            types = set(self.graph.objects(contrib, RDF.type))
            if V.BFFI.PrimaryContribution in types:
                continue  # already emitted as 100
            for agent in self.graph.objects(contrib, V.BFFI.agent):
                name_subs = self._name_subfields_from_marc_key(agent)
                used_marckey = name_subs is not None
                if name_subs is None:
                    label = self._first_label(agent)
                    if not label:
                        continue
                    name_subs = [("a", label)]
                role_subs = self._collect_role_subs(contrib)
                id_subs = self._collect_agent_id_subs(agent)
                tag = self._added_entry_tag(agent)
                lineage = self._lineage_token(agent)
                self._emit_datafield(
                    record,
                    tag,
                    *name_subs,
                    *role_subs,
                    *id_subs,
                    ind1="1",
                    lineage=lineage,
                    marckey_bypass=used_marckey,
                )

    def _added_entry_tag(self, agent: Node) -> str:
        """Route a non-primary contribution to 700 / 710 / 711 by the
        agent URI's M3-minted fragment ID. Default 700 (personal)."""
        if not isinstance(agent, URIRef):
            return "700"
        s = str(agent)
        if "#Agent710" in s:
            return "710"
        if "#Agent711" in s:
            return "711"
        return "700"

    def _emit_aggregated_component_analytical_entries(self, record: Element) -> None:
        """MARC 700 ind2=2 — analytical added entry for an aggregating
        record's component agents (P-52 Phase G.bis).

        Walks ``bffi:Expression bffi:aggregates ?component →
        bffi:contribution → bffi:agent → rdfs:label`` and emits one
        ``700 ind1=1 ind2=2 $a <agent name>`` row per component-agent
        pair. The component contribution chain is synthesised by M3's
        ``_enrich_aggregation_components_with_agents`` from each
        Hub's ``bflc:marcKey`` ``$g`` subfield (which carries the
        analytical agent name in MARC 730 / 740 source rows like
        ``"73000 $aFame /$gGore, Michael"``).

        Lineage rides on the component Expression's
        ``bffi-prov:fromMarcField`` token when present (Phase F's
        component-minting flows the token through from the source
        ``bf:Hub``); otherwise no lineage emit.

        Source records that aren't aggregating produce zero rows
        (the walk simply finds no ``bffi:aggregates`` edges).
        """
        expr = self.expression
        if expr is None:
            return
        seen: set[str] = set()
        for component in self.graph.objects(expr, V.BFFI.aggregates):
            if not isinstance(component, URIRef | BNode):
                continue
            for contrib in self.graph.objects(component, V.BFFI.contribution):
                for agent in self.graph.objects(contrib, V.BFFI.agent):
                    label = self._first_label(agent)
                    if not label or label in seen:
                        continue
                    seen.add(label)
                    self._emit_datafield(
                        record,
                        "700",
                        ("a", label),
                        ind1="1",
                        ind2="2",
                        lineage=self._lineage_token(component),
                    )

    def _emit_related_uniform_titles(self, record: Element) -> None:
        """MARC 730 / 740 (added entry — title).

        BFFI 1.0.0 path (canonical, ``owl:equivalentProperty`` /
        ``owl:equivalentClass`` to the BIBFRAME counterparts):

            bffi:Manifestation
              └─ bffi:relation
                 └─ bffi:Relation
                    ├─ bffi:relationship   <…/relatedwork>
                    └─ bffi:associatedResource
                       ├─ bf:Hub  → MARC 730   (uniform title)
                       │   ├─ bf:title → bf:Title → bf:mainTitle
                       │   └─ bflc:marcKey
                       └─ bf:Work → MARC 740   (uncontrolled
                           ├─ bf:title → bf:Title → bf:mainTitle    related/
                           └─ bflc:marcKey                        analytical)

        Strategy per row: prefer ``bflc:marcKey`` (carries the exact
        source subfield structure) when present, otherwise split
        ``bf:mainTitle`` on `` / `` into ``$a`` + ``$g``. Tag picked by
        the associated resource's ``rdf:type``: ``bf:Hub`` → 730,
        ``bf:Work`` → 740. ind1 = 0 ("no nonfiling characters"); ind2 =
        blank. (740 ind2=2 "analytical entry" semantics are not
        preserved — BFFI doesn't carry that bit.)
        """
        manif = self.manifestation
        if manif is None:
            return
        for rel in self.graph.objects(manif, V.BFFI.relation):
            # The marker is the associatedResource's rdf:type. Series
            # (bf:Series) is handled by _emit_series_statement.
            for resource in self.graph.objects(rel, V.BFFI.associatedResource):
                types = set(self.graph.objects(resource, RDF.type))
                if V.BF.Hub in types:
                    tag = "730"
                elif V.BF.Work in types:
                    tag = "740"
                else:
                    continue
                subs, used_marckey = self._related_title_subfields(resource)
                if not subs:
                    continue
                lineage = self._lineage_token(resource)
                self._emit_datafield(
                    record,
                    tag,
                    *subs,
                    ind1="0",
                    lineage=lineage,
                    marckey_bypass=used_marckey,
                )

    def _related_title_subfields(self, hub: Node) -> tuple[tuple[tuple[str, str], ...], bool]:
        """Build the 730 / 740 subfield tuple for one related title.

        Returns ``(subfields, used_marckey)``. Three tiers, preferring
        BFFI-native paths (no ``marckey_bypass`` flag) before falling
        back to ``bflc:marcKey`` parsing:

        Tier 1 — **bf:mainTitle split** (P-168 BFFI-native): take the
        ``bf:title → bf:Title → bf:mainTitle`` literal and split on
        ``" / "`` for ``$a`` (title proper) + ``$g`` (responsibility /
        composer reference). Combined with structured
        ``bf:partNumber`` / ``bf:partName`` for ``$n`` / ``$p``. No
        marckey_bypass — both ``mainTitle`` and the part predicates
        are dedicated BFFI predicates marc2bibframe2 emits natively.

        Tier 2 — **bflc:marcKey** (legacy fallback): parses ``$a`` +
        ``$g`` + ``$n`` + ``$p`` from the marcKey string. Used only
        when the Hub has no ``bf:title`` / ``bf:mainTitle`` chain.
        Flagged as ``marckey_bypass`` so the audit shows the
        dependency.

        Tier 3 — **structured only**: ``$n`` / ``$p`` only when the
        Hub has neither marcKey nor title. Defensive.
        """
        structured = self._hub_title_part_subs(hub)
        structured_codes = {code for code, _ in structured}
        # Tier 1 — BFFI-native bf:mainTitle split
        for title in self.graph.objects(hub, V.BFFI.title):
            for mt in self.graph.objects(title, V.BFFI.mainTitle):
                if isinstance(mt, Literal):
                    base = list(_split_title_responsibility(str(mt)))
                    if base:
                        return _merge_structured_parts(base, structured), False
        # Tier 2 — marcKey fallback
        for mk in self.graph.objects(hub, V.BFLC.marcKey):
            if not isinstance(mk, Literal):
                continue
            parsed = _parse_marc_key_subfields(str(mk), ("a", "n", "p", "g"))
            if not parsed:
                continue
            base = []
            used_marckey = False
            for code, value in parsed:
                if code in ("n", "p") and code in structured_codes:
                    continue
                base.append((code, value))
                used_marckey = True
            return _merge_structured_parts(base, structured), used_marckey
        # Tier 3 — structured only
        return tuple(structured), False

    def _emit_bib_id_local(self, record: Element, bib_id: str | None) -> None:
        if not bib_id:
            return
        self._emit_datafield(record, "907", ("a", f".{bib_id}"))

    def _emit_vernacular_880(self, record: Element) -> None:
        """MARC 880 — Alternate Graphic Representation (vernacular
        forms paired with Latin transliterations).

        marc2bibframe2 already pairs 880s natively when it processes
        source MARC: when both a primary tag (e.g. 245, 100, 700,
        260) and its `$6`-linked 880 exist, marc2bibframe2 emits
        BOTH literals on the SAME BIBFRAME entity, with ``xml:lang``
        on the vernacular form (typically the language code derived
        from the source `$6 …/(script-code)` convention). M3 SPARQL
        preserves both literals through to canonical.

        So the data is already in the canonical graph — this
        emitter just walks for language-tagged literal companions
        on the key predicates and re-emits each as a separate MARC
        880 row with a reconstructed ``$6 <primary-tag>-NN/<script>``
        link. The script code in ``$6`` is recovered via Unicode-
        block detection on the literal text (Cyrillic block →
        ``(N``, Arabic → ``(3``, etc.). The ``$<primary-tag>-NN``
        position counter uses ``01`` by default — the Helmet
        corpus has at most one vernacular pair per field in 99 %+
        of records (per the L-09 entry in
        ``docs/bffi_limitations.md``).

        Coverage scope: targets the highest-volume vernacular
        carriers found in the corpus:

          - ``bf:mainTitle@<lang>`` on the Manifestation's
            ``bffi:title`` → 880 paired with 245
          - ``bffi:responsibilityStatement@<lang>`` on Manifestation
            → 880 paired with 245 (statement of responsibility lives
            with the title)
          - ``bflc:simplePlace`` / ``bflc:simpleAgent`` /
            ``bflc:simpleDate`` ``@<lang>`` on ProvisionActivity →
            880 paired with 264 (one row per non-default-language
            literal, combining the place/agent/date if all three
            exist in the vernacular)
          - ``bffi:publicationStatement@<lang>`` on Manifestation →
            880 paired with 264 (single flat string form of place +
            agent + date)
          - ``rdfs:label@<lang>`` on the primary contribution agent
            (Work-side PrimaryContribution) → 880 paired with 100
          - ``rdfs:label@<lang>`` on each non-primary contribution
            agent → 880 paired with 700

        Out of scope (deferred): 130 / 240 / 246 / 600 / 610 / 611 /
        630 / 650 / 651 / 655 / 730 / 740 / 800 / 810 / 830 — these
        have lower vernacular volume in the corpus and follow the
        same pattern as the included set; extension is mechanical
        when needed.
        """
        seq = 0
        # 245 / 245-statement-of-responsibility
        for ent in [self.manifestation]:
            for title_node in self.graph.objects(ent, V.BFFI.title):
                seq = self._emit_vernacular_for_predicate(
                    record, "245", title_node, V.BFFI.mainTitle, seq
                )
            seq = self._emit_vernacular_for_predicate(
                record, "245", ent, V.BFFI.responsibilityStatement, seq
            )
        # 264 / publication statement — both the flat string and the
        # simple* triple chain.
        seq = self._emit_vernacular_for_predicate(
            record, "264", self.manifestation, V.BFFI.publicationStatement, seq
        )
        for pa in self.graph.objects(self.manifestation, V.BFFI.provisionActivity):
            for pred in (V.BFLC.simplePlace, V.BFLC.simpleAgent, V.BFLC.simpleDate):
                seq = self._emit_vernacular_for_predicate(record, "264", pa, pred, seq)
        # 100 — primary contribution agent rdfs:label vernacular.
        work = self.work
        if work is not None:
            for contrib in self.graph.objects(work, V.BFFI.contribution):
                if V.BFFI.PrimaryContribution not in set(self.graph.objects(contrib, RDF.type)):
                    continue
                for agent in self.graph.objects(contrib, V.BFFI.agent):
                    seq = self._emit_vernacular_for_predicate(
                        record, "100", agent, V.RDFS.label, seq
                    )
        # 700 — non-primary contribution agent rdfs:label vernacular.
        expr = self.expression
        if expr is not None:
            for contrib in self.graph.objects(expr, V.BFFI.contribution):
                if V.BFFI.PrimaryContribution in set(self.graph.objects(contrib, RDF.type)):
                    continue
                for agent in self.graph.objects(contrib, V.BFFI.agent):
                    seq = self._emit_vernacular_for_predicate(
                        record, "700", agent, V.RDFS.label, seq
                    )

    def _emit_vernacular_for_predicate(
        self,
        record: Element,
        primary_tag: str,
        subject: Node,
        predicate: URIRef,
        seq: int,
    ) -> int:
        """For ``subject ?predicate ?literal`` where the literal
        carries a language tag (and a companion untagged literal
        exists under the same predicate), emit an 880 row paired to
        ``primary_tag``. Returns the next ``seq`` counter.

        Pairing logic: the untagged literal is the primary form
        already emitted in ``primary_tag``; the language-tagged
        literal is the vernacular companion. If no untagged
        companion exists, skip — the language tag may be carried
        for genuine language-marker reasons (a Russian title on a
        Russian original) rather than vernacular pairing.
        """
        literals = list(self.graph.objects(subject, predicate))
        untagged_count = sum(1 for lit in literals if isinstance(lit, Literal) and not lit.language)
        if untagged_count == 0:
            return seq
        for lit in literals:
            if not isinstance(lit, Literal) or not lit.language:
                continue
            seq += 1
            occurrence = f"{seq:02d}"
            script_code = _detect_marc_script_code(str(lit))
            self._emit_datafield(
                record,
                "880",
                ("6", f"{primary_tag}-{occurrence}/{script_code}"),
                ("a", str(lit)),
                lineage=self._lineage_token(subject),
            )
        return seq


def _parse_marc_key_subfields(
    marc_key: str, codes: tuple[str, ...] | None = None
) -> tuple[tuple[str, str], ...]:
    """Parse a ``bflc:marcKey`` string like ``"73000 $aTitle$gAuthor"``
    or ``"60004$aMikki$c(fictional)"`` into
    ``(($a, "Title"), ($g, "Author"))``.

    Accepts both the space-separated form (``"73000 $a..."``) and the
    no-space form (``"60004$a..."``) that marc2bibframe2 alternates
    between. Everything from the first ``$`` onwards is parsed; the
    leading tag + indicator prefix is dropped.

    ``codes=(code1, code2, …)`` filters + reorders the output to
    those codes. ``codes=None`` returns all parsed subfields in
    source-MARC encounter order. Returns an empty tuple when no
    ``$`` is present.
    """
    first_dollar = marc_key.find("$")
    if first_dollar < 0:
        return ()
    body = marc_key[first_dollar:]
    # Split on "$" — first piece is empty (we sliced from $);
    # subsequent pieces start with the subfield code.
    encounter: list[tuple[str, str]] = []
    seen_codes: set[str] = set()
    for chunk in body.split("$")[1:]:
        if not chunk:
            continue
        code, value = chunk[0], chunk[1:]
        if code in seen_codes:
            continue  # keep first occurrence
        seen_codes.add(code)
        encounter.append((code, value))
    if codes is None:
        return tuple(encounter)
    code_to_value = dict(encounter)
    return tuple((c, code_to_value[c]) for c in codes if c in code_to_value)


def _split_title_responsibility(text: str) -> tuple[tuple[str, str], ...]:
    """Split a `"Title / Responsibility"` MARC main-title literal into
    ``$a`` (with trailing `` /`` preserved) + ``$g``. Returns just
    ``($a, text)`` when the splitter `` / `` is absent."""
    sep = " / "
    if sep in text:
        head, _, tail = text.partition(sep)
        return (("a", f"{head} /"), ("g", tail))
    return (("a", text),)


def _merge_structured_parts(
    base: list[tuple[str, str]], structured: list[tuple[str, str]]
) -> tuple[tuple[str, str], ...]:
    """Insert structured ``$n`` / ``$p`` subfields into ``base`` in
    MARC subfield order — after ``$a``, before ``$g`` / ``$l``.

    P-49 Layer 1 helper for 240 / 730 / 740 where ``$a`` comes from a
    marcKey-or-mainTitle parse and ``$n`` / ``$p`` come from structured
    ``bf:partNumber`` / ``bf:partName``. Inserts at the first ``$g`` or
    ``$l`` position so the output reads ``$a $n $p $g`` / ``$a $n $p $l``.
    Appends to the tail when neither ``$g`` nor ``$l`` is present.
    """
    if not structured:
        return tuple(base)
    merged: list[tuple[str, str]] = []
    inserted = False
    for code, value in base:
        if code in ("g", "l") and not inserted:
            merged.extend(structured)
            inserted = True
        merged.append((code, value))
    if not inserted:
        merged.extend(structured)
    return tuple(merged)


def _indent(elem: Element, level: int = 0) -> None:
    """Stdlib indent helper for pretty-printed serialisation."""
    i = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        for child in elem:
            _indent(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = i
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = i
