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
"""

from __future__ import annotations

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
        self._emit_helmet_source_marker(record)  # 040 synth marker
        self._emit_languages(record)  # 041
        self._emit_primary_contribution(record)  # 100
        self._emit_title(record)  # 245
        self._emit_publication_statement(record)  # 260
        self._emit_content_type(record)  # 336
        self._emit_media_type(record)  # 337
        self._emit_carrier_type(record)  # 338
        self._emit_digital_characteristic(record)  # 347
        self._emit_sound_characteristic(record)  # 344
        self._emit_color_content(record)  # 346
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
                # 100 ind1=1 ("surname"-form name) is the dominant
                # cataloguer choice for Helmet personal names. ind2 is
                # undefined in current MARC ⇒ blank.
                self._emit_datafield(record, "100", ("a", label), ind1="1")
                return  # only one primary

    def _first_label(self, node: Node) -> str | None:
        for prop in (V.RDFS.label, SKOS.prefLabel):
            for val in self.graph.objects(node, prop):
                if isinstance(val, Literal):
                    return str(val)
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
        self._emit_datafield(record, "245", ("a", label), ind1="1", ind2="0")

    def _emit_publication_statement(self, record: Element) -> None:
        # Parse "(<pub statement>)" suffix off the Manifestation's
        # prefLabel — that's where M3 stashed the bf:Instance's
        # bf:publicationStatement (P-45 commit 11).
        for lit in self.graph.objects(self.manifestation, SKOS.prefLabel):
            text = str(lit)
            if "(" in text and text.endswith(")"):
                pub = text[text.rindex("(") + 1 : -1].strip()
                if pub:
                    self._emit_datafield(record, "260", ("c", pub))
                    return

    def _emit_content_type(self, record: Element) -> None:
        # 336 — bffi:content lives on Expression. Not currently
        # forwarded onto BFFI in the pipeline; skip and surface in
        # diff for visibility.
        self.skipped.append("336")

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
        for subject in self.graph.objects(work, V.BFFI.subject):
            label = self._first_label(subject)
            if not label and isinstance(subject, URIRef):
                # Authority URI without an inline label — emit the
                # URI itself in $0, with $a empty (the diff surfaces
                # the missing label).
                label = None
            source = self._first_source(subject)
            # 650 = topical default. Distinguishing 600/610/611/648/651
            # from BFFI alone is heuristic — left for a future commit.
            tag = "650"
            subs: list[tuple[str, str]] = []
            if label:
                subs.append(("a", label))
            if source:
                subs.append(("2", source))
            if isinstance(subject, URIRef):
                subs.append(("0", str(subject)))
            if not subs:
                continue
            self._emit_datafield(record, tag, *subs, ind2="7")

    def _emit_genre_forms(self, record: Element) -> None:
        work = self.work
        if work is None:
            return
        for genre in self.graph.objects(work, V.BFFI.genreForm):
            label = self._first_label(genre)
            source = self._first_source(genre)
            subs: list[tuple[str, str]] = []
            if label:
                subs.append(("a", label))
            if source:
                subs.append(("2", source))
            if isinstance(genre, URIRef):
                subs.append(("0", str(genre)))
            if not subs:
                continue
            self._emit_datafield(record, "655", *subs, ind2="7")

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
        # Non-primary contributions on the Expression → 700.
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
                role_subs: list[tuple[str, str]] = []
                # Role can be:
                #  - URIRef: a LoC relator URI like .../relators/trl. We
                #    emit $4 from the URI tail (code) and $e from the
                #    URI's prefLabel (looked up in whatever graph the
                #    converter was handed — the runner merges the
                #    relators vocab dump so URI labels resolve).
                #  - BNode: M3's contrib cascade for cataloguer-typed
                #    free-text roles ($e from source MARC 700 $e).
                #    Carries an ``rdfs:label`` directly; we emit only
                #    $e (the original didn't carry a relator code).
                for role in self.graph.objects(contrib, V.BF.role):
                    if isinstance(role, URIRef):
                        role_subs.append(("4", str(role).rsplit("/", 1)[-1]))
                        relator_term = self._loc_label(role, lang_pref=("fi", "sv", "en"))
                        if relator_term:
                            role_subs.append(("e", relator_term))
                    else:
                        free_text = self._first_label(role)
                        if free_text:
                            role_subs.append(("e", free_text))
                self._emit_datafield(record, "700", ("a", label), *role_subs, ind1="1")

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
