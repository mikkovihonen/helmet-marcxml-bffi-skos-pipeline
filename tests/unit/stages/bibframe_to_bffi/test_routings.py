"""Unit tests for the P-56 Phase 4 discriminator routings."""

from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.stages.bibframe_to_bffi.routings import (
    AXIS_DEFAULT_CLASSES,
    AXIS_DEFAULT_PREDICATES,
    BF,
    BFFI,
    BFLC,
    LOC_IDENTIFIER_SCHEMES,
    SERIES_RELATIONSHIP,
    TITLE_VARIANT_CLASSES,
    _hub_target_type,
    apply_all_routings,
    rename_bflc_marckey,
    route_audio,
    route_axis_default_classes,
    route_axis_default_predicates,
    route_hubs,
    route_identifier_schemes,
    route_series_links,
    route_title_variants,
)

# --- bflc:marcKey rename ------------------------------------------------


def test_rename_bflc_marckey_rewrites_predicate_and_keeps_literal() -> None:
    g = Graph()
    s = URIRef("http://example.org/m")
    g.add((s, BFLC.marcKey, Literal("24500$aA Title")))
    rewritten = rename_bflc_marckey(g)
    assert rewritten == 1
    assert (s, BFLC.marcKey, Literal("24500$aA Title")) not in g
    assert (s, BFFI.marcKey, Literal("24500$aA Title")) in g


# --- Identifier-scheme routing -----------------------------------------


def test_route_identifier_schemes_rewrites_each_loc_class() -> None:
    g = Graph()
    isbn_node = URIRef("http://example.org/isbn-1")
    issn_node = URIRef("http://example.org/issn-1")
    g.add((isbn_node, RDF.type, BF.Isbn))
    g.add((isbn_node, RDF.value, Literal("9780123456789")))
    g.add((issn_node, RDF.type, BF.Issn))

    rewritten = route_identifier_schemes(g)
    assert rewritten == 2

    # Both nodes now type as bffi:Identifier.
    assert (isbn_node, RDF.type, BFFI.Identifier) in g
    assert (issn_node, RDF.type, BFFI.Identifier) in g

    # And carry the LoC scheme URI via bffi:source.
    assert (isbn_node, BFFI.source, LOC_IDENTIFIER_SCHEMES[BF.Isbn]) in g
    assert (issn_node, BFFI.source, LOC_IDENTIFIER_SCHEMES[BF.Issn]) in g

    # rdf:value passes through untouched.
    assert (isbn_node, RDF.value, Literal("9780123456789")) in g

    # The bf:* typing triples are gone.
    assert (isbn_node, RDF.type, BF.Isbn) not in g
    assert (issn_node, RDF.type, BF.Issn) not in g


def test_route_identifier_schemes_skips_already_renamed_block() -> None:
    """A graph that already has only bffi:Identifier (no bf:Isbn) is a no-op."""
    g = Graph()
    s = URIRef("http://example.org/ident")
    g.add((s, RDF.type, BFFI.Identifier))
    g.add((s, BFFI.source, LOC_IDENTIFIER_SCHEMES[BF.Isbn]))
    assert route_identifier_schemes(g) == 0


# --- Title-variant routing ---------------------------------------------


def test_route_title_variants_collapses_subclasses_to_bffi_title() -> None:
    g = Graph()
    for i, bf_class in enumerate(TITLE_VARIANT_CLASSES):
        node = URIRef(f"http://example.org/t-{i}")
        g.add((node, RDF.type, bf_class))

    rewritten = route_title_variants(g)
    assert rewritten == len(TITLE_VARIANT_CLASSES)
    # All four type slots collapse to bffi:Title.
    for i, bf_class in enumerate(TITLE_VARIANT_CLASSES):
        node = URIRef(f"http://example.org/t-{i}")
        assert (node, RDF.type, BFFI.Title) in g
        assert (node, RDF.type, bf_class) not in g


# --- Audio routing ------------------------------------------------------


def test_route_audio_rewrites_to_nonmusic_audio_expression() -> None:
    g = Graph()
    audio = URIRef("http://example.org/audio")
    g.add((audio, RDF.type, BF.Audio))
    rewritten = route_audio(g)
    assert rewritten == 1
    assert (audio, RDF.type, BFFI.NonMusicAudioExpression) in g
    assert (audio, RDF.type, BF.Audio) not in g


# --- Series-link routing -----------------------------------------------


def test_route_series_links_emits_structured_relation_bnode() -> None:
    g = Graph()
    m = URIRef("http://example.org/manifestation")
    series = URIRef("http://example.org/series-1")
    g.add((m, BF.hasSeries, series))

    rewritten = route_series_links(g)
    assert rewritten == 1

    # bf:hasSeries triple is gone.
    assert (m, BF.hasSeries, series) not in g

    # Manifestation now has a bffi:relation to a fresh Relation bnode.
    rel_objs = list(g.objects(m, BFFI.relation))
    assert len(rel_objs) == 1
    rel = rel_objs[0]

    # Relation bnode carries the series relationship and points at series.
    assert (rel, RDF.type, BFFI.Relation) in g
    assert (rel, BFFI.relationship, SERIES_RELATIONSHIP) in g
    assert (rel, BFFI.associatedResource, series) in g


# --- Hub routing --------------------------------------------------------


def test_hub_target_type_marckey_dispatch_table() -> None:
    """Spot-check the routing-table decisions from the mapping doc."""
    # Default: no marcKey → Work.
    assert _hub_target_type("") == BFFI.Work
    # $o arrangement → Arrangement.
    assert _hub_target_type("24000$aFoo$oarrangement") == BFFI.Arrangement
    # $l language → Expression.
    assert _hub_target_type("73002$aFoo$lenglanti") == BFFI.Expression
    # $r key → Expression.
    assert _hub_target_type("24000$aFoo$rD major") == BFFI.Expression
    # 100 + $t (author-attributed Work).
    assert _hub_target_type("1001 $aBach$tBrandenburg concertos") == BFFI.Work
    # 130 series → SeriesExpression (axis default).
    assert _hub_target_type("13000$aSeries title") == BFFI.SeriesExpression
    # 830 series → SeriesExpression.
    assert _hub_target_type("83000$aSeries title") == BFFI.SeriesExpression
    # 730 plain (no Expression signal) → Work.
    assert _hub_target_type("73002$aPlain transcribed title") == BFFI.Work
    # 740 plain → Work.
    assert _hub_target_type("74002$aAnother") == BFFI.Work


def test_route_hubs_picks_type_from_marckey() -> None:
    g = Graph()
    hub = URIRef("http://example.org/hub")
    g.add((hub, RDF.type, BF.Hub))
    g.add((hub, BFFI.marcKey, Literal("73002$aSymphonie no. 5$lenglanti")))

    rewritten = route_hubs(g)
    assert rewritten == 1
    # $l in the marcKey routes the Hub to Expression.
    assert (hub, RDF.type, BFFI.Expression) in g
    assert (hub, RDF.type, BF.Hub) not in g


def test_route_hubs_defaults_to_work_when_marckey_absent() -> None:
    g = Graph()
    hub = URIRef("http://example.org/hub")
    g.add((hub, RDF.type, BF.Hub))
    rewritten = route_hubs(g)
    assert rewritten == 1
    assert (hub, RDF.type, BFFI.Work) in g


# --- apply_all_routings -------------------------------------------------


def test_apply_all_routings_returns_per_routing_counts() -> None:
    """End-to-end on a small synthetic graph: every routing fires at least
    once, the counter dict shape matches the documented keys."""
    g = Graph()
    # Identifier
    isbn = URIRef("http://example.org/isbn")
    g.add((isbn, RDF.type, BF.Isbn))
    # Title variant
    vt = URIRef("http://example.org/vt")
    g.add((vt, RDF.type, BF.VariantTitle))
    # Audio
    au = URIRef("http://example.org/audio")
    g.add((au, RDF.type, BF.Audio))
    # Series link
    m = URIRef("http://example.org/m")
    s = URIRef("http://example.org/s")
    g.add((m, BF.hasSeries, s))
    # Hub (with a marcKey that needs the bflc rename first)
    hub = URIRef("http://example.org/hub")
    g.add((hub, RDF.type, BF.Hub))
    g.add((hub, BFLC.marcKey, Literal("73002$aFoo")))

    counters = apply_all_routings(g)

    assert counters == {
        "bflc_marckey_renamed": 1,
        "identifier_scheme": 1,
        "title_variant": 1,
        "audio": 1,
        "series_link": 1,
        "hub": 1,
        "axis_default_class": 0,
        "axis_default_predicate": 0,
    }

    # The Hub routing reads bffi:marcKey (after rename), so the rename
    # must have run first — Hub's chosen type is Work since the marcKey
    # is a plain 730 without Expression-level signals.
    assert (hub, RDF.type, BFFI.Work) in g


# --- routing 6 (axis-default classes) -----------------------------------


def test_route_axis_default_classes_picks_expression_variant_for_each() -> None:
    """Each ``bf:Monograph`` / ``bf:Series`` / ``bf:MusicAudio`` / ``…`` is
    rewritten to its Expression-axis BFFI counterpart per
    :data:`AXIS_DEFAULT_CLASSES`."""
    g = Graph()
    for i, bf_class in enumerate(AXIS_DEFAULT_CLASSES):
        node = URIRef(f"http://example.org/c-{i}")
        g.add((node, RDF.type, bf_class))
    rewritten = route_axis_default_classes(g)
    assert rewritten == len(AXIS_DEFAULT_CLASSES)
    for i, (bf_class, bffi_class) in enumerate(AXIS_DEFAULT_CLASSES.items()):
        node = URIRef(f"http://example.org/c-{i}")
        assert (node, RDF.type, bffi_class) in g
        assert (node, RDF.type, bf_class) not in g


def test_route_axis_default_classes_no_op_when_already_routed() -> None:
    g = Graph()
    s = URIRef("http://example.org/s")
    g.add((s, RDF.type, BFFI.SeriesExpression))
    assert route_axis_default_classes(g) == 0


# --- routing 7 (axis-default predicates) --------------------------------


def test_route_axis_default_predicates_rewrites_each_predicate() -> None:
    """``bf:instanceOf`` / ``bf:hasInstance`` / ``bf:issuance`` rewrite to
    their default ``bffi:*`` counterparts per :data:`AXIS_DEFAULT_PREDICATES`."""
    g = Graph()
    for i, bf_pred in enumerate(AXIS_DEFAULT_PREDICATES):
        s = URIRef(f"http://example.org/s-{i}")
        o = URIRef(f"http://example.org/o-{i}")
        g.add((s, bf_pred, o))
    rewritten = route_axis_default_predicates(g)
    assert rewritten == len(AXIS_DEFAULT_PREDICATES)
    for i, (bf_pred, bffi_pred) in enumerate(AXIS_DEFAULT_PREDICATES.items()):
        s = URIRef(f"http://example.org/s-{i}")
        o = URIRef(f"http://example.org/o-{i}")
        assert (s, bffi_pred, o) in g
        assert (s, bf_pred, o) not in g
