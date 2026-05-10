"""Unit tests for stages/bf_to_bffi.

Hand-craft small BIBFRAME graphs and verify the CONSTRUCT pair routes
properties to the right side (Work vs Expression), preserves the Helmet
identifier, links Expression to Work via bffi:expressionOf, and handles
the language-tag retag on skos:prefLabel.
"""

from __future__ import annotations

import textwrap

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from bffi_pipeline.contrib_extract_llm import (
    ContribCandidate,
    ContribExtractDecision,
    StubContribExtractor,
)
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


def test_pref_label_picks_main_work_language_not_translated_from() -> None:
    """marc2bibframe2 emits a `Note otx` sub-node whose `bf:language` carries
    the *original* language (MARC 041 $h "translated from"). The main Work's
    own `bf:language` is what describes the Expression's text. The post-process
    must tag prefLabels from the main Work, not the otx sub-node."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix bflc: <http://id.loc.gov/ontologies/bflc/> .
            @prefix res:  <http://id.loc.gov/vocabulary/resourceComponents/> .

            <{BF_WORK}> a bf:Work ;
                bf:title    [ bf:mainTitle "Kellontekijän tytär" ] ;
                bf:language <http://id.loc.gov/vocabulary/languages/fin> ;
                bf:note     [ a bf:Note, res:otx ;
                              bf:language <http://id.loc.gov/vocabulary/languages/eng> ] .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    post_process(bffi, source)
    label = next(bffi.objects(EXPECTED_WORK, V.SKOS.prefLabel))
    assert isinstance(label, Literal)
    # Without the main-Work filter, set iteration could pick `eng` and tag
    # this Finnish title as `@en`. With the filter + fi>sv>en priority, fi.
    assert label.language == "fi"


def test_pref_label_picks_main_work_language_not_contained_work() -> None:
    """Aggregate records (MARC 700 ind2=2) reference contained Works via
    `bf:associatedResource`; those contained Works often have their own
    `bf:language`. Tagging must ignore contained Works' languages."""
    contained = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work700-30"
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .

            <{BF_WORK}> a bf:Work ;
                bf:title    [ bf:mainTitle "Sagor från Mumindalen" ] ;
                bf:language <http://id.loc.gov/vocabulary/languages/swe> ;
                bf:associatedResource <{contained}> .

            <{contained}> a bf:Work ;
                bf:title    [ bf:mainTitle "The English original" ] ;
                bf:language <http://id.loc.gov/vocabulary/languages/eng> .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    post_process(bffi, source)
    label = next(bffi.objects(EXPECTED_WORK, V.SKOS.prefLabel))
    assert isinstance(label, Literal)
    assert label.language == "sv"


def test_post_process_emits_sierra_style_dct_identifier_on_work_and_expression() -> None:
    """Skosmos can't traverse the structured ``bf:Local`` blank node. M3
    post-processing denormalises the Helmet bib ID onto every Work and
    Expression as a flat ``dct:identifier`` literal so cataloguers can
    copy a Sierra-style bib number ("b<id>0") straight from the concept
    page into discussions. The bare numeric ID for this fixture is
    "10000001"; in Sierra-style display form that's "b100000010" — the
    trailing "0" stands in for the (undocumented) modulus-11 check
    digit, which the Helmet OPAC accepts in lookups."""
    source = _build_source()
    bffi = construct_bffi(source)
    post_process(bffi, source)
    for target in (EXPECTED_WORK, EXPECTED_EXPR):
        idents = list(bffi.objects(target, DCTERMS.identifier))
        assert Literal("b100000010") in idents, f"Sierra-style id missing on {target}"


def test_post_process_does_not_emit_dct_identifier_for_non_helmet_sources() -> None:
    """A ``bf:identifiedBy`` triple from a non-Helmet source must not produce a
    Sierra-style ``dct:identifier`` — that form is Helmet/Sierra-specific."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:  <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

            <{BF_WORK}> a bf:Work ;
                bf:title         [ bf:mainTitle "Untitled" ] ;
                bf:identifiedBy  <#other-id> .

            <#other-id> a bf:Local ;
                rdf:value "FOREIGN-42" ;
                bf:source <http://example.org/source/external> .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    post_process(bffi, source)
    assert not list(bffi.objects(EXPECTED_WORK, DCTERMS.identifier))


def test_pref_label_picks_language_via_lingua_when_multiple_candidates() -> None:
    """When the main Work declares multiple languages, the per-segment
    Lingua detector picks the language whose model fits the text best.
    'Sota ja rauha' is unambiguously Finnish; 'War and Peace' is
    English; both should tag correctly even if the same record
    declares all three of fi/sv/en."""
    for title, expected in [("Sota ja rauha", "fi"), ("War and Peace", "en")]:
        source = Graph()
        source.parse(
            data=textwrap.dedent(
                f"""
                @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
                <{BF_WORK}> a bf:Work ;
                    bf:title    [ bf:mainTitle "{title}" ] ;
                    bf:language <http://id.loc.gov/vocabulary/languages/swe> ,
                                <http://id.loc.gov/vocabulary/languages/eng> ,
                                <http://id.loc.gov/vocabulary/languages/fin> .
                """
            ).strip(),
            format="turtle",
        )
        bffi = construct_bffi(source)
        post_process(bffi, source)
        label = next(bffi.objects(EXPECTED_WORK, V.SKOS.prefLabel))
        assert isinstance(label, Literal)
        assert label.language == expected, f"{title!r} should be {expected}, got {label.language}"


# --- 245$c contributor-extraction emitter --------------------------------


def _build_source_with_245c(c_subfield: str, agent_label: str) -> Graph:
    """Minimal BIBFRAME fixture with a 245$c text + one 700 agent label so
    the contrib-extract heuristic + emitter have something to chew on."""
    g = Graph()
    g.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title       [ a bf:Title ; bf:mainTitle "Title" ] ;
                bf:language    <http://id.loc.gov/vocabulary/languages/eng> ;
                bf:hasInstance <{BF_WORK}#Instance> ;
                bf:contribution <{BF_WORK}#contrib1> .

            <{BF_WORK}#Instance> a bf:Instance ;
                bf:responsibilityStatement "{c_subfield}" .

            <{BF_WORK}#contrib1> a bf:Contribution ;
                bf:agent <{BF_WORK}#agent1> .

            <{BF_WORK}#agent1> a bf:Person ;
                rdfs:label "{agent_label}" .
            """
        ).strip(),
        format="turtle",
    )
    return g


def test_emitter_skips_when_extractor_flags_transliteration_variant() -> None:
    """Option (a): when the LLM tells us a 245$c name is a variant of an
    existing 100/700 agent, don't propagate the typo'd form as a new
    Contribution. The smoke surfaced this on Helmet record 1714651,
    where 245$c read 'Anssi Karttunen' but 700 carried the canonical
    'Karttunen, Assi'. M9 script-variant binding will consume the
    transliteration pointer downstream."""
    source = _build_source_with_245c(
        "Anssi Karttunen, cembalo",
        "Karttunen, Assi",
    )
    extractor = StubContribExtractor(
        decisions={
            "Anssi Karttunen, cembalo": ContribExtractDecision(
                contributions=[
                    ContribCandidate(
                        name="Anssi Karttunen",
                        relator_code="prf",
                        transliteration_of="Karttunen, Assi",
                    ),
                ],
                rationale=(
                    "Both fields set: relator hint and variant pointer. "
                    "Emitter must skip on transliteration_of."
                ),
            )
        }
    )
    bffi = construct_bffi(source)
    post_process(bffi, source, contrib_extractor=extractor)
    # No new Contribution should appear on the Expression beyond what
    # the M3 CONSTRUCT propagated from the existing 700.
    contributions_on_expr = list(bffi.objects(EXPECTED_EXPR, V.BFFI.contribution))
    role_triples = list(bffi.triples((None, V.BF.role, None)))
    assert role_triples == []
    # The pre-existing 700 contribution is still routed by the SPARQL
    # CONSTRUCT (one entry); the cascade adds nothing.
    assert len(contributions_on_expr) == 1


def test_emitter_emits_new_contribution_when_extractor_returns_pure_relator() -> None:
    """Mirror case: cascade returns a clean new-agent candidate (no
    transliteration_of). Emitter writes a Contribution with bf:role and
    a labelled bffi:Agent on the raw Expression."""
    source = _build_source_with_245c(
        "Some Composer ; with a foreword by Tim Spector",
        "Some Composer",
    )
    extractor = StubContribExtractor(
        decisions={
            "Some Composer ; with a foreword by Tim Spector": ContribExtractDecision(
                contributions=[
                    ContribCandidate(name="Tim Spector", relator_code="aft"),
                ],
                rationale="Tim Spector introduced by 'foreword by'; relator aft.",
            )
        }
    )
    bffi = construct_bffi(source)
    post_process(bffi, source, contrib_extractor=extractor)
    role_triples = list(bffi.triples((None, V.BF.role, None)))
    assert len(role_triples) == 1
    _, _, role_uri = role_triples[0]
    assert str(role_uri) == "http://id.loc.gov/vocabulary/relators/aft"
    # And the Agent node carries the LLM-supplied name as rdfs:label.
    role_subject = role_triples[0][0]
    agent = next(bffi.objects(role_subject, V.BFFI.agent))
    label = next(bffi.objects(agent, V.RDFS.label))
    assert str(label) == "Tim Spector"
