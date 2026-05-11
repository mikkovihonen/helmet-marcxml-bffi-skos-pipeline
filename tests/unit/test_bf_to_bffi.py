"""Unit tests for stages/bf_to_bffi.

Hand-craft small BIBFRAME graphs and verify the CONSTRUCT pair routes
properties to the right side (Work vs Expression), preserves the Helmet
identifier, links Expression to Work via bffi:expressionOf, and handles
the language-tag retag on skos:prefLabel.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from bffi_pipeline.contrib_extract_llm import (
    ContribCandidate,
    ContribExtractDecision,
    StubContribExtractor,
)
from bffi_pipeline.contrib_variants import load_variant_claims
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.bf_to_bffi import (
    _convert_one,
    _is_parseable_date,
    _sanitize_date_literals,
    _sanitize_uri,
    _sanitize_uri_whitespace,
    construct_bffi,
    post_process,
)
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


def test_post_process_propagates_uri_role_to_bffi_contribution() -> None:
    """Source MARC ``$4`` controlled relator → BIBFRAME ``bf:role <URI>``;
    M3 must carry that URI through to the bffi:Contribution it mints."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
            @prefix relators: <http://id.loc.gov/vocabulary/relators/> .

            <{BF_WORK}> a bf:Work ;
                bf:title       [ a bf:Title ; bf:mainTitle "T" ] ;
                bf:hasInstance <{BF_WORK}#Inst> ;
                bf:contribution <{BF_WORK}#c1> .

            <{BF_WORK}#Inst> a bf:Instance ;
                bf:responsibilityStatement "trans by X" .

            <{BF_WORK}#c1> a bf:Contribution ;
                bf:agent <{BF_WORK}#a1> ;
                bf:role  relators:trl .

            <{BF_WORK}#a1> a bf:Person ; rdfs:label "Translator, Anna" .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    post_process(bffi, source)
    [contrib] = list(bffi.objects(EXPECTED_EXPR, V.BFFI.contribution))
    assert (
        contrib,
        V.BF.role,
        URIRef("http://id.loc.gov/vocabulary/relators/trl"),
    ) in bffi


def test_post_process_propagates_blank_node_role_label_with_typing() -> None:
    """Source MARC ``$e`` free-text → BIBFRAME blank-node ``bf:role`` with
    ``rdfs:label``; M3 must re-emit a fresh blank node typed ``bf:Role``
    with the same label so Skosmos can render the cataloguer's
    Finnish role text on the canonical Expression."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title       [ a bf:Title ; bf:mainTitle "T" ] ;
                bf:hasInstance <{BF_WORK}#Inst> ;
                bf:contribution <{BF_WORK}#c1> .

            <{BF_WORK}#Inst> a bf:Instance ;
                bf:responsibilityStatement "Hogwood ; cembalo" .

            <{BF_WORK}#c1> a bf:Contribution ;
                bf:agent <{BF_WORK}#a1> ;
                bf:role  [ a bf:Role ; rdfs:label "cembalo" ] .

            <{BF_WORK}#a1> a bf:Person ; rdfs:label "Hogwood, Chrtistopher" .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    post_process(bffi, source)
    [contrib] = list(bffi.objects(EXPECTED_EXPR, V.BFFI.contribution))
    [role] = list(bffi.objects(contrib, V.BF.role))
    # Role is a blank node typed bf:Role with the cataloguer's label
    assert (role, RDF.type, V.BF.Role) in bffi
    assert (role, V.RDFS.label, Literal("cembalo")) in bffi


def test_post_process_routes_one_role_per_repeated_agent() -> None:
    """Cataloguer enters ``700 $a Hogwood, Christopher`` three times,
    once per instrument. Source has 3 distinct bf:Contributions sharing
    one agent URI but each carrying a different role. The propagator
    must route one role per output contribution (no fan-out, no
    duplication)."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title       [ a bf:Title ; bf:mainTitle "T" ] ;
                bf:hasInstance <{BF_WORK}#Inst> ;
                bf:contribution <{BF_WORK}#c1>, <{BF_WORK}#c2>, <{BF_WORK}#c3> .

            <{BF_WORK}#Inst> a bf:Instance ; bf:responsibilityStatement "Hogwood" .

            <{BF_WORK}#c1> a bf:Contribution ;
                bf:agent <{BF_WORK}#a1> ;
                bf:role  [ a bf:Role ; rdfs:label "johtaja" ] .

            <{BF_WORK}#c2> a bf:Contribution ;
                bf:agent <{BF_WORK}#a1> ;
                bf:role  [ a bf:Role ; rdfs:label "cembalo" ] .

            <{BF_WORK}#c3> a bf:Contribution ;
                bf:agent <{BF_WORK}#a1> ;
                bf:role  [ a bf:Role ; rdfs:label "urut" ] .

            <{BF_WORK}#a1> a bf:Person ; rdfs:label "Hogwood, Christopher" .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    post_process(bffi, source)
    contribs = list(bffi.objects(EXPECTED_EXPR, V.BFFI.contribution))
    assert len(contribs) == 3
    role_labels: set[str] = set()
    for c in contribs:
        for r in bffi.objects(c, V.BF.role):
            for lab in bffi.objects(r, V.RDFS.label):
                role_labels.add(str(lab))
    assert role_labels == {"johtaja", "cembalo", "urut"}


# --- F2: variants sidecar persistence ------------------------------------


def test_post_process_writes_variant_to_sidecar(tmp_path: Path) -> None:
    """When the cascade returns a transliteration_of pointer, M3
    appends a row to the variants sidecar (and skips Contribution
    emission)."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
            @prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

            <{BF_WORK}> a bf:Work ;
                bf:title       [ a bf:Title ; bf:mainTitle "T" ] ;
                bf:hasInstance <{BF_WORK}#Inst> ;
                bf:identifiedBy <{BF_WORK}#hid> ;
                bf:contribution <{BF_WORK}#c1> .

            <{BF_WORK}#Inst> a bf:Instance ;
                bf:responsibilityStatement "Anssi Karttunen, cembalo" .

            <{BF_WORK}#c1> a bf:Contribution ;
                bf:agent <{BF_WORK}#a1> .

            <{BF_WORK}#a1> rdfs:label "Karttunen, Assi" .

            <{BF_WORK}#hid> a bf:Local ;
                rdf:value "1714651" ;
                bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> .
            """
        ).strip(),
        format="turtle",
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
                rationale="Latin-script variant of the existing 700 entry.",
            )
        }
    )
    sidecar = tmp_path / "contrib-variants.jsonl"
    bffi = construct_bffi(source)
    post_process(bffi, source, contrib_extractor=extractor, variants_sidecar_path=sidecar)
    # No new bf:role triples (variant skipped from Contribution emission).
    assert list(bffi.triples((None, V.BF.role, None))) == []
    # Sidecar carries the variant claim.
    [claim] = load_variant_claims(sidecar)
    assert claim.helmet_bib_id == "1714651"
    assert claim.variant_label == "Anssi Karttunen"
    assert claim.canonical_label == "Karttunen, Assi"
    assert claim.relator_code_hint == "prf"


def test_post_process_skips_sidecar_when_cascade_finds_no_variants(tmp_path: Path) -> None:
    """A cascade run with only pure-new-agent decisions writes nothing
    to the sidecar — no zero-row file, no empty-stub claim."""
    source = _build_source_with_245c("Edited by Stanley Sadie", "Some Other Name")
    extractor = StubContribExtractor(
        decisions={
            "Edited by Stanley Sadie": ContribExtractDecision(
                contributions=[ContribCandidate(name="Stanley Sadie", relator_code="edt")],
                rationale="New editor; not a variant of any existing agent.",
            )
        }
    )
    sidecar = tmp_path / "contrib-variants.jsonl"
    bffi = construct_bffi(source)
    post_process(bffi, source, contrib_extractor=extractor, variants_sidecar_path=sidecar)
    assert load_variant_claims(sidecar) == []


def test_post_process_does_not_touch_sidecar_when_path_is_none() -> None:
    """No sidecar path supplied → no I/O, even when the cascade emits
    a variant. Backwards-compatible with callers that don't want the
    sidecar."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title       [ a bf:Title ; bf:mainTitle "T" ] ;
                bf:hasInstance <{BF_WORK}#Inst> ;
                bf:contribution <{BF_WORK}#c1> .

            <{BF_WORK}#Inst> a bf:Instance ; bf:responsibilityStatement "x" .
            <{BF_WORK}#c1> a bf:Contribution ; bf:agent <{BF_WORK}#a1> .
            <{BF_WORK}#a1> rdfs:label "Karttunen, Assi" .
            """
        ).strip(),
        format="turtle",
    )
    extractor = StubContribExtractor(
        decisions={
            "x": ContribExtractDecision(
                contributions=[
                    ContribCandidate(
                        name="x",
                        transliteration_of="Karttunen, Assi",
                    ),
                ],
                rationale="Variant — but no sidecar requested, so nothing persists.",
            )
        }
    )
    bffi = construct_bffi(source)
    # No path → no sidecar I/O. Should not raise.
    post_process(bffi, source, contrib_extractor=extractor, variants_sidecar_path=None)


# --- _sanitize_uri / _sanitize_uri_whitespace ---------------------------


def test_sanitize_uri_strips_trailing_whitespace() -> None:
    assert _sanitize_uri("http://urn.fi/URN:NBN:fi:au:slm:s1288 ") == (
        "http://urn.fi/URN:NBN:fi:au:slm:s1288"
    )


def test_sanitize_uri_strips_leading_whitespace() -> None:
    assert _sanitize_uri(" http://example.org/x") == "http://example.org/x"


def test_sanitize_uri_strips_multiple_trailing_spaces() -> None:
    assert _sanitize_uri("http://www.yso.fi/onto/kauno/p2755  ") == (
        "http://www.yso.fi/onto/kauno/p2755"
    )


def test_sanitize_uri_percent_encodes_internal_space() -> None:
    """Embedded whitespace probably means two cataloguer IDs got
    accidentally concatenated. Percent-encode rather than drop so the
    URI is lexically valid + auditable."""
    assert _sanitize_uri("http://urn.fi/URN:NBN:fi:au:slm:s1140655 7") == (
        "http://urn.fi/URN:NBN:fi:au:slm:s1140655%207"
    )


def test_sanitize_uri_passes_clean_uri_unchanged() -> None:
    assert _sanitize_uri("http://www.yso.fi/onto/yso/p1018") == ("http://www.yso.fi/onto/yso/p1018")


def test_sanitize_uri_whitespace_rewrites_graph_in_place() -> None:
    """Walking the graph rewrites every position (subject, predicate,
    object) so a single sanitization pass before the CONSTRUCT clears
    every malformed authority $0."""
    g = Graph()
    bad = URIRef("http://urn.fi/URN:NBN:fi:au:slm:s1288 ")
    clean = URIRef("http://urn.fi/URN:NBN:fi:au:slm:s1288")
    g.add((URIRef("http://example.org/work/1"), URIRef("http://example.org/p"), bad))
    n_rewrites = _sanitize_uri_whitespace(g)
    assert n_rewrites == 1
    assert (URIRef("http://example.org/work/1"), URIRef("http://example.org/p"), clean) in g
    assert (URIRef("http://example.org/work/1"), URIRef("http://example.org/p"), bad) not in g


def test_sanitize_uri_whitespace_handles_zero_rewrites() -> None:
    """A clean graph passes through unchanged with rewrite-count zero."""
    g = Graph()
    g.add(
        (
            URIRef("http://example.org/w/1"),
            URIRef("http://example.org/p"),
            URIRef("http://example.org/o"),
        )
    )
    assert _sanitize_uri_whitespace(g) == 0
    assert len(g) == 1


def test_convert_one_serialises_sanitised_uris_to_valid_turtle(tmp_path: Path) -> None:
    """End-to-end: a BIBFRAME source carrying a whitespace-tainted $0
    URI now round-trips through M3 to clean Turtle that Fuseki + rdflib
    can parse downstream. Before the sanitization, rdflib emitted a
    'does not look like a valid URI' warning and the conversion
    skipped the record entirely."""
    bib_root = "http://urn.fi/URN:NBN:fi:bib:raw/sanitize-test"
    source_ttl = f"""
    @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    <{bib_root}#Work> a bf:Work ;
        bf:title [ a bf:Title ; bf:mainTitle "Test" ] ;
        bf:language <http://id.loc.gov/vocabulary/languages/fin> ;
        bf:subject <http://urn.fi/URN:NBN:fi:au:slm:s1288 > .
    """
    input_path = tmp_path / "src.rdf"
    g = Graph()
    g.parse(data=source_ttl, format="turtle")
    g.serialize(destination=str(input_path), format="xml")
    output_path = tmp_path / "out.ttl"
    _convert_one(input_path, output_path, llm_detector=None, contrib_extractor=None)
    body = output_path.read_text(encoding="utf-8")
    # The cleaned URI is in the output; the whitespace-tainted form is not.
    assert "URN:NBN:fi:au:slm:s1288>" in body
    assert "s1288 >" not in body


# --- _is_parseable_date / _sanitize_date_literals -----------------------


def test_parseable_date_accepts_valid_datetime() -> None:
    assert _is_parseable_date(
        "2026-05-09T14:30:00", URIRef("http://www.w3.org/2001/XMLSchema#dateTime")
    )


def test_parseable_date_rejects_cataloguer_placeholder_datetime() -> None:
    """The 200-record corpus smoke surfaced ``'19  -  -  T00:00:00'``
    as a cataloguer placeholder for "year not yet entered". rdflib
    coerces this on load and raises ValueError, crashing the
    downstream merge. The sanitizer must reject the bad lexical form."""
    assert not _is_parseable_date(
        "19  -  -  T00:00:00",
        URIRef("http://www.w3.org/2001/XMLSchema#dateTime"),
    )


def test_parseable_date_accepts_valid_gyear() -> None:
    assert _is_parseable_date("2026", URIRef("http://www.w3.org/2001/XMLSchema#gYear"))


def test_parseable_date_rejects_short_gyear() -> None:
    assert not _is_parseable_date("26", URIRef("http://www.w3.org/2001/XMLSchema#gYear"))


def test_parseable_date_accepts_valid_gyearmonth() -> None:
    assert _is_parseable_date("2026-05", URIRef("http://www.w3.org/2001/XMLSchema#gYearMonth"))


def test_parseable_date_rejects_gyearmonth_with_bad_month() -> None:
    assert not _is_parseable_date("2026-13", URIRef("http://www.w3.org/2001/XMLSchema#gYearMonth"))


def test_sanitize_strips_datatype_on_bad_datetime() -> None:
    """A malformed xsd:dateTime literal loses its datatype tag and
    survives as a plain string — value visible for audit, no
    downstream rdflib crash on load."""
    g = Graph()
    work = URIRef("http://example.org/w/1")
    g.add(
        (
            work,
            V.BFFI.descriptionChangeDate,
            Literal(
                "19  -  -  T00:00:00",
                datatype=URIRef("http://www.w3.org/2001/XMLSchema#dateTime"),
            ),
        )
    )
    n = _sanitize_date_literals(g)
    assert n == 1
    # Round-trip: the value is still present but no longer typed.
    [value] = list(g.objects(work, V.BFFI.descriptionChangeDate))
    assert isinstance(value, Literal)
    assert str(value) == "19  -  -  T00:00:00"
    assert value.datatype is None


def test_sanitize_keeps_valid_datetime_unchanged() -> None:
    g = Graph()
    g.add(
        (
            URIRef("http://example.org/w/1"),
            V.BFFI.descriptionChangeDate,
            Literal(
                "2026-05-09T14:30:00",
                datatype=URIRef("http://www.w3.org/2001/XMLSchema#dateTime"),
            ),
        )
    )
    assert _sanitize_date_literals(g) == 0


def test_sanitize_does_not_touch_non_date_literals() -> None:
    """Plain xsd:string literals (e.g. titles, labels) must pass
    through untouched even if they happen to contain digits or
    look-like-date text — the sanitizer is scoped to the four
    XSD date datatypes."""
    g = Graph()
    g.add(
        (
            URIRef("http://example.org/w/1"),
            V.RDFS.label,
            Literal("Year 19  -  -  T00:00:00 (an art title)"),  # untyped
        )
    )
    assert _sanitize_date_literals(g) == 0


def test_convert_one_survives_record_with_malformed_date(tmp_path: Path) -> None:
    """End-to-end: a BIBFRAME source carrying a bad ``xsd:dateTime``
    placeholder now round-trips through M3 to Turtle that the M8
    merge can load without raising ValueError."""
    bib_root = "http://urn.fi/URN:NBN:fi:bib:raw/baddate-test"
    source_ttl = f"""
    @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
    <{bib_root}#Work> a bf:Work ;
        bf:title [ a bf:Title ; bf:mainTitle "Test" ] ;
        bf:language <http://id.loc.gov/vocabulary/languages/fin> ;
        bf:originDate "19  -  -  T00:00:00"^^xsd:dateTime .
    """
    input_path = tmp_path / "src.rdf"
    g = Graph()
    g.parse(data=source_ttl, format="turtle")
    g.serialize(destination=str(input_path), format="xml")
    output_path = tmp_path / "out.ttl"
    _convert_one(input_path, output_path, llm_detector=None, contrib_extractor=None)
    # The value survives as untyped text — no xsd:dateTime suffix.
    body = output_path.read_text(encoding="utf-8")
    assert "19  -  -  T00:00:00" in body
    # And the previously-failing typed form is gone.
    assert '"19  -  -  T00:00:00"^^xsd:dateTime' not in body
    # Sanity: the merge load shouldn't raise (which would happen if
    # rdflib re-coerced the bad lexical form to datetime).
    reloaded = Graph()
    reloaded.parse(str(output_path), format="turtle")
