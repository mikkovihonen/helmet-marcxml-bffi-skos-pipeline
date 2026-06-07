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

#: LoC ``organizations`` URI prefix. The URI tail reduces the MARC
#: organization code by lower-casing and removing hyphens — the
#: ``_ORG_URI_TO_MARC_CODE`` table reverses the dominant Helmet cases.
_ORG_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/organizations/"

#: Reverse lookup for the LoC organizations URI tail → cataloguer
#: MARC code. Covers the dominant Helmet cases. Unknown tails fall
#: back to ``tail.upper()`` (surfaces in the round-trip diff so
#: cataloguers see the heuristic's reach).
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


#: BIBFRAME subject-class ``rdf:type`` → MARC 6XX tag. Used by the
#: round-trip converter's ``_subject_marc_tag`` to route cataloguer-
#: typed ``$0`` URIs whose namespace alone doesn't reveal the
#: subject kind (the plain ``yso/`` URI case — `bf:Place rdf:about`
#: typing is the discriminator).
_SUBJECT_TYPE_TO_MARC_6XX_TAG: Final[dict[URIRef, str]] = {
    V.BF.Person: "600",
    V.BF.Organization: "610",
    V.BF.Meeting: "611",
    V.BF.Temporal: "648",
    V.BF.Place: "651",
    V.BF.Topic: "650",
}

#: BIBFRAME bf:ProvisionActivity subclass tail → MARC 264 ind2.
#: ind2=1 (Publication) is the dominant fallback.
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
        self._emit_related_uniform_titles(record)  # 730
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
        # Minimal synthesised leader. Real leader carries a/c/p/m/i
        # type-of-record + bibliographic-level codes that we can't
        # always recover; we emit a generic "language material /
        # monograph" shape (``nam``) which matches the dominant
        # Helmet pattern. Fields the original leader carries that we
        # cannot reconstruct (encoding level, descriptive cataloguing
        # form, multipart resource) stay as blanks.
        return "00000nam  2200000   4500"

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
        """Format the source ``bffi:transactionDate`` into the 6-char
        008 pos 00-05 representation. Returns blank when no source
        date is available."""
        for d in self.graph.objects(self.manifestation, V.BFFI.transactionDate):
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
        for ident in self.graph.objects(self.manifestation, V.BF.identifiedBy):
            types = set(self.graph.objects(ident, RDF.type))
            if V.BF.Isbn not in types:
                continue
            for value in self.graph.objects(ident, RDF.value):
                if not isinstance(value, Literal):
                    continue
                subs: list[tuple[str, str]] = [("a", str(value))]
                for qual in self.graph.objects(ident, V.BF.qualifier):
                    if isinstance(qual, Literal):
                        subs.append(("q", str(qual)))
                self._emit_datafield(record, "020", *subs)

    def _emit_publisher_numbers(self, record: Element) -> None:
        # MARC 028 publisher number / catalog number (music + video).
        # marc2bibframe2 emits ``bf:identifiedBy [a bf:AudioIssueNumber;
        # rdf:value "AM950224"]`` on bf:Instance — same shape as bf:Isbn,
        # different type. ind1=0 (issue number) is the dominant
        # cataloguer choice for audio; ind2=1 ("number, no note") is
        # the common Helmet choice but we drop to blank since we don't
        # carry that flag through.
        for ident in self.graph.objects(self.manifestation, V.BF.identifiedBy):
            types = set(self.graph.objects(ident, RDF.type))
            if V.BF.AudioIssueNumber in types:
                for value in self.graph.objects(ident, RDF.value):
                    if isinstance(value, Literal):
                        self._emit_datafield(record, "028", ("a", str(value)), ind1="0")

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
        for code in self.graph.objects(assigner, V.BF.code):
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
        for ident in self.graph.objects(self.manifestation, V.BF.identifiedBy):
            types = set(self.graph.objects(ident, RDF.type))
            if V.BF.Local not in types:
                continue
            assigner = next(iter(self.graph.objects(ident, V.BF.assigner)), None)
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
            self._emit_datafield(record, "035", ("a", formatted))

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
        # MARC 490 series statement. M3 routes the marc2bibframe2 chain
        # ``bf:Instance → bf:relation → bf:Relation →
        # bf:associatedResource → bf:Series → bf:title → bf:Title →
        # bf:mainTitle`` down to a flat ``bffi:hasSeries`` link from
        # the Manifestation to a ``bffi:Series`` node carrying
        # ``rdfs:label``. ind1 = 0 ("series not traced") is the
        # MARC default; ind2 has no meaning here.
        for series in self.graph.objects(self.manifestation, V.BFFI.hasSeries):
            label = self._first_label(series)
            if label:
                self._emit_datafield(record, "490", ("a", label), ind1="0")

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
                for code in self.graph.objects(agent, V.BF.code):
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
        for ident in self.graph.objects(agent, V.BF.identifiedBy):
            value: str | None = None
            for v in self.graph.objects(ident, RDF.value):
                if isinstance(v, Literal):
                    value = str(v)
                    break
            if value is None:
                continue
            code: str | None = None
            for source in self.graph.objects(ident, V.BF.source):
                for c in self.graph.objects(source, V.BF.code):
                    if isinstance(c, Literal):
                        code = str(c)
                        break
                if code is not None:
                    break
            formatted = f"({code}){value}" if code else value
            subs.append(("0", formatted))
        return subs

    def _collect_role_subs(self, contrib: Node) -> list[tuple[str, str]]:
        """Walk all ``bf:role`` triples on ``contrib`` and return the
        ordered ``$4`` / ``$e`` subfield pairs.

        Shapes handled:
          - URIRef role (a LoC relator URI). Emits ``$4`` from the URI
            tail (the relator code). Provides a fallback ``$e`` from
            the URI's prefLabel in the merged graph.
          - BNode role with ``rdfs:label``. Provides the preferred
            ``$e`` — the cataloguer's original Finnish / Swedish term.

        When both shapes coexist on the same contribution (the post-M3
        relator-term enrichment pass added the URI alongside the
        original blank node), prefer the BNode label for ``$e`` and
        suppress the URI's label fallback so we don't emit ``$e``
        twice. ``$4`` is taken from the URI in either case.
        """
        code: str | None = None
        uri_label: str | None = None
        bnode_label: str | None = None
        for role in self.graph.objects(contrib, V.BF.role):
            if isinstance(role, URIRef):
                code = code or str(role).rsplit("/", 1)[-1]
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

    def _emit_uniform_title(self, record: Element) -> None:
        """MARC 240 — uniform title for the work.

        Source data lives on a per-record ``bf:Hub`` (``#Hub240-N``)
        attached to the Expression via ``bffi:uniformTitleHub``. Two
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
        for hub in self.graph.objects(expr, V.BFFI.uniformTitleHub):
            structured = self._hub_title_part_subs(hub)
            mk_lit = self._first_marc_key(hub)
            if mk_lit is None:
                # No marcKey at all — emit whatever structured parts
                # exist (rare; defensive).
                if structured:
                    self._emit_datafield(record, "240", *structured, ind1="1", ind2="0")
                    return
                continue
            # Parse marcKey to recover $a (from $t) and $l. Use
            # structured for $n / $p when present.
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
                    "240",
                    *subs,
                    ind1="1",
                    ind2="0",
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
        for title in self.graph.objects(hub, V.BF.title):
            for pn in self.graph.objects(title, V.BF.partNumber):
                if isinstance(pn, Literal):
                    out.append(("n", str(pn)))
                    break
            for pname in self.graph.objects(title, V.BF.partName):
                if isinstance(pname, Literal):
                    out.append(("p", str(pname)))
                    break
            if out:
                return out
        return out

    def _first_marc_key(self, node: Node) -> Literal | None:
        for mk in self.graph.objects(node, V.BFLC.marcKey):
            if isinstance(mk, Literal):
                return mk
        return None

    def _emit_variant_title(self, record: Element) -> None:
        """MARC 246 — varying form of title. Routed through
        ``bffi:variantTitle`` → ``bf:VariantTitle`` blank node →
        ``bf:mainTitle``. Single ``$a`` per row. ind1=3
        (no note, added entry) is Helmet's dominant choice; ind2
        blank (type of title unspecified)."""
        expr = self.expression
        if expr is None:
            return
        for variant in self.graph.objects(expr, V.BFFI.variantTitle):
            for title in self.graph.objects(variant, V.BF.mainTitle):
                if isinstance(title, Literal):
                    self._emit_datafield(record, "246", ("a", str(title)), ind1="3")
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
        main_title, subtitle = self._manifestation_title_parts()
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
        self._emit_datafield(record, "245", *subs, ind1="1", ind2="0")

    def _manifestation_title_parts(self) -> tuple[str | None, str | None]:
        """Read the Manifestation's structured title — returns
        ``(main_title, subtitle)`` with either / both possibly ``None``.

        The M3 manifestation CONSTRUCT routes bf:Instance's bf:Title
        bnode to a sha1-minted ``bffi:Title`` node under
        ``bffi:title``, copying its ``bf:mainTitle``/``bf:subtitle``
        as ``bffi:mainTitle``/``bffi:subtitle``. We pick the first of
        each (Helmet records have one transcribed title per 245).
        """
        main_title: str | None = None
        subtitle: str | None = None
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
                break
        return main_title, subtitle

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
        # Structured path: one 264 per provisionActivity bnode.
        emitted_structured = False
        for prov in self.graph.objects(self.manifestation, V.BFFI.provisionActivity):
            if isinstance(prov, Literal):
                continue
            subs = self._provision_activity_subs(prov)
            if not subs:
                continue
            ind2 = self._provision_activity_ind2(prov)
            self._emit_datafield(record, "264", *subs, ind2=ind2)
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
        subs.extend(self._extent_subs())
        subs.extend(self._dimensions_subs())
        subs.extend(self._accmat_subs())
        if subs:
            self._emit_datafield(record, "300", *subs)

    def _extent_subs(self) -> list[tuple[str, str]]:
        """``$a`` + nested ``$b`` from the Manifestation's first
        ``bffi:extent``. Literal extents emit only ``$a``; bf:Extent
        bnodes walk into ``bf:note`` for ``$b`` other-physical."""
        out: list[tuple[str, str]] = []
        for ext in self.graph.objects(self.manifestation, V.BFFI.extent):
            if isinstance(ext, Literal):
                out.append(("a", str(ext)))
                return out
            lbl = self._first_label(ext)
            if lbl:
                out.append(("a", lbl))
            for note in self.graph.objects(ext, V.BF.note):
                if self._has_marc_note_type(note, "physical"):
                    nlbl = self._first_label(note)
                    if nlbl:
                        out.append(("b", nlbl))
                        return out
            return out
        return out

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
                    self._emit_datafield(record, "500", ("a", text))

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
                self._emit_datafield(record, "505", ("a", text), ind1="0")

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

    def _emit_subjects(self, record: Element) -> None:
        work = self.work
        if work is None:
            return
        seen_authorities: set[URIRef] = self._authority_targets(work, V.BFFI.subject)
        raw_origin_hints = self._build_raw_origin_hints(work, V.BFFI.subject)

        # P-50 Phase C — walk via reified ``rdf:Statement`` first. Each
        # statement has ``rdf:subject ?work ; rdf:predicate bffi:subject
        # ; rdf:object ?target`` and carries the per-record provenance
        # token on the statement URI (not on the shared target). One
        # MARC 6XX row per reified statement.
        emitted_targets: set[Node] = set()
        for stmt in self.graph.subjects(RDF.subject, work):
            if (stmt, RDF.type, RDF.Statement) not in self.graph:
                continue
            if (stmt, RDF.predicate, V.BFFI.subject) not in self.graph:
                continue
            target = next(self.graph.objects(stmt, RDF.object), None)
            if target is None:
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
            lineage = self._lineage_for_subject(genre, raw_origin_hints)
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

        Returns ``(subfields, used_marckey)``. Combines:

        - **Structured** (P-49 Layer 1): ``bf:partNumber`` / ``bf:partName``
          from the Hub's ``bf:Title`` for ``$n`` / ``$p`` when present.
          No bypass flag because the source is a structured BFFI
          predicate.

        - **bflc:marcKey** (legacy): parses ``$a`` (title proper) +
          ``$g`` (responsibility / misc). When this path supplies any
          subfield the row is flagged ``marckey_bypass`` — both $a and
          $g are P-49 Layer 3 gaps (no dedicated BFFI predicate).

        - **bf:mainTitle split** (last-resort fallback): splits the
          concatenated main title on `` / `` for ``$a`` / ``$g``. The
          music-collection idiom where marc2bibframe2 produced a Hub
          but no marcKey survived M3 propagation.
        """
        structured = self._hub_title_part_subs(hub)
        structured_codes = {code for code, _ in structured}
        for mk in self.graph.objects(hub, V.BFLC.marcKey):
            if not isinstance(mk, Literal):
                continue
            parsed = _parse_marc_key_subfields(str(mk), ("a", "n", "p", "g"))
            if not parsed:
                continue
            base: list[tuple[str, str]] = []
            used_marckey = False
            for code, value in parsed:
                if code in ("n", "p") and code in structured_codes:
                    continue
                base.append((code, value))
                used_marckey = True
            return _merge_structured_parts(base, structured), used_marckey
        # Fall back to bf:mainTitle split. Structured $n / $p still
        # apply (rare combo, defensible).
        for title in self.graph.objects(hub, V.BF.title):
            for mt in self.graph.objects(title, V.BF.mainTitle):
                if isinstance(mt, Literal):
                    return _merge_structured_parts(
                        list(_split_title_responsibility(str(mt))), structured
                    ), False
        return tuple(structured), False

    def _emit_bib_id_local(self, record: Element, bib_id: str | None) -> None:
        if not bib_id:
            return
        self._emit_datafield(record, "907", ("a", f".{bib_id}"))


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
