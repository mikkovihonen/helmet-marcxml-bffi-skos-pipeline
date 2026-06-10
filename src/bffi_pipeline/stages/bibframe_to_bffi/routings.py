"""P-56 Phase 4: per-instance discriminator routings.

The clean-rename pass in :mod:`bffi_pipeline.stages.bibframe_to_bffi.mappings`
handles every ``bf:*`` term with a direct ``owl:equivalentClass`` /
``owl:equivalentProperty`` to ``bffi:*``. Phase 4 covers the residue:
classes and predicates that don't have a single direct counterpart but
*do* have a canonical routing in `docs/bf_to_bffi_mapping.md`.

Five routings ship in this module, in the order documented in the
mapping doc:

1. **Identifier-scheme** — ``bf:Isbn`` / ``bf:Issn`` / ``bf:Ean`` /
   ``bf:AudioIssueNumber`` / ``bf:Lccn`` / ``bf:IssnL`` / ``bf:OtherIdentifier``
   → ``bffi:Identifier`` + ``bffi:source <loc-scheme-URI>``.
2. **Title-variant** — ``bf:VariantTitle`` / ``bf:ParallelTitle`` /
   ``bf:KeyTitle`` / ``bf:CollectiveTitle`` → ``bffi:Title``. The
   ``bffi:marcKey`` discriminator is preserved by the
   ``bflc:marcKey`` → ``bffi:marcKey`` rename below.
3. **Audio content-type** — ``bf:Audio`` → ``bffi:NonMusicAudioExpression``
   (Expression-axis default per the doc).
4. **Series-link** — ``bf:hasSeries`` → ``bffi:relation`` ⇒ structured
   ``bffi:Relation`` bnode with ``bffi:relationship
   <vocabulary/relationship/series>`` + ``bffi:associatedResource``.
5. **Hub** — ``bf:Hub`` → ``bffi:Work`` or ``bffi:Expression`` (or a leaf
   subclass) based on the ``bflc:marcKey`` content.

Plus one prerequisite rename:

- ``bflc:marcKey`` → ``bffi:marcKey`` (`owl:equivalentProperty` per
  ``lkd.rdf``; emit-side closes to ``bffi:`` for downstream consumers).

Each routing is a single graph-mutation function returning the number
of patterns it rewrote. :func:`apply_all_routings` runs them in order
and returns the per-routing counter dict so the runner can surface them
in the observability ``end`` event.

Out of scope for v0 — flagged in the mapping doc but deferred to a
follow-on:

- Hub routing currently picks the *type* per marcKey signals but does
  NOT also attach the optional facet predicates (``bffi:languageOfExpression``,
  ``bffi:musicKey``, ``bffi:version``). Those are nice-to-have signal
  promotions; the type rewrite is what unblocks closed-namespace
  discipline.
- Audio routing defaults to the Expression axis without per-record
  content-type inspection. A follow-on can split by axis when the BFFI
  graph already carries content-type evidence.
"""

from __future__ import annotations

from typing import Final

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

#: BIBFRAME namespace — the input side. Routings remove triples that
#: still carry these URIs after the clean-rename pass.
BF: Final[Namespace] = Namespace("http://id.loc.gov/ontologies/bibframe/")

#: BFLC (LoC) extension namespace — carries ``marcKey`` at the BIBFRAME
#: side; we close it to ``bffi:`` for downstream consumers.
BFLC: Final[Namespace] = Namespace("http://id.loc.gov/ontologies/bflc/")

#: BFFI emit namespace.
BFFI: Final[Namespace] = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi:")

#: Per the mapping doc's Identifier-scheme routing. The class slot becomes
#: ``bffi:Identifier``; the scheme moves into ``bffi:source <…>`` as a
#: LoC-namespaced URI. Keeping these in one closed map lets the same
#: vocabulary file extend with new schemes without touching the loop.
LOC_IDENTIFIER_SCHEMES: Final[dict[URIRef, URIRef]] = {
    BF.Isbn: URIRef("http://id.loc.gov/vocabulary/identifiers/isbn"),
    BF.Issn: URIRef("http://id.loc.gov/vocabulary/identifiers/issn"),
    BF.IssnL: URIRef("http://id.loc.gov/vocabulary/identifiers/issn-l"),
    BF.Ean: URIRef("http://id.loc.gov/vocabulary/identifiers/ean"),
    BF.AudioIssueNumber: URIRef("http://id.loc.gov/vocabulary/identifiers/audio-issue-number"),
    BF.Lccn: URIRef("http://id.loc.gov/vocabulary/identifiers/lccn"),
    BF.OtherIdentifier: URIRef("http://id.loc.gov/vocabulary/identifiers/other"),
}

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


def route_identifier_schemes(graph: Graph) -> int:
    """``bf:Isbn`` / ``bf:Issn`` / etc. → ``bffi:Identifier`` + ``bffi:source``.

    The mapping doc's Identifier-scheme routing: the BIBFRAME identifier
    subclass collapses into the ``bffi:Identifier`` anchor with the
    scheme encoded as a LoC-vocabulary URI on ``bffi:source``. The
    ``rdf:value`` carrying the actual identifier text is left untouched.

    Returns the count of identifier blocks rewritten across all schemes.
    """
    rewritten = 0
    for bf_class, loc_scheme in LOC_IDENTIFIER_SCHEMES.items():
        for subject in list(graph.subjects(RDF.type, bf_class)):
            graph.remove((subject, RDF.type, bf_class))
            graph.add((subject, RDF.type, BFFI.Identifier))
            graph.add((subject, BFFI.source, loc_scheme))
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


# --- routing 3: Audio content-type --------------------------------------


def route_audio(graph: Graph) -> int:
    """``bf:Audio`` → ``bffi:NonMusicAudioExpression``.

    marc2bibframe2 emits ``bf:Audio`` only for non-music audio (music
    gets the more specific ``bf:MusicAudio``); BFFI splits the result
    into Work-axis vs Expression-axis variants. v0 defaults to the
    Expression axis per the mapping doc's recommendation; per-record
    axis discrimination is a follow-on.
    """
    rewritten = 0
    for subject in list(graph.subjects(RDF.type, BF.Audio)):
        graph.remove((subject, RDF.type, BF.Audio))
        graph.add((subject, RDF.type, BFFI.NonMusicAudioExpression))
        rewritten += 1
    return rewritten


# --- routing 4: Series-link ---------------------------------------------


def route_series_links(graph: Graph) -> int:
    """``?m bf:hasSeries ?s`` → structured ``bffi:relation`` chain.

    The mapping doc's Series-link routing: a fresh ``bffi:Relation``
    bnode carries ``bffi:relationship <…/relationship/series>`` +
    ``bffi:associatedResource ?s``. ``?m bffi:relation [relation-bnode]``
    threads it back onto the Manifestation.
    """
    rewritten = 0
    for m, _, s in list(graph.triples((None, BF.hasSeries, None))):
        graph.remove((m, BF.hasSeries, s))
        rel_bnode = BNode()
        graph.add((m, BFFI.relation, rel_bnode))
        graph.add((rel_bnode, RDF.type, BFFI.Relation))
        graph.add((rel_bnode, BFFI.relationship, SERIES_RELATIONSHIP))
        graph.add((rel_bnode, BFFI.associatedResource, s))
        rewritten += 1
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
    return {
        "bflc_marckey_renamed": rename_bflc_marckey(graph),
        "identifier_scheme": route_identifier_schemes(graph),
        "title_variant": route_title_variants(graph),
        "audio": route_audio(graph),
        "series_link": route_series_links(graph),
        "hub": route_hubs(graph),
    }
