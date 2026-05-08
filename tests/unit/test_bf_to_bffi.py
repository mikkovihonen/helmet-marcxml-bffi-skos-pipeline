"""Unit tests for stages/bf_to_bffi.

Hand-craft small BIBFRAME graphs and verify the CONSTRUCT pair routes
properties to the right side (Work vs Expression), preserves the Helmet
identifier, links Expression to Work via bffi:expressionOf, and handles
the language-tag retag on skos:prefLabel.
"""

from __future__ import annotations

import textwrap

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.bf_to_bffi import construct_bffi, post_process
from bffi_pipeline.uris import mint_raw_expression_uri, mint_raw_work_uri

BF_WORK = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work"
EXPECTED_WORK = URIRef(mint_raw_work_uri(BF_WORK))
EXPECTED_EXPR = URIRef(mint_raw_expression_uri(BF_WORK))

# A minimal BIBFRAME graph mimicking marc2bibframe2 v3.1.0 output for a
# Tolstoy translation. Two contributions: PrimaryContribution (Tolstoy)
# and a non-primary (translator).
SOURCE_TTL = textwrap.dedent(
    f"""
    @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
    @prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    <{BF_WORK}> a bf:Work ;
        bf:title [ a bf:Title ; bf:mainTitle "Sota ja rauha" ] ;
        bf:language <http://id.loc.gov/vocabulary/languages/fin> ;
        bf:originDate "2023" ;
        bf:contribution <#contrib-primary> ;
        bf:contribution <#contrib-translator> ;
        bf:identifiedBy <#helmet-id> ;
        bf:summary "Russian historical novel." ;
        bf:note "Translated by Esa Adrian." .

    <#contrib-primary> a bf:Contribution, bf:PrimaryContribution ;
        bf:agent <urn:agent/Tolstoy> .

    <#contrib-translator> a bf:Contribution ;
        bf:agent <urn:agent/Adrian> .

    <#helmet-id> a bf:Local ;
        rdf:value "10000001" ;
        bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> .
    """
).strip()


def _build_source() -> Graph:
    g = Graph()
    g.parse(data=SOURCE_TTL, format="turtle")
    return g


def test_construct_mints_paired_work_and_expression_uris() -> None:
    bffi = construct_bffi(_build_source())
    works = set(bffi.subjects(RDF.type, V.BFFI.Work))
    exprs = set(bffi.subjects(RDF.type, V.BFFI.Expression))
    assert works == {EXPECTED_WORK}
    assert exprs == {EXPECTED_EXPR}


def test_expression_links_back_to_work() -> None:
    bffi = construct_bffi(_build_source())
    assert (EXPECTED_EXPR, V.BFFI.expressionOf, EXPECTED_WORK) in bffi
    assert (EXPECTED_WORK, V.BFFI.hasExpression, EXPECTED_EXPR) in bffi


def test_primary_contribution_routed_to_work() -> None:
    bffi = construct_bffi(_build_source())
    contributions_on_work = list(bffi.objects(EXPECTED_WORK, V.BFFI.contribution))
    assert len(contributions_on_work) == 1
    [contrib] = contributions_on_work
    types = set(bffi.objects(contrib, RDF.type))
    assert V.BFFI.PrimaryContribution in types
    agents = set(bffi.objects(contrib, V.BFFI.agent))
    assert URIRef("urn:agent/Tolstoy") in agents


def test_non_primary_contribution_routed_to_expression() -> None:
    bffi = construct_bffi(_build_source())
    contributions_on_expr = list(bffi.objects(EXPECTED_EXPR, V.BFFI.contribution))
    assert len(contributions_on_expr) == 1
    [contrib] = contributions_on_expr
    types = set(bffi.objects(contrib, RDF.type))
    assert V.BFFI.PrimaryContribution not in types
    agents = set(bffi.objects(contrib, V.BFFI.agent))
    assert URIRef("urn:agent/Adrian") in agents


def test_language_routed_to_expression_only() -> None:
    bffi = construct_bffi(_build_source())
    assert (
        EXPECTED_EXPR,
        V.BFFI.language,
        URIRef("http://id.loc.gov/vocabulary/languages/fin"),
    ) in bffi
    assert not list(bffi.objects(EXPECTED_WORK, V.BFFI.language))


def test_origin_date_routed_to_work_only() -> None:
    bffi = construct_bffi(_build_source())
    work_dates = set(bffi.objects(EXPECTED_WORK, V.BFFI.originDate))
    assert work_dates == {Literal("2023")}
    assert not list(bffi.objects(EXPECTED_EXPR, V.BFFI.originDate))


def test_summary_and_note_routed_to_expression() -> None:
    bffi = construct_bffi(_build_source())
    assert any(bffi.objects(EXPECTED_EXPR, V.BFFI.summary))
    assert any(bffi.objects(EXPECTED_EXPR, V.BFFI.note))
    assert not list(bffi.objects(EXPECTED_WORK, V.BFFI.summary))


def test_helmet_identifier_preserved_on_both_sides() -> None:
    bffi = construct_bffi(_build_source())
    helmet = URIRef("http://urn.fi/URN:NBN:fi:bib:source:helmet")
    for target in (EXPECTED_WORK, EXPECTED_EXPR):
        idents = list(bffi.objects(target, V.BF.identifiedBy))
        assert len(idents) == 1, f"missing Helmet identifier on {target}"
        ident = idents[0]
        assert (ident, V.BF.source, helmet) in bffi
        assert (ident, RDF.value, Literal("10000001")) in bffi


def test_post_process_tags_pref_labels_with_language() -> None:
    source = _build_source()
    bffi = construct_bffi(source)
    post_process(bffi, source)
    work_label = next(bffi.objects(EXPECTED_WORK, V.SKOS.prefLabel))
    expr_label = next(bffi.objects(EXPECTED_EXPR, V.SKOS.prefLabel))
    assert isinstance(work_label, Literal)
    assert isinstance(expr_label, Literal)
    assert work_label.language == "fi"
    assert expr_label.language == "fi"
    assert str(work_label) == "Sota ja rauha"


def test_pref_label_untagged_when_language_not_in_priority_set() -> None:
    """A French original would leave prefLabel untagged (fr is not in fi/sv/en)."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
            <{BF_WORK}> a bf:Work ;
                bf:title [ bf:mainTitle "Étranger" ] ;
                bf:language <http://id.loc.gov/vocabulary/languages/fre> .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    post_process(bffi, source)
    label = next(bffi.objects(EXPECTED_WORK, V.SKOS.prefLabel))
    assert isinstance(label, Literal)
    assert label.language is None
