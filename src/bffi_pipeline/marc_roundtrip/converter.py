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

from rdflib import Graph, Literal, URIRef
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

#: M3's raw-URI positional fragment grammar. Three named groups:
#:   - ``kind``: the M3 routing prefix (Topic / Place / Agent / Hub / ...)
#:   - ``tag``:  the source MARC datafield tag (650 / 651 / 700 / ...)
#:   - ``ord``:  the 1-indexed position of the source field within
#:               the record (M3's per-record counter).
_LINEAGE_FRAGMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"#(?P<kind>Topic|Place|Agent|Hub|MusicMedium|IntendedAudience|"
    r"CreatorCharacteristic)(?P<tag>\d{3})-(?P<ord>\d+)$"
)


def _extract_lineage_token(source: Node | None) -> str | None:
    """Parse an M3-minted raw URI fragment into a ``<tag>-<ordinal>``
    lineage token. Returns ``None`` for non-URI inputs, URIs outside
    the raw-bib namespace, and URIs whose fragment doesn't match the
    M3 positional convention.
    """
    if not isinstance(source, URIRef):
        return None
    s = str(source)
    if not s.startswith(_RAW_BIB_URI_PREFIX):
        return None
    m = _LINEAGE_FRAGMENT_RE.search(s)
    if m is None:
        return None
    return f"{m.group('tag')}-{m.group('ord')}"


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
    graph: Graph, manifestation_uri: URIRef, *, bib_id: str | None = None
) -> ReconstructedRecord:
    """Reconstruct a MARCXML record from the BFFI graph rooted at the
    given Manifestation. See module docstring for the field coverage."""
    return _Reconstructor(graph, manifestation_uri, bib_id=bib_id).build()


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

    def build(self) -> ReconstructedRecord:
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
        self._emit_helmet_source_marker(record)  # 040 synth marker
        self._emit_languages(record)  # 041
        self._emit_primary_contribution(record)  # 100
        self._emit_title(record)  # 245
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
        # 40 chars. Positions we can fill: 35-37 (primary language).
        # Others stay as blanks; the diff will surface them.
        lang_code = self._primary_language_code() or "   "
        s = list(" " * 40)
        # Synthesise minimal date type: 's' (single date) is the safest
        # default — see leader for caveat.
        s[6] = "s"
        for i, c in enumerate(lang_code[:3]):
            s[35 + i] = c
        return "".join(s)

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
        if add_marker:
            sf = SubElement(df, f"{{{MARC_NAMESPACE}}}subfield", attrib={"code": "5"})
            sf.text = ROUNDTRIP_MARKER

    def _emit_isbns(self, record: Element) -> None:
        # bf:identifiedBy → bf:Isbn → rdf:value on the Manifestation.
        for ident in self.graph.objects(self.manifestation, V.BF.identifiedBy):
            types = set(self.graph.objects(ident, RDF.type))
            if V.BF.Isbn in types:
                for value in self.graph.objects(ident, RDF.value):
                    if isinstance(value, Literal):
                        self._emit_datafield(record, "020", ("a", str(value)))

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

    def _emit_helmet_source_marker(self, record: Element) -> None:
        # 040: cataloguing source. We can't reconstruct the original
        # ``$a $b $c $d`` chain from BFFI's AdminMetadata block —
        # emit a synth row that flags the round-trip.
        self._emit_datafield(
            record,
            "040",
            ("a", "FI-HELME"),
            ("d", "FI-HELME/bffi-roundtrip"),
            add_marker=False,  # this row IS the marker
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
                label = self._first_label(agent)
                if not label:
                    continue
                role_subs = self._collect_role_subs(contrib)
                # 100 ind1=1 ("surname"-form name) is the dominant
                # cataloguer choice for Helmet personal names. ind2 is
                # undefined in current MARC ⇒ blank.
                self._emit_datafield(record, "100", ("a", label), *role_subs, ind1="1")
                return  # only one primary

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

    def _emit_title(self, record: Element) -> None:
        work = self.work
        if work is None:
            return
        label = self._first_label(work)
        if not label:
            return
        # 245 ind1=1 ("title added entry") ind2=0 (no non-filing chars
        # to skip). Both are best-effort defaults.
        # 245 $c — statement of responsibility (lifted by M3 onto the
        # Manifestation as ``bffi:responsibilityStatement``, P-47).
        subs: list[tuple[str, str]] = [("a", label)]
        for stmt in self.graph.objects(self.manifestation, V.BFFI.responsibilityStatement):
            if isinstance(stmt, Literal):
                subs.append(("c", str(stmt)))
                break
        self._emit_datafield(record, "245", *subs, ind1="1", ind2="0")

    def _emit_publication_statement(self, record: Element) -> None:
        # 260 $c date. Prefer the lifted ``bffi:publicationStatement``
        # literal (P-47 — M3 carries it from bf:Instance verbatim);
        # fall back to parsing the prefLabel suffix when the literal
        # is absent (older M3 outputs or records that synthesized the
        # prefLabel suffix without a bf:publicationStatement source).
        for stmt in self.graph.objects(self.manifestation, V.BFFI.publicationStatement):
            if isinstance(stmt, Literal):
                self._emit_datafield(record, "260", ("c", str(stmt)))
                return
        for lit in self.graph.objects(self.manifestation, SKOS.prefLabel):
            text = str(lit)
            if "(" in text and text.endswith(")"):
                pub = text[text.rindex("(") + 1 : -1].strip()
                if pub:
                    self._emit_datafield(record, "260", ("c", pub))
                    return

    def _emit_extent_and_dimensions(self, record: Element) -> None:
        # 300 $a extent ($c dimensions). Both live on the Manifestation
        # via M3's bf:Instance lift (P-47). Either may be absent — emit
        # whichever side is present; skip entirely when both are missing.
        subs: list[tuple[str, str]] = []
        for ext in self.graph.objects(self.manifestation, V.BFFI.extent):
            if isinstance(ext, Literal):
                subs.append(("a", str(ext)))
            else:
                lbl = self._first_label(ext)
                if lbl:
                    subs.append(("a", lbl))
            break
        for dim in self.graph.objects(self.manifestation, V.BFFI.dimensions):
            if isinstance(dim, Literal):
                subs.append(("c", str(dim)))
            else:
                lbl = self._first_label(dim)
                if lbl:
                    subs.append(("c", lbl))
            break
        if subs:
            self._emit_datafield(record, "300", *subs)

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
        # Look up skos:prefLabel preferentially in the requested
        # languages. Returns None if no label.
        labels: dict[str | None, str] = {}
        for val in self.graph.objects(uri, SKOS.prefLabel):
            if isinstance(val, Literal):
                labels[val.language] = str(val)
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
        for subject in self.graph.objects(work, V.BFFI.subject):
            row = self._subject_row(subject, seen_authorities)
            if row is None:
                continue
            tag = self._subject_marc_tag(subject, raw_origin_hints)
            lineage = self._lineage_for_subject(subject, raw_origin_hints)
            self._emit_datafield(record, tag, *row, ind2="7", lineage=lineage)

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
        direct = _extract_lineage_token(target)
        if direct is not None:
            return direct
        if isinstance(target, URIRef):
            raw = raw_origin_hints.get(target)
            if raw:
                return _extract_lineage_token(URIRef(raw))
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
            row = self._subject_row(genre, seen_authorities)
            if row is None:
                continue
            lineage = self._lineage_for_subject(genre, raw_origin_hints)
            self._emit_datafield(record, "655", *row, ind2="7", lineage=lineage)

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
    ) -> tuple[tuple[str, str], ...] | None:
        """Build the subfield tuple for one 650/655 row, applying the
        raw-vs-authority dedup + skos:exactMatch redirect logic.

        Returns ``None`` when the row should be suppressed entirely
        (raw URI shadowed by a sibling authority binding on the same
        Work; the authority will emit its own row independently)."""
        if isinstance(target, URIRef) and str(target).startswith(_RAW_BIB_URI_PREFIX):
            # Raw bib URI. Follow skos:exactMatch when M9 attached one
            # — that becomes the row's $0. Otherwise emit the row WITHOUT
            # $0 (the raw URI is pipeline-internal; cataloguers don't
            # want it in MARC).
            redirected = self._first_authority_redirect(target)
            if redirected is not None:
                # Honest cataloguer view: the row "is" the authority's
                # row — let the authority's own iteration emit it
                # (which is guaranteed since M9 also adds <work>
                # predicate <auth> alongside the redirect).
                return None
            # Look for a same-Work authority whose label matches —
            # M9-pre-skos-exactMatch back-compat. The match drops this
            # raw row in favour of the authority's own iteration.
            label = self._first_label(target)
            if label is not None and any(
                self._target_label_matches(auth, label) for auth in authorities_on_work
            ):
                return None
            label = self._first_label(target)
            source = self._first_source(target)
            subs: list[tuple[str, str]] = []
            if label:
                subs.append(("a", label))
            if source:
                subs.append(("2", source))
            return tuple(subs) if subs else None
        # Authority URI (or blank node) — emit normally. Use the
        # language-aware authority lookup (prefer fi > sv > en) so
        # YSO / KANTO / SLM URIs resolve to their Finnish prefLabel
        # in $a. Falls back to walking back to the originating raw
        # URI's rdfs:label (the cataloguer's typed text) when the
        # authority itself has no label — happens when the Finto
        # vocab dump for the URI's namespace wasn't loaded.
        label = self._authority_label(target)
        if label is None and isinstance(target, URIRef):
            label = self._raw_origin_label(target)
        source = self._first_source(target)
        subs = []
        if label:
            subs.append(("a", label))
        if source:
            subs.append(("2", source))
        if isinstance(target, URIRef):
            subs.append(("0", str(target)))
        return tuple(subs) if subs else None

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
        for val in self.graph.objects(node, V.BF.source):
            if isinstance(val, Literal):
                return str(val)
            if isinstance(val, URIRef):
                # If the source is the Helmet bf:Source URI etc., emit
                # the namespace tail as the cataloguer-typed code.
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
                label = self._first_label(agent)
                if not label:
                    continue
                role_subs = self._collect_role_subs(contrib)
                tag = self._added_entry_tag(agent)
                lineage = _extract_lineage_token(agent)
                self._emit_datafield(
                    record,
                    tag,
                    ("a", label),
                    *role_subs,
                    ind1="1",
                    lineage=lineage,
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

    def _emit_bib_id_local(self, record: Element, bib_id: str | None) -> None:
        if not bib_id:
            return
        self._emit_datafield(record, "907", ("a", f".{bib_id}"))


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
