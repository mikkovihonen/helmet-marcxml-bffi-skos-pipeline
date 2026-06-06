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

#: M3 mints raw URIs under this prefix for cataloguer-typed subjects /
#: genre-forms that have no ``$0`` in the source MARC. M9 then emits
#: ``<raw> skos:exactMatch <authority>`` when it binds the raw URI to
#: a Finto-vocab concept. The converter follows that link so MARC ``$0``
#: carries the authority URI cataloguers care about, not pipeline-
#: internal scaffolding.
_RAW_BIB_URI_PREFIX: Final[str] = "http://urn.fi/URN:NBN:fi:bib:raw/"


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
        self._emit_extent_and_dimensions(record)  # 300
        self._emit_content_type(record)  # 336
        self._emit_media_type(record)  # 337
        self._emit_carrier_type(record)  # 338
        self._emit_digital_characteristic(record)  # 347
        self._emit_sound_characteristic(record)  # 344
        self._emit_color_content(record)  # 346
        self._emit_notes(record)  # 500
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
        # 500 general note. M3 emits ``bffi:note`` on Expression, with
        # the cataloguer's note text as ``rdf:value`` on a ``bf:Note``
        # blank node. Each distinct note becomes one 500 row.
        expr = self.expression
        if expr is None:
            return
        for note in self.graph.objects(expr, V.BFFI.note):
            # The note may be a literal directly or a blank node with
            # rdf:value.
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
            if text:
                self._emit_datafield(record, "500", ("a", text))

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
        for subject in self.graph.objects(work, V.BFFI.subject):
            row = self._subject_row(subject, seen_authorities)
            if row is None:
                continue
            tag = self._subject_marc_tag(subject)
            self._emit_datafield(record, tag, *row, ind2="7")

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

    def _subject_marc_tag(self, target: Node) -> str:
        """Route a BFFI subject node to 600 / 610 / 611 / 648 / 651 /
        650 based on URI hints. The hints are the M3-minted fragment
        IDs (``#Agent600-N``, ``#Place651-N``, etc.) for raw URIs, and
        Finto vocab namespaces (yso-paikat, yso-aika, finaf) for
        M9-bound authority URIs. Default = 650 (topical)."""
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
        return "650"

    def _emit_genre_forms(self, record: Element) -> None:
        work = self.work
        if work is None:
            return
        seen_authorities: set[URIRef] = self._authority_targets(work, V.BFFI.genreForm)
        for genre in self.graph.objects(work, V.BFFI.genreForm):
            row = self._subject_row(genre, seen_authorities)
            if row is None:
                continue
            self._emit_datafield(record, "655", *row, ind2="7")

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
        # Authority URI (or blank node) — emit normally.
        label = self._first_label(target)
        source = self._first_source(target)
        subs = []
        if label:
            subs.append(("a", label))
        if source:
            subs.append(("2", source))
        if isinstance(target, URIRef):
            subs.append(("0", str(target)))
        return tuple(subs) if subs else None

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
                self._emit_datafield(record, tag, ("a", label), *role_subs, ind1="1")

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
