"""P-56 Phase 4: per-instance discriminator routings.

The clean-rename pass in :mod:`bffi_pipeline.stages.bibframe_to_bffi.mappings`
handles every ``bf:*`` term with a direct ``owl:equivalentClass`` /
``owl:equivalentProperty`` to ``bffi:*``. Phase 4 covers the residue:
classes and predicates that don't have a single direct counterpart but
*do* have a canonical routing in `docs/bf_to_bffi_mapping.md`.

Six routings ship in this module, in the order documented in the
mapping doc:

1. **Identifier-scheme** — ``bf:Isbn`` / ``bf:Issn`` / ``bf:Ean`` /
   ``bf:AudioIssueNumber`` / ``bf:Lccn`` / ``bf:IssnL`` / ``bf:OtherIdentifier``
   → ``bffi:Identifier`` + ``bffi:source <loc-scheme-URI>``.
2. **Title-variant** — ``bf:VariantTitle`` / ``bf:ParallelTitle`` /
   ``bf:KeyTitle`` / ``bf:CollectiveTitle`` → ``bffi:Title``. The
   ``bffi:marcKey`` discriminator is preserved by the
   ``bflc:marcKey`` → ``bffi:marcKey`` rename below.
3. **Series-link** — ``bf:hasSeries`` → ``bffi:relation`` ⇒ structured
   ``bffi:Relation`` bnode with ``bffi:relationship
   <vocabulary/relationship/series>`` + ``bffi:associatedResource``.
4. **Hub** — ``bf:Hub`` → ``bffi:Work`` or ``bffi:Expression`` (or a leaf
   subclass) based on the ``bflc:marcKey`` content.
5. **Axis-default class** — ``bf:Monograph`` / ``bf:Series`` /
   ``bf:Serial`` / ``bf:MusicAudio`` / ``bf:MovingImage`` /
   ``bf:Cartography`` / ``bf:NonMusicAudio`` / ``bf:Audio`` →
   per-subject pick between the Work-axis and Expression-axis BFFI
   variants (discriminated by the subject's co-typed ``rdf:type``
   assertions; see :data:`_WORK_AXIS_SIGNALS`).
6. **Axis-default predicate** — ``bf:instanceOf`` / ``bf:hasInstance``
   / ``bf:issuance`` → BFFI defaults per :data:`AXIS_DEFAULT_PREDICATES`.

Plus one prerequisite rename:

- ``bflc:marcKey`` → ``bffi:marcKey`` (`owl:equivalentProperty` per
  ``lkd.rdf``; emit-side closes to ``bffi:`` for downstream consumers).

Each routing is a single graph-mutation function returning the number
of patterns it rewrote (or a per-discriminator counter dict for the
two routings that split — axis-default class and
provision-activity-statement). :func:`apply_all_routings` runs them in
order and returns the merged counter dict for the observability
``end`` event.

Out of scope for v0 — flagged in the mapping doc but deferred to a
follow-on:

- Hub routing currently picks the *type* per marcKey signals but does
  NOT also attach the optional facet predicates (``bffi:languageOfExpression``,
  ``bffi:musicKey``, ``bffi:version``). Those are nice-to-have signal
  promotions; the type rewrite is what unblocks closed-namespace
  discipline.
"""

from __future__ import annotations

import re
from typing import Final

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS
from rdflib.term import Node

from bffi_pipeline.bibframe import BibframeOntology, load_ontology

#: BIBFRAME namespace — the input side. Routings remove triples that
#: still carry these URIs after the clean-rename pass.
BF: Final[Namespace] = Namespace("http://id.loc.gov/ontologies/bibframe/")

#: BFLC (LoC) extension namespace — carries ``marcKey`` at the BIBFRAME
#: side; we close it to ``bffi:`` for downstream consumers.
BFLC: Final[Namespace] = Namespace("http://id.loc.gov/ontologies/bflc/")

#: BFFI emit namespace.
BFFI: Final[Namespace] = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi:")

#: LoC identifier-scheme vocabulary stem. Every BIBFRAME ``bf:Identifier``
#: subclass routes to ``<stem><scheme-token>`` on ``bffi:source``.
_LOC_IDENTIFIER_SCHEME_STEM: Final[str] = "http://id.loc.gov/vocabulary/identifiers/"

#: BIBFRAME class local names whose LoC vocabulary token doesn't match the
#: default CamelCase → kebab-case convention. Two cases:
#:
#: - ``OtherIdentifier`` collapses to just ``other`` (drops the redundant
#:   "Identifier" suffix; LoC's vocab uses the bare adjective).
#: - ``VideoRecordingNumber`` fuses ``video`` + ``recording`` into one
#:   token; LoC's vocab is ``videorecording-number``, not
#:   ``video-recording-number``.
#:
#: Everything else (``Isbn`` → ``isbn``, ``IssnL`` → ``issn-l``,
#: ``AudioIssueNumber`` → ``audio-issue-number``, ``MusicPlate`` →
#: ``music-plate``, the 36 less-common subclasses…) follows the
#: convention deterministically.
_IDENTIFIER_SCHEME_TOKEN_OVERRIDES: Final[dict[str, str]] = {
    "OtherIdentifier": "other",
    "VideoRecordingNumber": "videorecording-number",
}

_CAMEL_TO_KEBAB: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _identifier_scheme_token(local_name: str) -> str:
    """LoC vocabulary token for the given BIBFRAME class local name.

    Applies the documented override map first, falls back to the
    CamelCase → kebab-case convention (insert hyphen between lowercase
    or digit followed by uppercase; then lowercase everything).
    """
    if local_name in _IDENTIFIER_SCHEME_TOKEN_OVERRIDES:
        return _IDENTIFIER_SCHEME_TOKEN_OVERRIDES[local_name]
    return _CAMEL_TO_KEBAB.sub("-", local_name).lower()


def loc_scheme_uri(bf_class: URIRef) -> URIRef:
    """Public helper: the canonical LoC scheme URI for a BIBFRAME identifier class.

    ``bf:Isbn`` → ``<…/identifiers/isbn>``,
    ``bf:OclcNumber`` → ``<…/identifiers/oclc-number>``,
    ``bf:OtherIdentifier`` → ``<…/identifiers/other>``, etc.
    """
    local_name = str(bf_class).rsplit("/", 1)[-1]
    return URIRef(_LOC_IDENTIFIER_SCHEME_STEM + _identifier_scheme_token(local_name))


#: Predicate-side gaps with no direct ``bffi:*`` equivalent in `lkd.rdf`
#: but a natural routing through the structured ``bffi:relation`` chain
#: (same shape as Series-link). Each maps the BIBFRAME predicate to a
#: LoC ``vocabulary/relationship/<term>`` URI that the Relation bnode
#: carries on its ``bffi:relationship`` slot.
RELATION_PREDICATE_ROUTINGS: Final[dict[URIRef, URIRef]] = {
    BF.hasSeries: URIRef("http://id.loc.gov/vocabulary/relationship/series"),
    BF.accompaniedBy: URIRef("http://id.loc.gov/vocabulary/relationship/accompaniedby"),
}

#: P-56 Phase 3 axis-pick: BIBFRAME classes that BFFI splits into
#: Work-axis and Expression-axis variants. Each entry maps a ``bf:*``
#: class to a ``(work_axis_pick, expression_axis_pick)`` tuple.
#:
#: :func:`route_axis_default_classes` picks per subject: if the subject
#: carries any of :data:`_WORK_AXIS_SIGNALS` as another ``rdf:type``
#: (i.e. it's the Work URI marc2bibframe2 emitted), it routes to the
#: Work-axis variant; otherwise to the Expression-axis variant. The
#: Helmet corpus pattern is marc2bibframe2 emitting the same axis-split
#: class on BOTH the Work URI (co-typed ``bf:Work``) and the Instance
#: URI (typed ``bf:Instance`` only), so this discriminator catches the
#: Work side cleanly while the Instance side defaults to Expression
#: (the existing behaviour for Helmet's "one localised Expression
#: per record" pattern).
#:
#: ``bf:MusicAudio`` has asymmetric naming in lkd.rdf: the Work-axis
#: pick is ``bffi:MusicWork`` (no ``MusicAudioWork`` class exists);
#: only the Expression-axis pick keeps the ``MusicAudio`` prefix.
#:
#: ``bf:Audio`` shares NonMusicAudio's targets — marc2bibframe2 emits
#: ``bf:Audio`` only for non-music audio (music gets the more specific
#: ``bf:MusicAudio`` directly), so both route to the same BFFI pair.
AXIS_DEFAULT_CLASSES: Final[dict[URIRef, tuple[URIRef, URIRef]]] = {
    BF.Monograph: (BFFI.MonographWork, BFFI.MonographExpression),
    BF.Series: (BFFI.SeriesWork, BFFI.SeriesExpression),
    BF.Serial: (BFFI.SerialWork, BFFI.SerialExpression),
    BF.MusicAudio: (BFFI.MusicWork, BFFI.MusicAudioExpression),
    BF.MovingImage: (BFFI.MovingImageWork, BFFI.MovingImageExpression),
    BF.Cartography: (BFFI.CartographyWork, BFFI.CartographyExpression),
    BF.NonMusicAudio: (BFFI.NonMusicAudioWork, BFFI.NonMusicAudioExpression),
    BF.Audio: (BFFI.NonMusicAudioWork, BFFI.NonMusicAudioExpression),
}

#: ``rdf:type`` assertions that signal a subject is the Work-axis side.
#: The set covers both clean-rename outcomes (``bffi:BibframeWork`` ←
#: ``bf:Work``) and Hub-routing outcomes (``bffi:Work`` /
#: ``bffi:AggregatingWork`` / ``bffi:Arrangement``). Any subject
#: carrying one of these as a co-type is routed to the Work-axis
#: variant by :func:`route_axis_default_classes`.
_WORK_AXIS_SIGNALS: Final[frozenset[URIRef]] = frozenset(
    {
        BFFI.BibframeWork,
        BFFI.Work,
        BFFI.AggregatingWork,
        BFFI.Arrangement,
    }
)

#: Per-statement axis discriminator: BIBFRAME predicates that BFFI
#: splits into Work-axis and Expression-axis variants. Each entry maps
#: a ``bf:*`` predicate to a ``(work_axis_pick, expression_axis_pick)``
#: tuple. :func:`route_axis_default_predicates` picks per statement by
#: inspecting the rdf:type of either the subject or the object,
#: depending on the predicate's direction:
#:
#:   - ``bf:instanceOf`` (Manifestation → Work/Expression): inspect the
#:     OBJECT's rdf:type. Object typed bffi:Expression (or descendant)
#:     → ``bffi:expressionManifested``; otherwise → ``bffi:workManifested``.
#:   - ``bf:hasInstance`` (Work/Expression → Manifestation): inspect the
#:     SUBJECT's rdf:type. Subject typed bffi:Expression (or descendant)
#:     → ``bffi:manifestationOfExpression``; otherwise →
#:     ``bffi:manifestationOfWork``.
#:   - ``bf:issuance`` is a flat rename — both tuple slots are
#:     ``bffi:issuance``. The ``lkd.rdf`` peer ``bffi:extensionPlan``
#:     is NOT an alternative for the same triple; it's a separate
#:     concept linked to a different RDA term list
#:     (``RDAExtensionPlan`` m5119, on the Work side) while
#:     ``bffi:issuance`` links to ``ModeIssue`` (m4372, on the
#:     Manifestation side). Both happen to carry
#:     ``bffi-meta:broadMatch bf:issuance`` but they are not
#:     interchangeable.
AXIS_DEFAULT_PREDICATES: Final[dict[URIRef, tuple[URIRef, URIRef]]] = {
    BF.instanceOf: (BFFI.workManifested, BFFI.expressionManifested),
    BF.hasInstance: (BFFI.manifestationOfWork, BFFI.manifestationOfExpression),
    BF.issuance: (BFFI.issuance, BFFI.issuance),
}

#: ``rdf:type`` assertions that signal a subject (or object) is on the
#: Expression axis. The set covers the BFFI Expression class and its
#: declared descendants in ``lkd.rdf`` — :func:`route_axis_default_predicates`
#: uses this as the per-statement discriminator. Any axis-default
#: predicate statement whose discriminator-side type intersects this
#: set routes to the Expression-axis variant; everything else lands on
#: the Work-axis default.
_EXPRESSION_AXIS_SIGNALS: Final[frozenset[URIRef]] = frozenset(
    {
        BFFI.Expression,
        BFFI.AggregatingExpression,
        BFFI.MonographExpression,
        BFFI.SeriesExpression,
        BFFI.SerialExpression,
        BFFI.MusicAudioExpression,
        BFFI.MovingImageExpression,
        BFFI.CartographyExpression,
        BFFI.NonMusicAudioExpression,
    }
)

#: BIBFRAME ``bf:Title`` subclasses that BFFI collapses into the
#: ``bffi:Title`` anchor + ``bffi:marcKey`` discriminator.
TITLE_VARIANT_CLASSES: Final[tuple[URIRef, ...]] = (
    BF.VariantTitle,
    BF.ParallelTitle,
    BF.KeyTitle,
    BF.CollectiveTitle,
)

#: Relationship URI for Series membership (LoC's relationships vocab).
SERIES_RELATIONSHIP: Final[URIRef] = URIRef("http://id.loc.gov/vocabulary/relationship/series")


# --- prerequisite rename -------------------------------------------------


def rename_bflc_marckey(graph: Graph) -> int:
    """Rewrite every ``?s bflc:marcKey ?lit`` to ``?s bffi:marcKey ?lit``.

    ``bffi:marcKey owl:equivalentProperty bflc:marcKey`` per ``lkd.rdf``;
    closing to the BFFI namespace at emit time keeps consumers on a
    single vocabulary. The literal value is preserved verbatim — the
    downstream discriminator (first-3-char tag + subfield codes) reads
    the same content.
    """
    rewritten = 0
    for s, _, o in list(graph.triples((None, BFLC.marcKey, None))):
        graph.remove((s, BFLC.marcKey, o))
        graph.add((s, BFFI.marcKey, o))
        rewritten += 1
    return rewritten


# --- routing 1: Identifier-scheme ---------------------------------------


def route_identifier_schemes(graph: Graph, ontology: BibframeOntology | None = None) -> int:
    """``bf:Isbn`` / ``bf:Issn`` / etc. → ``bffi:Identifier`` + ``bffi:source``.

    The mapping doc's Identifier-scheme routing: every BIBFRAME
    subclass of ``bf:Identifier`` collapses into the ``bffi:Identifier``
    anchor with the scheme encoded as a LoC-vocabulary URI on
    ``bffi:source``. The ``rdf:value`` carrying the actual identifier
    text is left untouched.

    Subclass discovery is ontology-driven via :func:`load_ontology`,
    so the routing automatically picks up any ``bf:Identifier``
    descendant declared in BIBFRAME 3.0.1 (currently 52 subclasses).
    Subclasses that BFFI's ``lkd.rdf`` already covers via
    ``owl:equivalentClass`` (``bf:Local`` → ``bffi:Local``,
    ``bf:ShelfMark`` → ``bffi:ShelfMark``) get rewritten by the
    upstream clean-rename pass and are no-ops here — the
    ``graph.subjects()`` query returns zero matches for them.

    Returns the count of identifier blocks rewritten across all
    schemes. Pass ``ontology`` explicitly in tests using a fixture
    snippet; production callers leave it ``None`` to use the cached
    vendored vocab.
    """
    if ontology is None:
        ontology = load_ontology()
    rewritten = 0
    for bf_class in ontology.class_descendants(BF.Identifier):
        scheme_uri = loc_scheme_uri(bf_class)
        for subject in list(graph.subjects(RDF.type, bf_class)):
            graph.remove((subject, RDF.type, bf_class))
            graph.add((subject, RDF.type, BFFI.Identifier))
            graph.add((subject, BFFI.source, scheme_uri))
            rewritten += 1
    return rewritten


# --- routing 2: Title-variant -------------------------------------------


def route_title_variants(graph: Graph) -> int:
    """``bf:VariantTitle`` / ``bf:ParallelTitle`` / ``bf:KeyTitle`` /
    ``bf:CollectiveTitle`` → ``bffi:Title``.

    BFFI deliberately collapses the BIBFRAME Title subclass tree into
    one class with marcKey-discriminated instances. The marcKey itself
    is preserved by :func:`rename_bflc_marckey`.
    """
    rewritten = 0
    for bf_class in TITLE_VARIANT_CLASSES:
        for subject in list(graph.subjects(RDF.type, bf_class)):
            graph.remove((subject, RDF.type, bf_class))
            graph.add((subject, RDF.type, BFFI.Title))
            rewritten += 1
    return rewritten


# --- routing 4: Series-link ---------------------------------------------


def _route_predicate_via_relation(graph: Graph, bf_pred: URIRef, relationship_uri: URIRef) -> int:
    """Rewrite ``?m <bf_pred> ?o`` to the structured ``bffi:relation`` chain.

    Used by Series-link routing and any other BIBFRAME predicate that
    BFFI exposes through the general ``bffi:Relation`` shape with a
    LoC-namespaced ``bffi:relationship`` URI.
    """
    rewritten = 0
    for s, _, o in list(graph.triples((None, bf_pred, None))):
        graph.remove((s, bf_pred, o))
        rel_bnode = BNode()
        graph.add((s, BFFI.relation, rel_bnode))
        graph.add((rel_bnode, RDF.type, BFFI.Relation))
        graph.add((rel_bnode, BFFI.relationship, relationship_uri))
        graph.add((rel_bnode, BFFI.associatedResource, o))
        rewritten += 1
    return rewritten


def route_series_links(graph: Graph) -> int:
    """``?m bf:hasSeries ?s`` → structured ``bffi:relation`` chain.

    The mapping doc's Series-link routing: a fresh ``bffi:Relation``
    bnode carries ``bffi:relationship <…/relationship/series>`` +
    ``bffi:associatedResource ?s``. ``?m bffi:relation [relation-bnode]``
    threads it back onto the Manifestation.
    """
    return _route_predicate_via_relation(graph, BF.hasSeries, SERIES_RELATIONSHIP)


def route_relation_predicates(graph: Graph) -> int:
    """Catch-all for predicates with no direct ``bffi:*`` equivalent but a
    natural routing through ``bffi:relation`` (see
    :data:`RELATION_PREDICATE_ROUTINGS`). Covers ``bf:accompaniedBy``
    today; the table extends with future true-gap predicates.

    Excludes ``bf:hasSeries`` (handled by :func:`route_series_links`
    above so its counter stays separate in the observability summary).
    """
    rewritten = 0
    for bf_pred, relationship_uri in RELATION_PREDICATE_ROUTINGS.items():
        if bf_pred == BF.hasSeries:
            continue
        rewritten += _route_predicate_via_relation(graph, bf_pred, relationship_uri)
    return rewritten


# --- routing 5: Hub ------------------------------------------------------


def _hub_target_type(marc_key: str) -> URIRef:  # noqa: PLR0911 — the routing table from the mapping doc is intentionally flat; collapsing branches into a lookup dict would obscure which subfield drives which target.
    """Discriminate a ``bf:Hub`` by its ``marcKey`` content.

    Implements the mapping doc's Hub routing table (first-match-wins).
    The marcKey shape is ``<3-char-tag><ind1><ind2> $a…$l…$o…`` etc.
    Empty / missing marcKey falls through to the safe ``bffi:Work``
    default.
    """
    if not marc_key:
        return BFFI.Work
    tag = marc_key[:3]

    # $o = arrangement; Expression-level by definition.
    if "$o" in marc_key:
        return BFFI.Arrangement
    # $l language qualifier — the dominant Expression signal.
    if "$l" in marc_key:
        return BFFI.Expression
    # $r key — Expression-level.
    if "$r" in marc_key:
        return BFFI.Expression
    # $s version — Expression-level.
    if "$s" in marc_key:
        return BFFI.Expression
    # 100/700 + $t (author-attributed uniform title): Work-level.
    if tag in ("100", "700") and "$t" in marc_key:
        return BFFI.Work
    # 130/830 series uniform title: axis-pick. v0 defaults to
    # Expression per the mapping doc's recommendation.
    if tag in ("130", "830"):
        return BFFI.SeriesExpression
    # 730/740 plain transcribed title with no Expression signal.
    if tag in ("730", "740"):
        return BFFI.Work
    return BFFI.Work


def route_hubs(graph: Graph) -> int:
    """``bf:Hub`` → ``bffi:Work`` / ``bffi:Expression`` / leaf subclass.

    Per-instance choice driven by the ``bffi:marcKey`` literal already
    attached to the Hub bnode (which started as ``bflc:marcKey`` from
    marc2bibframe2 — :func:`rename_bflc_marckey` must run first).
    """
    rewritten = 0
    for hub in list(graph.subjects(RDF.type, BF.Hub)):
        marc_key_lit = next(graph.objects(hub, BFFI.marcKey), None)
        marc_key = str(marc_key_lit) if isinstance(marc_key_lit, Literal) else ""
        target = _hub_target_type(marc_key)
        graph.remove((hub, RDF.type, BF.Hub))
        graph.add((hub, RDF.type, target))
        rewritten += 1
    return rewritten


# --- routing 6: axis-default class rewrites -----------------------------


def route_axis_default_classes(graph: Graph) -> dict[str, int]:
    """Per-subject axis discriminator for axis-split BIBFRAME classes.

    For each ``bf:*`` class in :data:`AXIS_DEFAULT_CLASSES`, inspects
    every typed subject and routes to:

      - the **Work-axis** variant if the subject also carries any of
        :data:`_WORK_AXIS_SIGNALS` as another ``rdf:type`` — i.e. it's
        the Work URI marc2bibframe2 emitted (typed ``bf:Work`` →
        renamed to ``bffi:BibframeWork``), or a Hub URI that
        :func:`route_hubs` already routed to ``bffi:Work`` /
        ``bffi:AggregatingWork`` / ``bffi:Arrangement``.
      - the **Expression-axis** variant otherwise — Instance URIs
        (which marc2bibframe2 also tags with the content-type class
        but doesn't co-type as ``bf:Work``) plus the fallback for any
        subject without a clear axis signal.

    Returns a counter dict split by axis so the observability summary
    can show the discriminator's effect:

        {"axis_default_class_work":       <n>,
         "axis_default_class_expression": <n>}
    """
    work_count = 0
    expr_count = 0
    for bf_class, (work_pick, expr_pick) in AXIS_DEFAULT_CLASSES.items():
        for subject in list(graph.subjects(RDF.type, bf_class)):
            co_types = set(graph.objects(subject, RDF.type)) - {bf_class}
            if co_types & _WORK_AXIS_SIGNALS:
                pick = work_pick
                work_count += 1
            else:
                pick = expr_pick
                expr_count += 1
            graph.remove((subject, RDF.type, bf_class))
            graph.add((subject, RDF.type, pick))
    return {
        "axis_default_class_work": work_count,
        "axis_default_class_expression": expr_count,
    }


# --- routing 7: axis-default predicate rewrites -------------------------


def drop_undeclared_bf_terms(graph: Graph, ontology: BibframeOntology | None = None) -> int:
    """Drop every triple referencing a ``bf:*`` URI not declared in the
    vendored BIBFRAME ontology.

    The guard set is the union of the ontology's classes, object
    properties, and datatype properties — i.e. every URI BIBFRAME
    formally declares. A ``bf:*`` URI appearing in the subject,
    predicate, or object slot of a triple that isn't in that set is an
    upstream artifact (typically marc2bibframe2 emitting a term
    BIBFRAME itself doesn't recognise — e.g. ``bf:Statement`` carrying
    a flat-text publisher statement redundant with a sibling
    structured ``bf:ProvisionActivity`` block).

    The whole triple is removed when any of its three slots references
    an undeclared ``bf:*`` URI. Returns the count of triples dropped so
    the observability sidecar can surface the artifact rate per run.

    Pass ``ontology`` explicitly in tests; production callers leave it
    ``None`` to use :func:`load_ontology`'s cached vendored vocab.

    Runs LAST in :func:`apply_all_routings` so legitimate ``bf:*`` URIs
    that earlier routings consumed are out of the graph before this
    check. A non-zero count after this routing means a real
    marc2bibframe2 artifact, not a routing oversight.
    """
    if ontology is None:
        ontology = load_ontology()
    known = ontology.classes | ontology.object_properties | ontology.datatype_properties

    dropped = 0
    for s, p, o in list(graph):
        for node in (s, p, o):
            if isinstance(node, URIRef) and str(node).startswith(str(BF)) and node not in known:
                graph.remove((s, p, o))
                dropped += 1
                break
    return dropped


#: marc2bibframe2 attaches ``bf:provisionActivityStatement`` to related-Instance
#: hubs from MARC 76X-78X linking-entry fields (760 main series, 762 has
#: subseries, 765 original language, 767 translation, 770/772 supplements,
#: 773 host-item, 774 constituent, 775 other edition, 776 additional
#: physical form, 777 issued with, 780 preceding entry, 785 succeeding
#: entry, 786 data source, 787 other relationship). The Instance URI's
#: fragment carries the MARC tag (``…#Instance780-25``), giving us a
#: structural discriminator analogous to Hub routing's marcKey check.
_PROVISION_STATEMENT_SUCCESSION_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"#Instance(76[0-9]|77[0-9]|78[0-9])-"
)


def route_provision_activity_statement(graph: Graph) -> dict[str, int]:
    """``bf:provisionActivityStatement`` → ``bffi:date`` (when on a 76X-78X
    related-Instance hub) or wrapped in a ``bffi:Note`` bnode (otherwise).

    BFFI has no ``bffi:provisionActivityStatement`` equivalent in
    `lkd.rdf`. The corpus shape on Helmet (102 occurrences in the 20 k
    bench, all on related-Instance hubs from MARC 78X succession
    fields) is **date ranges** — ``"1980-1981"``, ``"1909-1993"``, etc.
    The URI-fragment discriminator confirms the succession-link
    context; in that case we route to ``bffi:date`` as a plain string
    literal (no EDTF datatype claim — content isn't always
    EDTF-conformant, e.g. ``"(1990-2013), ISSN"``).

    If the Instance URI doesn't match the succession-link pattern, we
    fall back to wrapping the literal in a ``bffi:Note`` bnode
    (``?inst bffi:note [a bffi:Note ; rdfs:label "text"]``). This is
    the generic carrier that preserves the text without asserting a
    semantic interpretation.

    Returns counts for both targets so the operator can see the
    discriminator split in the observability summary.
    """
    routed_to_date = 0
    routed_to_note = 0
    for s, _, o in list(graph.triples((None, BF.provisionActivityStatement, None))):
        graph.remove((s, BF.provisionActivityStatement, o))
        if isinstance(s, URIRef) and _PROVISION_STATEMENT_SUCCESSION_LINK_PATTERN.search(str(s)):
            graph.add((s, BFFI.date, o))
            routed_to_date += 1
        else:
            note_bnode = BNode()
            graph.add((s, BFFI.note, note_bnode))
            graph.add((note_bnode, RDF.type, BFFI.Note))
            graph.add((note_bnode, RDFS.label, o))
            routed_to_note += 1
    return {
        "provision_statement_to_date": routed_to_date,
        "provision_statement_to_note": routed_to_note,
    }


def _expression_axis(types: set[Node]) -> bool:
    """Helper: does ``types`` contain any Expression-axis BFFI signal?"""
    return any(t in _EXPRESSION_AXIS_SIGNALS for t in types)


def route_axis_default_predicates(graph: Graph) -> dict[str, int]:
    """Per-statement axis discriminator for the broadMatch predicates
    ``bf:instanceOf`` / ``bf:hasInstance`` / ``bf:issuance``.

    Each statement is routed individually based on the rdf:type
    assertions on the discriminator-side node — see
    :data:`AXIS_DEFAULT_PREDICATES`. The signal direction differs per
    predicate:

      - ``bf:instanceOf`` (Manifestation → Work/Expression): the
        OBJECT carries the axis signal.
      - ``bf:hasInstance`` (Work/Expression → Manifestation): the
        SUBJECT carries the axis signal.
      - ``bf:issuance``: flat rename to ``bffi:issuance``. The
        ``lkd.rdf`` peer ``bffi:extensionPlan`` reads as an
        "alternative" only on first glance — it's actually a
        separate concept, linked to RDA's ``RDAExtensionPlan`` term
        list (m5119: "Will not be extended" / "Has no plan to be
        extended" / …) on the Work side, while ``bffi:issuance``
        links to RDA's ``ModeIssue`` (m4372: serial / monograph /
        integrating resource / multipart). Both happen to carry
        ``bffi-meta:broadMatch bf:issuance`` but they are not
        interchangeable on a single triple. The object URI of a
        ``bf:issuance`` statement (``<…/issuance/{serl,mono,intg,mulu}>``)
        is a meaningful signal — but it discriminates between codes
        *inside* the ``ModeIssue`` vocabulary, all of which map
        cleanly to ``bffi:issuance``.

    Returns a counter dict split per predicate-and-axis so the
    observability summary surfaces the discriminator's per-direction
    effect.
    """
    counters = {
        "instance_of_work": 0,
        "instance_of_expression": 0,
        "has_instance_of_work": 0,
        "has_instance_of_expression": 0,
        "issuance": 0,
    }

    # bf:instanceOf — discriminate by object's type.
    work_pred, expr_pred = AXIS_DEFAULT_PREDICATES[BF.instanceOf]
    for s, _, o in list(graph.triples((None, BF.instanceOf, None))):
        graph.remove((s, BF.instanceOf, o))
        object_types = set(graph.objects(o, RDF.type))
        if _expression_axis(object_types):
            graph.add((s, expr_pred, o))
            counters["instance_of_expression"] += 1
        else:
            graph.add((s, work_pred, o))
            counters["instance_of_work"] += 1

    # bf:hasInstance — discriminate by subject's type.
    work_pred, expr_pred = AXIS_DEFAULT_PREDICATES[BF.hasInstance]
    for s, _, o in list(graph.triples((None, BF.hasInstance, None))):
        graph.remove((s, BF.hasInstance, o))
        subject_types = set(graph.objects(s, RDF.type))
        if _expression_axis(subject_types):
            graph.add((s, expr_pred, o))
            counters["has_instance_of_expression"] += 1
        else:
            graph.add((s, work_pred, o))
            counters["has_instance_of_work"] += 1

    # bf:issuance — flat rename (both tuple slots equal bffi:issuance).
    flat_pred, _ = AXIS_DEFAULT_PREDICATES[BF.issuance]
    for s, _, o in list(graph.triples((None, BF.issuance, None))):
        graph.remove((s, BF.issuance, o))
        graph.add((s, flat_pred, o))
        counters["issuance"] += 1

    return counters


# --- top-level entry point ----------------------------------------------


def apply_all_routings(graph: Graph) -> dict[str, int]:
    """Apply every Phase 4 routing in dependency order.

    Order matters: the ``bflc:marcKey`` rename has to happen *before*
    Hub routing, because the Hub discriminator reads ``bffi:marcKey``
    (the post-rename name). Identifier / Title / Audio / Series-link
    are independent and can run in any order.

    Returns a per-routing counter dict suitable for inclusion in the
    observability ``end`` event.
    """
    counters: dict[str, int] = {
        "bflc_marckey_renamed": rename_bflc_marckey(graph),
        "identifier_scheme": route_identifier_schemes(graph),
        "title_variant": route_title_variants(graph),
        "series_link": route_series_links(graph),
        "relation_predicate": route_relation_predicates(graph),
        "hub": route_hubs(graph),
    }
    # The remaining three routings each split their counters into
    # per-discriminator buckets so the observability summary surfaces
    # the pick distribution per axis / direction.
    counters.update(route_axis_default_predicates(graph))
    counters.update(route_axis_default_classes(graph))
    counters.update(route_provision_activity_statement(graph))
    # Runs LAST. By the time we get here, every legitimate bf:* URI
    # has either been renamed (clean-rename pass) or routed
    # (Phase 4 / axis defaults / provision-statement). What survives
    # is either undeclared in BIBFRAME (artifact — drop) or declared
    # but unrouted (residue — leave for observability to surface).
    counters["dropped_undeclared_bf"] = drop_undeclared_bf_terms(graph)
    return counters
