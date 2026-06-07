"""Unit tests for stages/bf_to_bffi.

Hand-craft small BIBFRAME graphs and verify the CONSTRUCT pair routes
properties to the right side (Work vs Expression), preserves the Helmet
identifier, links Expression to Work via bffi:expressionOf, and handles
the language-tag retag on skos:prefLabel.
"""

from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from bffi_pipeline.contrib_extract_llm import (
    ContribCandidate,
    ContribExtractDecision,
    StubContribExtractor,
)
from bffi_pipeline.contrib_variants import load_variant_claims
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m3 import (
    BFFI_CORPUS_FILENAME,
    ValidationRow,
    construct_bffi,
    post_process,
)
from bffi_pipeline.stages.m3.post_process import (
    _enrich_aggregation_components_with_agents,
)
from bffi_pipeline.stages.m3.runner import (
    _convert_one,
    _emit_validation_tsv,
    _is_parseable_date,
    _sanitize_date_literals,
    _sanitize_uri,
    _sanitize_uri_whitespace,
    _write_bffi_corpus,
)
from bffi_pipeline.uris import (
    mint_raw_expression_uri,
    mint_raw_manifestation_uri,
    mint_raw_work_uri,
)

BF_WORK = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work"
BF_INSTANCE = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Instance"
EXPECTED_WORK = URIRef(mint_raw_work_uri(BF_WORK))
EXPECTED_EXPR = URIRef(mint_raw_expression_uri(BF_WORK))
EXPECTED_MANIF = URIRef(mint_raw_manifestation_uri(BF_INSTANCE))

# A minimal BIBFRAME graph mimicking marc2bibframe2 v3.1.0 output for a
# Tolstoy translation. Two contributions: PrimaryContribution (Tolstoy)
# and a non-primary (translator). The bf:Instance is included because
# P-45 commit 2 moved the Helmet bib_id from Work → Manifestation, and
# the M3 manifestation CONSTRUCT requires ``?bfInstance bf:instanceOf
# ?bfWork`` to produce any bffi:Manifestation triples.
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

    <{BF_INSTANCE}> a bf:Instance ;
        bf:instanceOf <{BF_WORK}> ;
        bf:media      <http://id.loc.gov/vocabulary/mediaTypes/n> ;
        bf:carrier    <http://id.loc.gov/vocabulary/carriers/nc> ;
        bf:digitalCharacteristic <http://id.loc.gov/vocabulary/mencformat/dvdv> ;
        bf:soundCharacteristic   <http://id.loc.gov/vocabulary/mrecmedium/opt> ;
        bf:colorContent          <http://id.loc.gov/vocabulary/mcolor/mul> ;
        bf:title      [ a bf:Title ; bf:mainTitle "Sota ja rauha" ] ;
        bf:publicationStatement "Helsinki : Otava, 1923" .

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


def test_construct_passes_through_uri_keyed_from_marc_field_tokens() -> None:
    """M2-post attaches ``bffi-prov:fromMarcField`` tokens to raw-bib
    URIs (``#Agent700-N``, ``#Hub730-N``, ``#Topic650-N``, ...) before
    M3 runs. The M3 CONSTRUCTs reference those raw URIs verbatim as
    targets of ``bffi:agent`` / ``bffi:subject`` / etc., so the token
    triple should land on the BFFI-side graph unchanged. Without the
    passthrough, M8's catch-all only sees the Phase C Statement-subject
    tokens and the round-trip converter falls back to the legacy
    rank-bucket scheme."""
    source = _build_source()
    agent = URIRef("urn:agent/Tolstoy")
    source.add((agent, V.fromMarcField, Literal("10000001:100:1")))
    bffi = construct_bffi(source)
    tokens = list(bffi.objects(agent, V.fromMarcField))
    assert tokens == [Literal("10000001:100:1")]


def test_construct_passes_through_bnode_keyed_from_marc_field_tokens() -> None:
    """Phase B flat-field tokens (``bf:Isbn`` / ``bf:Note`` / ``bf:Title``
    / ``bf:Extent`` / ``bf:ProvisionActivity``) live on source blank
    nodes. rdflib preserves bnode identity within a single ``Graph``
    so the same blank-node object that's emitted on the BFFI side via
    the manifestation CONSTRUCT also carries the ``fromMarcField``
    triple after the passthrough. The per-record Turtle serialiser
    uses one consistent label per ``BNode``, so the M8 corpus concat
    round-trips the identity within per-record scope — sufficient for
    the round-trip diff comparator's per-Manifestation scan."""
    source = _build_source()
    bnode_subject = BNode()
    source.add((bnode_subject, V.fromMarcField, Literal("10000001:020:1")))
    bffi = construct_bffi(source)
    assert (bnode_subject, V.fromMarcField, Literal("10000001:020:1")) in bffi


def test_construct_mirrors_expression_axis_bibframe_classes_as_bffi_subclasses() -> None:
    """P-52 Phase A — Expression-axis OWL-equivalent typing mirror.

    Each of the 9 BIBFRAME content / realisation-form classes that BFFI
    declares ``owl:equivalentClass`` on the Expression side gets a
    parallel ``a bffi:<Class>`` triple emitted on the bffi:Expression
    URI. Existing ``a bffi:Expression`` typing stays (additive).
    """
    expr_pairs = [
        ("Text", V.BFFI.Text),
        ("NotatedMusic", V.BFFI.NotatedMusic),
        ("NotatedMovement", V.BFFI.NotatedMovement),
        ("StillImage", V.BFFI.StillImage),
        ("Dataset", V.BFFI.Dataset),
        ("Object", V.BFFI.Object),
        ("MixedMaterial", V.BFFI.MixedMaterial),
        ("Multimedia", V.BFFI.Multimedia),
        ("Arrangement", V.BFFI.Arrangement),
    ]
    for bf_local_name, bffi_class in expr_pairs:
        source = _build_source()
        source.add((URIRef(BF_WORK), RDF.type, V.BF[bf_local_name]))
        bffi = construct_bffi(source)
        assert (EXPECTED_EXPR, RDF.type, bffi_class) in bffi, (
            f"bf:{bf_local_name} not mirrored to {bffi_class!r} on Expression"
        )
        # The default bffi:Expression typing is still present.
        assert (EXPECTED_EXPR, RDF.type, V.BFFI.Expression) in bffi


def test_construct_mirrors_work_axis_bibframe_classes_as_bffi_subclasses() -> None:
    """P-52 Phase A — Work-axis mirror (Manuscript + Integrating)."""
    work_pairs = [
        ("Manuscript", V.BFFI.Manuscript),
        ("Integrating", V.BFFI.Integrating),
    ]
    for bf_local_name, bffi_class in work_pairs:
        source = _build_source()
        source.add((URIRef(BF_WORK), RDF.type, V.BF[bf_local_name]))
        bffi = construct_bffi(source)
        assert (EXPECTED_WORK, RDF.type, bffi_class) in bffi, (
            f"bf:{bf_local_name} not mirrored to {bffi_class!r} on Work"
        )
        assert (EXPECTED_WORK, RDF.type, V.BFFI.Work) in bffi


def test_construct_mirrors_manifestation_axis_bibframe_classes_from_instance() -> None:
    """P-52 Phase A — Manifestation-axis mirror for the bf:Instance
    side (the natural BIBFRAME home for carrier-form classes)."""
    manif_pairs = [
        ("Print", V.BFFI.Print),
        ("Electronic", V.BFFI.Electronic),
        ("Microform", V.BFFI.Microform),
        ("Tactile", V.BFFI.Tactile),
        ("Archival", V.BFFI.Archival),
    ]
    for bf_local_name, bffi_class in manif_pairs:
        source = _build_source()
        source.add((URIRef(BF_INSTANCE), RDF.type, V.BF[bf_local_name]))
        bffi = construct_bffi(source)
        assert (EXPECTED_MANIF, RDF.type, bffi_class) in bffi, (
            f"bf:{bf_local_name} not mirrored to {bffi_class!r} on Manifestation (Instance-side)"
        )
        assert (EXPECTED_MANIF, RDF.type, V.BFFI.Manifestation) in bffi


def test_construct_mirrors_manifestation_axis_bibframe_classes_from_work() -> None:
    """P-52 Phase A — Manifestation-axis mirror when the BIBFRAME
    class lands on ``bf:Work`` (older Helmet records pre-dating the
    bf:Instance routing convention). The CONSTRUCT UNION's both sides
    so cataloguer-typing-style drift is absorbed without per-record
    pre-flight."""
    source = _build_source()
    source.add((URIRef(BF_WORK), RDF.type, V.BF.Print))
    bffi = construct_bffi(source)
    assert (EXPECTED_MANIF, RDF.type, V.BFFI.Print) in bffi


def test_construct_emits_phase_b_broadmatch_two_axis_typing() -> None:
    """P-52 Phase B — content-shape classes that map to BFFI Work +
    Expression subclass pairs via ``bffi-meta:broadMatch``. Each
    BIBFRAME class produces typing on BOTH the bffi:Work and the
    bffi:Expression (different BFFI subclasses, same source signal).
    """
    pairs = [
        ("Cartography", V.BFFI.CartographyWork, V.BFFI.CartographyExpression),
        ("MovingImage", V.BFFI.MovingImageWork, V.BFFI.MovingImageExpression),
        ("MusicAudio", V.BFFI.MusicWork, V.BFFI.MusicAudioExpression),
        ("Audio", V.BFFI.NonMusicAudioWork, V.BFFI.NonMusicAudioExpression),
    ]
    for bf_name, work_class, expr_class in pairs:
        source = _build_source()
        source.add((URIRef(BF_WORK), RDF.type, V.BF[bf_name]))
        bffi = construct_bffi(source)
        assert (EXPECTED_WORK, RDF.type, work_class) in bffi, (
            f"bf:{bf_name} not routed to {work_class!r} on Work axis"
        )
        assert (EXPECTED_EXPR, RDF.type, expr_class) in bffi, (
            f"bf:{bf_name} not routed to {expr_class!r} on Expression axis"
        )


def test_construct_emits_phase_b_notatedmusic_routes_to_musicwork_on_work_axis() -> None:
    """bf:NotatedMusic produces ``bffi:NotatedMusic`` on the
    Expression (Phase A OWL-equivalent) AND ``bffi:MusicWork`` on
    the Work (Phase B broadMatch, since sheet music IS a music
    work, just realised in notated form rather than as audio)."""
    source = _build_source()
    source.add((URIRef(BF_WORK), RDF.type, V.BF.NotatedMusic))
    bffi = construct_bffi(source)
    assert (EXPECTED_EXPR, RDF.type, V.BFFI.NotatedMusic) in bffi
    assert (EXPECTED_WORK, RDF.type, V.BFFI.MusicWork) in bffi


def test_construct_emits_phase_c_issuance_derived_typing() -> None:
    """P-52 Phase C — Work + Expression typing routed from
    ``bf:Instance bf:issuance <…/issuance/{mono,serial,collection,integrating}>``.

    The Helmet fixture's bf:Instance has no bf:issuance set; we add
    it per test case and assert the matching axis subclass appears.
    """
    cases = [
        ("mono", V.BFFI.MonographWork, V.BFFI.MonographExpression, None),
        ("serial", V.BFFI.SerialWork, V.BFFI.SerialExpression, None),
        (
            "collection",
            V.BFFI.CollectionWork,
            V.BFFI.CollectionExpression,
            V.BFFI.CollectionManifestation,
        ),
        # Integrating has no Expression / Manifestation counterpart in BFFI.
        ("integrating", V.BFFI.Integrating, None, None),
    ]
    for issuance, work_class, expr_class, manif_class in cases:
        source = _build_source()
        issuance_uri = URIRef(f"http://id.loc.gov/vocabulary/issuance/{issuance}")
        source.add((URIRef(BF_INSTANCE), V.BF.issuance, issuance_uri))
        bffi = construct_bffi(source)
        assert (EXPECTED_WORK, RDF.type, work_class) in bffi, (
            f"issuance/{issuance} not routed to {work_class!r} on Work"
        )
        if expr_class is not None:
            assert (EXPECTED_EXPR, RDF.type, expr_class) in bffi
        if manif_class is not None:
            assert (EXPECTED_MANIF, RDF.type, manif_class) in bffi


def test_construct_emits_phase_d_carrier_derived_manifestation_typing() -> None:
    """P-52 Phase D — Manifestation-axis class inferred from
    ``bffi:carrier`` URI. Tests the four dominant routings."""
    cases = [
        ("nc", V.BFFI.Print),
        ("cr", V.BFFI.Electronic),
        ("he", V.BFFI.Microform),
    ]
    for carrier_tail, manif_class in cases:
        source = _build_source()
        # Replace the fixture's existing nc carrier with the test case
        for o in list(source.objects(URIRef(BF_INSTANCE), V.BF.carrier)):
            source.remove((URIRef(BF_INSTANCE), V.BF.carrier, o))
        carrier_uri = URIRef(f"http://id.loc.gov/vocabulary/carriers/{carrier_tail}")
        source.add((URIRef(BF_INSTANCE), V.BF.carrier, carrier_uri))
        bffi = construct_bffi(source)
        assert (EXPECTED_MANIF, RDF.type, manif_class) in bffi, (
            f"carriers/{carrier_tail} not routed to {manif_class!r}"
        )


def test_construct_emits_phase_e_aggregating_typing_with_multiple_hubs() -> None:
    """P-52 Phase E — ≥2 ``bf:Hub`` linked via ``bf:relation`` →
    parent gets ``bffi:AggregatingWork`` + ``bffi:AggregatingExpression``
    typing."""
    source = _build_source()
    # Add 2 distinct Hubs reachable via bf:relation
    bf_work = URIRef(BF_WORK)
    for i in range(2):
        rel = URIRef(f"urn:test:rel/{i}")
        hub = URIRef(f"urn:test:hub/{i}")
        source.add((bf_work, V.BF.relation, rel))
        source.add((rel, V.BF.associatedResource, hub))
        source.add((hub, RDF.type, V.BF.Hub))
    bffi = construct_bffi(source)
    assert (EXPECTED_WORK, RDF.type, V.BFFI.AggregatingWork) in bffi
    assert (EXPECTED_EXPR, RDF.type, V.BFFI.AggregatingExpression) in bffi


def test_construct_emits_phase_e_aggregating_typing_with_partnumber() -> None:
    """P-52 Phase E — ``bf:partNumber``/``bf:partName`` on a title is
    a multipart-record signal; parent gets AggregatingWork +
    AggregatingExpression typing."""
    source = _build_source()
    title = URIRef("urn:test:title")
    source.add((URIRef(BF_WORK), V.BF.title, title))
    source.add((title, V.BF.partNumber, Literal("2")))
    bffi = construct_bffi(source)
    assert (EXPECTED_WORK, RDF.type, V.BFFI.AggregatingWork) in bffi
    assert (EXPECTED_EXPR, RDF.type, V.BFFI.AggregatingExpression) in bffi


def test_construct_does_not_emit_aggregating_typing_for_single_hub() -> None:
    """A single source Hub (one related uniform title) is NOT
    aggregation. Phase E detection requires ≥2 distinct Hubs."""
    source = _build_source()
    rel = URIRef("urn:test:rel/0")
    hub = URIRef("urn:test:hub/0")
    source.add((URIRef(BF_WORK), V.BF.relation, rel))
    source.add((rel, V.BF.associatedResource, hub))
    source.add((hub, RDF.type, V.BF.Hub))
    bffi = construct_bffi(source)
    assert (EXPECTED_WORK, RDF.type, V.BFFI.AggregatingWork) not in bffi
    assert (EXPECTED_EXPR, RDF.type, V.BFFI.AggregatingExpression) not in bffi


def test_construct_emits_phase_f_component_expressions_for_aggregating_parent() -> None:
    """P-52 Phase F — when parent is AggregatingExpression-typed,
    each source Hub gets a derived component bffi:Expression URI
    linked via ``bffi:aggregates`` (forward) and ``bffi:aggregatedBy``
    (reverse)."""
    source = _build_source()
    bf_work = URIRef(BF_WORK)
    hubs = []
    for i in range(2):
        rel = URIRef(f"urn:test:rel/{i}")
        hub = URIRef(f"urn:test:hub/component-{i}")
        source.add((bf_work, V.BF.relation, rel))
        source.add((rel, V.BF.associatedResource, hub))
        source.add((hub, RDF.type, V.BF.Hub))
        source.add((hub, V.RDFS.label, Literal(f"Component {i}")))
        hubs.append(hub)
    bffi = construct_bffi(source)
    # bffi:aggregates count matches Hub count
    components = list(bffi.objects(EXPECTED_EXPR, V.BFFI.aggregates))
    assert len(components) == 2, f"Expected 2 components, got {len(components)}: {components!r}"
    # Each component is a bffi:Expression with the reverse bffi:aggregatedBy
    for comp in components:
        assert (comp, RDF.type, V.BFFI.Expression) in bffi
        assert (comp, V.BFFI.aggregatedBy, EXPECTED_EXPR) in bffi
        # Label survives via skos:prefLabel
        labels = list(bffi.objects(comp, V.SKOS.prefLabel))
        assert any("Component" in str(lbl) for lbl in labels), (
            f"Component label missing on {comp!r}: {labels!r}"
        )


def test_phase_g_bis_extracts_g_subfield_from_component_marckey() -> None:
    """P-52 Phase G.bis Sub-task A — components carrying source
    ``bflc:marcKey`` with a ``$g`` subfield (e.g. MARC 730 ``$aTitle
    /$gAgent Name``) get a synthesised ``bffi:contribution →
    bffi:agent → rdfs:label`` chain. Drives the round-trip 700 ind2=2
    emit + M9 component-agent reconciliation."""
    source = _build_source()
    bf_work = URIRef(BF_WORK)
    # Two source 730 hubs, each with an $a title and $g agent
    hubs_and_agents = [
        ("Component One", "Author One"),
        ("Component Two", "Author Two"),
    ]
    for i, (title, agent_name) in enumerate(hubs_and_agents):
        rel = URIRef(f"urn:test:rel/{i}")
        hub = URIRef(f"urn:test:hub/{i}")
        source.add((bf_work, V.BF.relation, rel))
        source.add((rel, V.BF.associatedResource, hub))
        source.add((hub, RDF.type, V.BF.Hub))
        source.add((hub, V.RDFS.label, Literal(title)))
        source.add(
            (
                hub,
                V.BFLC.marcKey,
                Literal(f"73000 $a{title} /$g{agent_name}"),
            )
        )
    bffi = construct_bffi(source)
    post_process(bffi, source)

    components = list(bffi.objects(EXPECTED_EXPR, V.BFFI.aggregates))
    assert len(components) == 2
    # Collect agent labels from each component's contribution chain.
    agent_labels: set[str] = set()
    for comp in components:
        for contrib in bffi.objects(comp, V.BFFI.contribution):
            assert (contrib, RDF.type, V.BFFI.Contribution) in bffi
            for agent in bffi.objects(contrib, V.BFFI.agent):
                assert (agent, RDF.type, V.BF.Agent) in bffi
                for lbl in bffi.objects(agent, V.RDFS.label):
                    agent_labels.add(str(lbl))
    assert agent_labels == {"Author One", "Author Two"}


def test_phase_g_bis_skips_components_without_g_subfield() -> None:
    """A component whose marcKey has no ``$g`` (e.g. 730 with only
    ``$a``) gets no contribution chain — there's no agent signal to
    synthesise from. The component still has skos:prefLabel +
    bflc:marcKey."""
    source = _build_source()
    bf_work = URIRef(BF_WORK)
    for i in range(2):
        rel = URIRef(f"urn:test:rel/{i}")
        hub = URIRef(f"urn:test:hub/{i}")
        source.add((bf_work, V.BF.relation, rel))
        source.add((rel, V.BF.associatedResource, hub))
        source.add((hub, RDF.type, V.BF.Hub))
        source.add((hub, V.RDFS.label, Literal(f"Component {i}")))
        # marcKey with $a only, no $g
        source.add((hub, V.BFLC.marcKey, Literal(f"73000 $aComponent {i}")))
    bffi = construct_bffi(source)
    post_process(bffi, source)

    for comp in bffi.objects(EXPECTED_EXPR, V.BFFI.aggregates):
        contribs = list(bffi.objects(comp, V.BFFI.contribution))
        assert contribs == [], f"Component without $g should have no contribution; got {contribs!r}"


def test_phase_g_bis_is_idempotent_on_already_enriched_components() -> None:
    """Re-running the enrichment pass on a graph that already has
    component contributions is a no-op (no duplicate contributions
    or agents emitted)."""
    source = _build_source()
    bf_work = URIRef(BF_WORK)
    rel = URIRef("urn:test:rel/0")
    hub = URIRef("urn:test:hub/0")
    rel2 = URIRef("urn:test:rel/1")
    hub2 = URIRef("urn:test:hub/1")
    for r, h, agent in [(rel, hub, "X"), (rel2, hub2, "Y")]:
        source.add((bf_work, V.BF.relation, r))
        source.add((r, V.BF.associatedResource, h))
        source.add((h, RDF.type, V.BF.Hub))
        source.add((h, V.BFLC.marcKey, Literal(f"73000 $aTitle /$g{agent}")))
    bffi = construct_bffi(source)

    _enrich_aggregation_components_with_agents(bffi)
    first_pass_count = len(list(bffi.triples((None, V.BFFI.contribution, None))))
    _enrich_aggregation_components_with_agents(bffi)
    second_pass_count = len(list(bffi.triples((None, V.BFFI.contribution, None))))
    # Counts equal across passes — re-running adds no new contributions.
    # Absolute count includes the fixture's primary + non-primary
    # contributions plus the 2 we synthesised.
    assert first_pass_count == second_pass_count
    # And the 2 component contributions specifically are present.
    component_contribs = [
        c
        for comp in bffi.objects(EXPECTED_EXPR, V.BFFI.aggregates)
        for c in bffi.objects(comp, V.BFFI.contribution)
    ]
    assert len(component_contribs) == 2


def test_construct_does_not_emit_components_for_non_aggregating_parent() -> None:
    """A non-aggregating parent (single Hub) gets no
    ``bffi:aggregates`` edges — the predicate's domain is
    AggregatingExpression and the SPARQL gates on the multi-Hub
    detection rule."""
    source = _build_source()
    rel = URIRef("urn:test:rel/0")
    hub = URIRef("urn:test:hub/0")
    source.add((URIRef(BF_WORK), V.BF.relation, rel))
    source.add((rel, V.BF.associatedResource, hub))
    source.add((hub, RDF.type, V.BF.Hub))
    source.add((hub, V.RDFS.label, Literal("Solo component")))
    bffi = construct_bffi(source)
    components = list(bffi.objects(EXPECTED_EXPR, V.BFFI.aggregates))
    assert components == [], (
        f"Non-aggregating parent should have no bffi:aggregates edges; got {components!r}"
    )


def test_construct_emits_only_signal_supported_subclass_typing() -> None:
    """The baseline fixture has ``bf:carrier <carriers/nc>`` (regular
    printed volume) but no content-type secondary class, no
    ``bf:issuance``, no ``bf:Manuscript`` / ``bf:Tactile`` / etc.
    Only Phase D's carrier-derived ``bffi:Print`` should fire from
    the fixture's signals; no other BFFI subclass should appear.
    """
    bffi = construct_bffi(_build_source())
    all_bffi_subclasses = {
        # Phase A — OWL-equivalent
        V.BFFI.Text,
        V.BFFI.NotatedMusic,
        V.BFFI.NotatedMovement,
        V.BFFI.StillImage,
        V.BFFI.Dataset,
        V.BFFI.Object,
        V.BFFI.MixedMaterial,
        V.BFFI.Multimedia,
        V.BFFI.Arrangement,
        V.BFFI.Manuscript,
        V.BFFI.Integrating,
        V.BFFI.Electronic,
        V.BFFI.Microform,
        V.BFFI.Tactile,
        V.BFFI.Archival,
        # Phase B — broadMatch
        V.BFFI.CartographyWork,
        V.BFFI.CartographyExpression,
        V.BFFI.MovingImageWork,
        V.BFFI.MovingImageExpression,
        V.BFFI.MusicWork,
        V.BFFI.MusicAudioExpression,
        V.BFFI.NonMusicAudioWork,
        V.BFFI.NonMusicAudioExpression,
        # Phase C — issuance
        V.BFFI.MonographWork,
        V.BFFI.MonographExpression,
        V.BFFI.SerialWork,
        V.BFFI.SerialExpression,
        V.BFFI.CollectionWork,
        V.BFFI.CollectionExpression,
        V.BFFI.CollectionManifestation,
    }
    emitted_types = (
        set(bffi.objects(EXPECTED_WORK, RDF.type))
        | set(bffi.objects(EXPECTED_EXPR, RDF.type))
        | set(bffi.objects(EXPECTED_MANIF, RDF.type))
    )
    expected_from_fixture = {V.BFFI.Print}  # carriers/nc → Phase D
    extras = (emitted_types & all_bffi_subclasses) - expected_from_fixture
    assert not extras, f"Unexpected BFFI subclass typing in baseline fixture: {extras!r}"
    assert V.BFFI.Print in emitted_types, "Expected Phase D Print typing from bf:carrier/nc"


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


def test_helmet_identifier_lives_on_manifestation_not_work_or_expression() -> None:
    """P-45 commit 2: ``bf:identifiedBy`` moved from Work + Expression to
    Manifestation. The bib_id identifies a published embodiment, not the
    abstract creative entity. Discoverable from Work via the
    Work → Expression ← Manifestation walk."""
    bffi = construct_bffi(_build_source())
    helmet = URIRef("http://urn.fi/URN:NBN:fi:bib:source:helmet")
    # Manifestation carries the identifier.
    idents = list(bffi.objects(EXPECTED_MANIF, V.BF.identifiedBy))
    assert len(idents) == 1, f"missing Helmet identifier on Manifestation {EXPECTED_MANIF}"
    ident = idents[0]
    assert (ident, V.BF.source, helmet) in bffi
    assert (ident, RDF.value, Literal("10000001")) in bffi
    # Work + Expression do NOT carry it directly anymore.
    assert not list(bffi.objects(EXPECTED_WORK, V.BF.identifiedBy)), (
        "Work must not carry bf:identifiedBy post-P-45"
    )
    assert not list(bffi.objects(EXPECTED_EXPR, V.BF.identifiedBy)), (
        "Expression must not carry bf:identifiedBy post-P-45"
    )


def test_subject_with_authority_cross_link_resolves_to_authority_uri() -> None:
    """P-15: bf:Place with ``madsrdf:isIdentifiedByAuthority`` collapses to the
    authority URI as ``bffi:subject``, not the per-record raw URI.

    Reproduces the b26322791 case from the 2026-05-13 cataloguer audit:
    marc2bibframe2 emits 651 geographic subjects as ``bf:Place`` with a
    per-record raw URI plus a ``madsrdf:isIdentifiedByAuthority`` link
    to the cataloguer-supplied ``$0`` URI. Pre-fix M3 emitted the raw URI
    as ``bffi:subject`` and M9 re-reconciled from the literal label —
    binding the Swedish form to ``allars`` instead of ``yso``. Post-fix
    the YSO URI propagates directly so M9 sees the entity pre-bound and
    skips reconcile.
    """
    yso_italy = URIRef("http://www.yso.fi/onto/yso/p105111")
    raw_place_uri = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b26322791#Place651-54")
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:      <http://id.loc.gov/ontologies/bibframe/> .
            @prefix madsrdf: <http://www.loc.gov/mads/rdf/v1#> .
            @prefix rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Mafia-saga" ] ;
                bf:subject <{raw_place_uri}> .

            <{raw_place_uri}> a bf:Place ;
                rdfs:label "Italien" ;
                madsrdf:isIdentifiedByAuthority <{yso_italy}> .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    subjects = set(bffi.objects(EXPECTED_WORK, V.BFFI.subject))
    assert yso_italy in subjects, f"YSO URI missing from bffi:subject: {subjects}"
    assert raw_place_uri not in subjects, (
        f"raw bf:Place URI leaked into bffi:subject (P-15 fix did not apply): {subjects}"
    )


def test_subject_without_authority_cross_link_falls_back_to_bf_subject_uri() -> None:
    """P-15: subjects WITHOUT a ``madsrdf:isIdentifiedByAuthority`` link
    continue to use the bf:subject URI as the ``bffi:subject`` value.

    Pre-existing behaviour is preserved for ``bf:Topic`` (650 topical
    subjects), which marc2bibframe2 emits with the YSO URI directly as
    ``rdf:about`` and no separate authority cross-link. The COALESCE
    path's else-branch covers this case.
    """
    yso_topic = URIRef("http://www.yso.fi/onto/yso/p19771")  # religionspsykologia
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Religionspsykologi" ] ;
                bf:subject <{yso_topic}> .

            <{yso_topic}> a bf:Topic ;
                rdfs:label "religionspsykologia" .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    subjects = set(bffi.objects(EXPECTED_WORK, V.BFFI.subject))
    assert yso_topic in subjects, f"bf:Topic URI missing from bffi:subject: {subjects}"


def test_construct_routes_subject_label_for_local_authority() -> None:
    """P-36 Phase C: 650 subjects without $0 (local Topic nodes carrying
    ``rdfs:label`` in marc2bibframe2 output) must round-trip their
    cataloguer-supplied label through M3's CONSTRUCT so M9's
    ``_iter_subject_requests`` can build a candidate.

    Pre-fix: 18,928 ``bffi:subject`` targets on the helmet-5k-clean-full
    bench (run ``02924cb38191``) carried zero ``rdfs:label`` triples;
    M9 silently dropped every one. Same bug shape as the agent-label
    bug fixed at ``d040a90``.
    """
    local_topic = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b10189452#Topic650-23")
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Viulukoulu" ] ;
                bf:subject <{local_topic}> .

            <{local_topic}> a bf:Topic ;
                rdfs:label "viulu" .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    subjects = set(bffi.objects(EXPECTED_WORK, V.BFFI.subject))
    assert local_topic in subjects, f"local Topic missing from bffi:subject: {subjects}"
    labels = set(bffi.objects(local_topic, V.RDFS.label))
    assert Literal("viulu") in labels, (
        f"expected the cataloguer-supplied label to round-trip onto the "
        f"local Topic URI; got labels={labels}"
    )


def test_construct_does_not_route_subject_label_for_authority_uri_subjects() -> None:
    """P-36 Phase C + R7: when the COALESCE picks an authority URI as
    ``bffi:subject`` (the P-15 path — bf:Place / bf:Person /
    bf:Organization with ``madsrdf:isIdentifiedByAuthority``), the
    authority URI must NOT carry an ``rdfs:label`` triple in the BFFI
    output. Skosmos resolves authority labels from the loaded Finto
    graphs at render time; labeling YSO/KANTO URIs in our local graph
    would clash with Finto's authoritative form.

    The CONSTRUCT routes the label onto ``?bfSubject`` (the source raw
    URI) rather than ``?subject`` (the COALESCE result), so the
    authority URI stays label-free. The raw URI carries the label as
    an orphan triple — harmless because nothing references the raw
    URI in the BFFI output (``bffi:subject`` points at the authority
    URI).
    """
    yso_italy = URIRef("http://www.yso.fi/onto/yso/p105111")
    raw_place_uri = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b26322791#Place651-54")
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:      <http://id.loc.gov/ontologies/bibframe/> .
            @prefix madsrdf: <http://www.loc.gov/mads/rdf/v1#> .
            @prefix rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Mafia-saga" ] ;
                bf:subject <{raw_place_uri}> .

            <{raw_place_uri}> a bf:Place ;
                rdfs:label "Italien" ;
                madsrdf:isIdentifiedByAuthority <{yso_italy}> .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    auth_labels = set(bffi.objects(yso_italy, V.RDFS.label))
    assert not auth_labels, (
        f"authority URI {yso_italy} should not carry rdfs:label in the BFFI "
        f"output (Skosmos resolves authority labels from Finto); got {auth_labels}"
    )


def test_construct_extracts_music_medium_from_marc_382_ensemble() -> None:
    """MARC 382 → ``bf:ensemble`` → ``bf:mediumComponent`` →
    ``bf:mediumOfPerformance`` → ``rdfs:label``. marc2bibframe2 emits a
    three-level nested blank-node structure for 382; M3 flattens it to
    a single ``bffi:musicMedium`` link with the reconcilable label.

    Mints a local URI ``<record-root>#MusicMedium382-<sha1(label)>``
    because the source nodes are bnodes (no #MusicMedium382-N fragments
    the way 6XX subjects get #Topic650-N URIs from marc2bibframe2)."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Kantelekonsertti" ] ;
                bf:ensemble [
                    a bf:Ensemble ;
                    bf:mediumComponent [
                        a bf:MediumComponent ;
                        bf:mediumOfPerformance [
                            a bf:MediumOfPerformance ;
                            rdfs:label "kantele" ;
                            bf:source <http://id.loc.gov/authorities/performanceMediums>
                        ]
                    ]
                ] .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    media = list(bffi.objects(EXPECTED_WORK, V.BFFI.musicMedium))
    assert len(media) == 1, f"expected exactly one bffi:musicMedium link; got {media}"
    medium = media[0]
    assert isinstance(medium, URIRef), (
        f"expected a minted URI (not a blank node); got {type(medium).__name__}"
    )
    # The URI must follow ``<bib:raw/.../#MusicMedium382-<hash>>`` shape.
    assert "#MusicMedium382-" in str(medium), (
        f"expected #MusicMedium382-<hash> fragment; got {medium}"
    )
    # The bib_id prefix from the source Work URI carries through.
    assert str(medium).startswith("http://urn.fi/URN:NBN:fi:bib:raw/10000001"), (
        f"music-medium URI must inherit the source Work's record root; got {medium}"
    )
    # Type + label + source round-trip onto the minted URI.
    assert (medium, RDF.type, V.BFFI.MusicMedium) in bffi
    assert Literal("kantele") in set(bffi.objects(medium, V.RDFS.label))
    sources = set(bffi.objects(medium, V.BF.source))
    assert any("performanceMediums" in str(s) for s in sources), (
        f"expected bf:source to round-trip with the LCMPT base URI; got {sources}"
    )


def test_construct_music_medium_coalesces_same_label_on_one_work() -> None:
    """Two 382 fields on the same Work with the same label (e.g. cataloguer
    duplicates the entry) should mint the SAME ``bffi:musicMedium`` URI so
    the BFFI graph has one node per distinct instrument, not two."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Pianokvartet" ] ;
                bf:ensemble [
                    a bf:Ensemble ;
                    bf:mediumComponent [
                        bf:mediumOfPerformance [ rdfs:label "piano" ]
                    ]
                ] ;
                bf:ensemble [
                    a bf:Ensemble ;
                    bf:mediumComponent [
                        bf:mediumOfPerformance [ rdfs:label "piano" ]
                    ]
                ] .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    media = set(bffi.objects(EXPECTED_WORK, V.BFFI.musicMedium))
    assert len(media) == 1, (
        f"expected the two duplicate 'piano' entries to coalesce into one "
        f"bffi:musicMedium URI; got {media}"
    )


def test_construct_extracts_intended_audience_from_marc_385() -> None:
    """MARC 385 → ``bf:intendedAudience`` → ``bf:IntendedAudience`` →
    ``rdfs:label``. marc2bibframe2 emits a flat predicate; M3 mints
    a local ``#IntendedAudience385-<sha1(label)>`` URI so the resolver
    has a stable target for the audience reconciliation."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Lasten kuvakirja" ] ;
                bf:intendedAudience [
                    a bf:IntendedAudience ;
                    rdfs:label "lapset" ;
                    bf:source <http://urn.fi/URN:NBN:fi:au:yso>
                ] .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    audiences = list(bffi.objects(EXPECTED_WORK, V.BFFI.intendedAudience))
    assert len(audiences) == 1, f"expected one bffi:intendedAudience; got {audiences}"
    audience = audiences[0]
    assert isinstance(audience, URIRef)
    assert "#IntendedAudience385-" in str(audience)
    assert str(audience).startswith("http://urn.fi/URN:NBN:fi:bib:raw/10000001")
    assert (audience, RDF.type, V.BFFI.IntendedAudience) in bffi
    assert Literal("lapset") in set(bffi.objects(audience, V.RDFS.label))


def test_construct_extracts_creator_characteristic_from_marc_386() -> None:
    """MARC 386 → ``bflc:creatorCharacteristic`` (note BFLC, not
    BIBFRAME) → CreatorCharacteristic blank node with label + source.
    Same flatten-and-mint pattern as 385."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix bflc: <http://id.loc.gov/ontologies/bflc/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Naiskirjailijoiden antologia" ] ;
                bflc:creatorCharacteristic [
                    a bflc:CreatorCharacteristic ;
                    rdfs:label "naiset" ;
                    bf:source <http://urn.fi/URN:NBN:fi:au:yso>
                ] .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    characteristics = list(bffi.objects(EXPECTED_WORK, V.BFFI.creatorCharacteristic))
    assert len(characteristics) == 1, (
        f"expected one bffi:creatorCharacteristic; got {characteristics}"
    )
    cc = characteristics[0]
    assert isinstance(cc, URIRef)
    assert "#CreatorCharacteristic386-" in str(cc)
    assert (cc, RDF.type, V.BFFI.CreatorCharacteristic) in bffi
    assert Literal("naiset") in set(bffi.objects(cc, V.RDFS.label))


def test_construct_extracts_origin_place_from_marc_257_via_instance() -> None:
    """MARC 257 (Country of Producing Entity) → marc2bibframe2 emits
    ``bf:originPlace`` on the ``bf:Instance``, NOT the Work (per the
    BIBFRAME three-class data model). M3 hoists the property from the
    Instance up to the Work via the inverse ``bf:instanceOf`` link
    because lkd.rdf's ``bffi:originPlace`` has ``rdfs:domain bffi:Work``.

    Tests the hoist by building a BIBFRAME graph where the Work has no
    direct ``bf:originPlace`` (just like real marc2bibframe2 output) —
    the property lives on the related Instance."""
    instance_uri = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/10000001#Instance")
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Past lives" ] .

            <{instance_uri}> a bf:Instance ;
                bf:instanceOf <{BF_WORK}> ;
                bf:originPlace [
                    a bf:Place ;
                    rdfs:label "Yhdysvallat" ;
                    bf:source <http://urn.fi/URN:NBN:fi:au:yso>
                ] .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    places = list(bffi.objects(EXPECTED_WORK, V.BFFI.originPlace))
    assert len(places) == 1, (
        f"expected one bffi:originPlace hoisted from the Instance; got {places}"
    )
    place = places[0]
    assert isinstance(place, URIRef)
    # Fragment scheme distinguishes 257 origins from 651 place subjects.
    assert "#OriginPlace257-" in str(place)
    assert str(place).startswith("http://urn.fi/URN:NBN:fi:bib:raw/10000001")
    assert (place, RDF.type, V.BFFI.Place) in bffi
    assert Literal("Yhdysvallat") in set(bffi.objects(place, V.RDFS.label))


def test_construct_extracts_multiple_origin_places_from_one_instance() -> None:
    """Cataloguers tag multiple 257 fields when an AV work has more than
    one producing country (Past Lives = US + South Korea). Each surfaces
    as its own ``bffi:originPlace`` link with a distinct minted URI
    (different label hashes)."""
    instance_uri = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/10000001#Instance")
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Past lives" ] .

            <{instance_uri}> a bf:Instance ;
                bf:instanceOf <{BF_WORK}> ;
                bf:originPlace [ a bf:Place ; rdfs:label "Yhdysvallat" ] ;
                bf:originPlace [ a bf:Place ; rdfs:label "Korean tasavalta" ] .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    places = list(bffi.objects(EXPECTED_WORK, V.BFFI.originPlace))
    assert len(places) == 2, f"expected two distinct originPlaces; got {places}"
    labels = {str(lab) for place in places for lab in bffi.objects(place, V.RDFS.label)}
    assert labels == {"Yhdysvallat", "Korean tasavalta"}


def test_construct_routes_genreform_label() -> None:
    """P-36 Phase C: bf:genreForm targets must round-trip their
    ``rdfs:label`` through M3's CONSTRUCT so M9 has something to walk.

    Pre-fix: 2,997 ``bffi:genreForm`` targets on the helmet-5k bench
    carried zero ``rdfs:label`` triples. Same routing bug as for
    subjects, fixed by the same single-triple inner OPTIONAL pattern.
    """
    genre_uri = URIRef("http://urn.fi/URN:NBN:fi:au:kaunokki:p10072")
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Sotaromaani" ] ;
                bf:genreForm <{genre_uri}> .

            <{genre_uri}> a bf:GenreForm ;
                rdfs:label "sotaromaanit" .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    genres = set(bffi.objects(EXPECTED_WORK, V.BFFI.genreForm))
    assert genre_uri in genres, f"genreForm URI missing from bffi:genreForm: {genres}"
    labels = set(bffi.objects(genre_uri, V.RDFS.label))
    assert Literal("sotaromaanit") in labels, (
        f"expected genreForm label to round-trip; got labels={labels}"
    )


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


def test_post_process_pre_tags_manifestation_pref_labels_with_primary_language() -> None:
    """M3 synthesises ``bffi:Manifestation skos:prefLabel`` by
    concatenating title + publication statement (e.g. ``"Sota ja
    rauha (Helsinki : Otava, 1923)"``). It's pipeline-generated, not
    cataloguer-typed — pre-tag with the record's primary language so
    the title-language LLM cascade doesn't fire on it."""
    source = _build_source()
    bffi = construct_bffi(source)
    post_process(bffi, source)
    manif_labels = [
        o for o in bffi.objects(EXPECTED_MANIF, V.SKOS.prefLabel) if isinstance(o, Literal)
    ]
    assert len(manif_labels) >= 1
    # Every Manifestation prefLabel carries the record's primary
    # language tag (here Finnish from the source bf:Work bf:language).
    # Pre-tagging ran before any other re-tag pass.
    for label in manif_labels:
        assert label.language == "fi", f"untagged Manifestation prefLabel survived: {label!r}"


def test_post_process_leaves_already_tagged_manifestation_pref_labels_alone() -> None:
    """Idempotent: if a Manifestation prefLabel already carries a
    language tag (e.g. from a re-run or upstream stage), don't
    re-tag it. The pre-tag pass only operates on untagged literals."""
    source = _build_source()
    bffi = construct_bffi(source)
    # Strip whatever the CONSTRUCT emitted and inject one Swedish-
    # tagged literal so we can verify it survives untouched.
    for existing in list(bffi.objects(EXPECTED_MANIF, V.SKOS.prefLabel)):
        bffi.remove((EXPECTED_MANIF, V.SKOS.prefLabel, existing))
    bffi.add(
        (
            EXPECTED_MANIF,
            V.SKOS.prefLabel,
            Literal("Sota ja rauha (Helsinki : Otava, 1912)", lang="sv"),
        )
    )
    post_process(bffi, source)
    manif_labels = [
        o for o in bffi.objects(EXPECTED_MANIF, V.SKOS.prefLabel) if isinstance(o, Literal)
    ]
    # Only the Swedish-tagged literal survives (the source's primary
    # language is Finnish but the existing tag must not be overwritten).
    assert len(manif_labels) == 1
    assert manif_labels[0].language == "sv"


def test_pref_label_tagged_via_single_declared_language_fast_path() -> None:
    """When MARC 041 declares a single language outside the Lingua-
    detectable set (here ``fre``→``fr``) and the title has no RDA
    parallel-title separator, tag the prefLabel with the cataloguer's
    declared BCP-47 code. No detection needed — the declaration is
    authoritative for mono-language titles."""
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
    assert label.language == "fr"


def test_pref_label_untagged_when_marc_language_code_unmapped() -> None:
    """A MARC 041 code outside ``_LANG_3_TO_2`` (e.g. an obscure code
    we haven't curated) still leaves the prefLabel untagged — we never
    invent a BCP-47 tag we don't trust."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
            <{BF_WORK}> a bf:Work ;
                bf:title [ bf:mainTitle "Klingon test title" ] ;
                bf:language <http://id.loc.gov/vocabulary/languages/tlh> .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    post_process(bffi, source)
    label = next(bffi.objects(EXPECTED_WORK, V.SKOS.prefLabel))
    assert isinstance(label, Literal)
    assert label.language is None


def test_pref_label_untagged_when_declared_language_is_parallel_title() -> None:
    """If the literal contains an RDA parallel-title separator the fast
    path must not fire — we don't have enough information to claim the
    *whole* string is in the single declared language. Falls back to
    Lingua detection, which (for declared-only languages like German)
    has no overlap with its supported set and emits nothing → label
    stays untagged."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
            <{BF_WORK}> a bf:Work ;
                bf:title [ bf:mainTitle "Buddenbrooks = Die Buddenbrooks" ] ;
                bf:language <http://id.loc.gov/vocabulary/languages/ger> .
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


def test_construct_mints_manifestation_linked_to_expression() -> None:
    """P-45 commit 2: the third pass mints a bffi:Manifestation per
    bf:Instance and links it to the bffi:Expression via
    bffi:expressionManifested. The Expression URI is computed from the
    same bf:Work hash that the Expression CONSTRUCT uses, so the
    three classes form a connected triangle:

        Work — hasExpression → Expression
         ↑                       ↑
         bf:instanceOf           bffi:expressionManifested
         (source side)           (BFFI side)
         ↑                       ↑
         bf:Instance — — minted into — — bffi:Manifestation
    """
    bffi = construct_bffi(_build_source())
    manifs = set(bffi.subjects(RDF.type, V.BFFI.Manifestation))
    assert manifs == {EXPECTED_MANIF}, f"expected one minted bffi:Manifestation; got {manifs}"
    # Expression-manifested link (Manifestation → Expression).
    assert (EXPECTED_MANIF, V.BFFI.expressionManifested, EXPECTED_EXPR) in bffi
    # The Work isn't directly linked TO the Manifestation; the relation
    # is mediated by Expression (FRBR semantics).
    assert not list(bffi.objects(EXPECTED_WORK, V.BFFI.expressionManifested))


def test_construct_synthesises_manifestation_pref_label_from_title_and_pub_year() -> None:
    """P-45: Manifestations need a ``skos:prefLabel`` or Skosmos
    renders them as the bare URI. M3 synthesises one from the
    bf:Instance's ``bf:title``/``bf:mainTitle`` and (when present)
    ``bf:publicationStatement`` so cataloguers see "Sota ja rauha
    (Helsinki : Otava, 1923)" instead of an opaque URN. Different
    publications of the same intellectual content get distinct
    Manifestation labels because the pub statement differs per
    edition."""
    bffi = construct_bffi(_build_source())
    labels = list(bffi.objects(EXPECTED_MANIF, V.SKOS.prefLabel))
    assert labels == [Literal("Sota ja rauha (Helsinki : Otava, 1923)")]


def test_construct_synthesises_manifestation_pref_label_without_pub_year() -> None:
    """When the bf:Instance has a title but no ``bf:publicationStatement``,
    the Manifestation label falls back to the title alone."""
    g = Graph()
    g.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:  <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

            <{BF_WORK}> a bf:Work ;
                bf:title [ a bf:Title ; bf:mainTitle "Sota ja rauha" ] .
            <{BF_INSTANCE}> a bf:Instance ;
                bf:instanceOf <{BF_WORK}> ;
                bf:title [ a bf:Title ; bf:mainTitle "Sota ja rauha" ] .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(g)
    labels = list(bffi.objects(EXPECTED_MANIF, V.SKOS.prefLabel))
    assert labels == [Literal("Sota ja rauha")]


def test_construct_forwards_marc_007_format_details_to_manifestation() -> None:
    """P-45 commit 17: marc2bibframe2 lifts MARC 007/04 → bf:digitalCharacteristic
    (DVD/Blu-ray distinction), MARC 007 sound positions →
    bf:soundCharacteristic (CD/vinyl, mono/stereo, etc.), and MARC
    007/03 video color → bf:colorContent. The Manifestation CONSTRUCT
    forwards all three onto BFFI so cataloguers can distinguish what
    the coarse RDA carrier collapses (``vd`` = videodisc covers both
    DVD and Blu-ray; ``sd`` = audio disc covers both CD and vinyl)."""
    bffi = construct_bffi(_build_source())
    digital = list(bffi.objects(EXPECTED_MANIF, V.BFFI.digitalCharacteristic))
    sound = list(bffi.objects(EXPECTED_MANIF, V.BFFI.soundCharacteristic))
    color = list(bffi.objects(EXPECTED_MANIF, V.BFFI.colorContent))
    assert digital == [URIRef("http://id.loc.gov/vocabulary/mencformat/dvdv")]
    assert sound == [URIRef("http://id.loc.gov/vocabulary/mrecmedium/opt")]
    assert color == [URIRef("http://id.loc.gov/vocabulary/mcolor/mul")]
    # Manifestation-only — Work and Expression must NOT carry these
    # (BFFI 1.0.0 ``rdfs:domain bffi:Manifestation``).
    for target in (EXPECTED_WORK, EXPECTED_EXPR):
        assert not list(bffi.objects(target, V.BFFI.digitalCharacteristic))
        assert not list(bffi.objects(target, V.BFFI.soundCharacteristic))
        assert not list(bffi.objects(target, V.BFFI.colorContent))


def test_construct_forwards_bf_media_and_bf_carrier_to_manifestation() -> None:
    """P-45 commit 4: marc2bibframe2 deterministically resolves MARC
    337$b → ``bf:media`` URI in ``id.loc.gov/vocabulary/mediaTypes/`` and
    MARC 338$b → ``bf:carrier`` URI in ``id.loc.gov/vocabulary/carriers/``.
    Both URIs are already canonical LoC RDA terms, so M3 just forwards
    them onto the Manifestation under their ``bffi:`` parallels. Skosmos
    renders them as labelled clickable concepts once the RDA-Media +
    RDA-Carrier graphs are loaded by ``load-finto`` (commit 3).

    The fixture ``bf:media .../mediaTypes/n`` = "unmediated" and
    ``bf:carrier .../carriers/nc`` = "volume" (the codes a print book
    carries in MARC 337$b ``n`` / 338$b ``nc``)."""
    bffi = construct_bffi(_build_source())
    media = list(bffi.objects(EXPECTED_MANIF, V.BFFI.media))
    carriers = list(bffi.objects(EXPECTED_MANIF, V.BFFI.carrier))
    assert media == [URIRef("http://id.loc.gov/vocabulary/mediaTypes/n")]
    assert carriers == [URIRef("http://id.loc.gov/vocabulary/carriers/nc")]
    # Work / Expression must NOT carry these — they're Manifestation-only
    # per the BFFI 1.0.0 ontology (lkd.rdf rdfs:domain = bffi:Manifestation).
    for target in (EXPECTED_WORK, EXPECTED_EXPR):
        assert not list(bffi.objects(target, V.BFFI.media))
        assert not list(bffi.objects(target, V.BFFI.carrier))


def test_construct_emits_sierra_style_dct_identifier_on_manifestation() -> None:
    """P-45 commit 2: ``dct:identifier`` moved from Work + Expression to
    Manifestation. M3's Manifestation CONSTRUCT denormalises the Helmet
    bib ID as a flat ``dct:identifier`` literal alongside the
    structured ``bf:identifiedBy`` block — Skosmos can't traverse the
    blank-node structure, so the flat predicate is the bib-number
    display form on Manifestation pages.

    Discoverable from Work / Expression via the
    Work → bffi:hasExpression → Expression ← bffi:expressionManifested
    ← Manifestation → dct:identifier walk.

    Fixture uses the bare numeric ``10000001`` as a stand-in; production
    data carries the full Sierra display form (``b<id><check>``).
    """
    source = _build_source()
    bffi = construct_bffi(source)
    idents = list(bffi.objects(EXPECTED_MANIF, DCTERMS.identifier))
    assert Literal("10000001") in idents, "Helmet bib id missing on Manifestation"
    # And confirm the Work / Expression do NOT carry it directly.
    for target in (EXPECTED_WORK, EXPECTED_EXPR):
        assert not list(bffi.objects(target, DCTERMS.identifier)), (
            f"dct:identifier must not be on {target} post-P-45"
        )


def test_construct_does_not_emit_dct_identifier_for_non_helmet_sources() -> None:
    """A ``bf:identifiedBy`` triple from a non-Helmet source must not produce a
    Sierra-style ``dct:identifier`` — that form is Helmet/Sierra-specific.

    The Manifestation CONSTRUCT's WHERE clause filters on
    ``?ident bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet>``, so
    non-Helmet identifiers don't bind ``?helmetBibIdLiteral`` and no
    ``dct:identifier`` triple is emitted on Manifestation either."""
    source = Graph()
    source.parse(
        data=textwrap.dedent(
            f"""
            @prefix bf:  <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

            <{BF_WORK}> a bf:Work ;
                bf:title         [ bf:mainTitle "Untitled" ] ;
                bf:identifiedBy  <#other-id> .

            <{BF_INSTANCE}> a bf:Instance ;
                bf:instanceOf <{BF_WORK}> .

            <#other-id> a bf:Local ;
                rdf:value "FOREIGN-42" ;
                bf:source <http://example.org/source/external> .
            """
        ).strip(),
        format="turtle",
    )
    bffi = construct_bffi(source)
    # Work + Manifestation both stay free of dct:identifier when the
    # source identifier isn't a Helmet bib_id.
    assert not list(bffi.objects(EXPECTED_WORK, DCTERMS.identifier))
    assert not list(bffi.objects(EXPECTED_MANIF, DCTERMS.identifier))


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


def test_construct_routes_uri_role_to_bffi_contribution() -> None:
    """P-36 Phase A: source MARC ``$4`` controlled relator →
    BIBFRAME ``bf:role <URI>``; M3's SPARQL CONSTRUCT carries that URI
    through to the bffi:Contribution it mints.

    Pre-Phase-A this was a Python post-process helper
    (``_propagate_non_primary_roles``); Phase A routes it via the inner
    OPTIONAL in ``bf_to_bffi_expression.rq``.
    """
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
    [contrib] = list(bffi.objects(EXPECTED_EXPR, V.BFFI.contribution))
    assert (
        contrib,
        V.BF.role,
        URIRef("http://id.loc.gov/vocabulary/relators/trl"),
    ) in bffi


def test_construct_routes_blank_node_role_label_with_typing() -> None:
    """P-36 Phase A: source MARC ``$e`` free-text → BIBFRAME blank-node
    ``bf:role`` with ``rdfs:label``; M3's SPARQL CONSTRUCT re-emits a
    fresh blank node typed ``bf:Role`` with the same label so Skosmos
    can render the cataloguer's Finnish role text on the canonical
    Expression."""
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
    [contrib] = list(bffi.objects(EXPECTED_EXPR, V.BFFI.contribution))
    [role] = list(bffi.objects(contrib, V.BF.role))
    # Role is a blank node typed bf:Role with the cataloguer's label
    assert (role, RDF.type, V.BF.Role) in bffi
    assert (role, V.RDFS.label, Literal("cembalo")) in bffi


def test_construct_routes_one_role_per_repeated_agent() -> None:
    """P-36 Phase A: cataloguer enters ``700 $a Hogwood, Christopher``
    three times, once per instrument. Source has 3 distinct
    bf:Contributions sharing one agent URI but each carrying a different
    role. M3's SPARQL CONSTRUCT routes one role per output contribution
    (no fan-out, no duplication) because the BIND(BNODE() AS ?otherContrib)
    fires once per (source ?c, ?otherRole) solution row."""
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


# --- P-19 corpus concat ---------------------------------------------------


def test_p19_write_bffi_corpus_concatenates_per_record_files(tmp_path: Path) -> None:
    """P-19 Phase A — _write_bffi_corpus collapses N per-record Turtle
    files into one stream parseable by rdflib in a single open()."""
    bffi_dir = tmp_path / "bffi"
    bffi_dir.mkdir()
    (bffi_dir / "a.ttl").write_text(
        "@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .\n"
        "@prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi#> .\n"
        "\n"
        "<http://example.invalid/a> a bffi:Work .\n",
        encoding="utf-8",
    )
    (bffi_dir / "b.ttl").write_text(
        "@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .\n"
        "@prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi#> .\n"
        "\n"
        "<http://example.invalid/b> a bffi:Work .\n",
        encoding="utf-8",
    )

    corpus = tmp_path / BFFI_CORPUS_FILENAME
    written = _write_bffi_corpus(bffi_dir, corpus)
    assert written == 2

    # Each per-record file's prefix block stays inline with its body
    # (one ``@prefix bf:`` per file). Pooling them at the top of the
    # corpus would silently corrupt the graph when different files
    # use the same prefix name (e.g. ``ns1:``) for different
    # namespaces — see ``test_p19_corpus_handles_conflicting_prefix_names``.
    body = corpus.read_text(encoding="utf-8")
    assert body.count("@prefix bf:") == 2
    assert body.count("@prefix bffi:") == 2
    # Both record bodies survive into the concat.
    assert "<http://example.invalid/a>" in body
    assert "<http://example.invalid/b>" in body
    # The concat parses as valid Turtle.
    g = Graph()
    g.parse(str(corpus), format="turtle")
    subjects = {str(s) for s in g.subjects()}
    assert "http://example.invalid/a" in subjects
    assert "http://example.invalid/b" in subjects


def test_p19_corpus_handles_conflicting_prefix_names(tmp_path: Path) -> None:
    """Regression guard for the bug observed in run 20260607-1049-3f262f:
    rdflib serialises a Graph with auto-assigned short prefix names
    (``ns1:``, ``ns2:``, etc.) — and the same namespace can be
    serialised under different prefix names by different files. If the
    corpus concat pools ``@prefix`` declarations at the top, the
    Turtle parser sees conflicting declarations for the same prefix
    name and the LAST one wins for the whole file. Bodies that meant
    to use ``ns1:`` for bflc get parsed as ``bffi-prov:`` (or
    vice-versa), silently rewriting predicate URIs across ~36% of
    records and breaking MARC 264 round-trip (everything dumped in
    ``$c``).

    This test simulates the failure mode: file A serialises bflc as
    ``ns1:``; file B serialises bflc as ``ns2:`` (and bffi-prov as
    ``ns1:``). After concat, both files' ``ns1:simpleAgent`` triples
    must still resolve to bflc:simpleAgent in their respective
    contexts.
    """
    bffi_dir = tmp_path / "bffi"
    bffi_dir.mkdir()
    (bffi_dir / "a.ttl").write_text(
        "@prefix ns1: <http://id.loc.gov/ontologies/bflc/> .\n"
        "@prefix ns2: <http://urn.fi/URN:NBN:fi:schema:bffi-prov#> .\n"
        "\n"
        '<http://example.invalid/a> ns1:simpleAgent "PublisherA" .\n',
        encoding="utf-8",
    )
    (bffi_dir / "b.ttl").write_text(
        "@prefix ns1: <http://urn.fi/URN:NBN:fi:schema:bffi-prov#> .\n"
        "@prefix ns2: <http://id.loc.gov/ontologies/bflc/> .\n"
        "\n"
        '<http://example.invalid/b> ns2:simpleAgent "PublisherB" .\n',
        encoding="utf-8",
    )

    corpus = tmp_path / BFFI_CORPUS_FILENAME
    _write_bffi_corpus(bffi_dir, corpus)

    g = Graph()
    g.parse(str(corpus), format="turtle")

    bflc_simple_agent = URIRef("http://id.loc.gov/ontologies/bflc/simpleAgent")
    bffi_prov_simple_agent = URIRef("http://urn.fi/URN:NBN:fi:schema:bffi-prov#simpleAgent")

    # Both records should resolve to bflc:simpleAgent — the
    # cataloguer's intent in each per-record file.
    a_values = {str(o) for o in g.objects(URIRef("http://example.invalid/a"), bflc_simple_agent)}
    b_values = {str(o) for o in g.objects(URIRef("http://example.invalid/b"), bflc_simple_agent)}
    assert a_values == {"PublisherA"}, a_values
    assert b_values == {"PublisherB"}, b_values

    # Neither record should leak into bffi-prov:simpleAgent.
    assert not any(g.triples((None, bffi_prov_simple_agent, Literal("PublisherA"))))
    assert not any(g.triples((None, bffi_prov_simple_agent, Literal("PublisherB"))))


def test_p19_write_bffi_corpus_is_idempotent_when_fresh(tmp_path: Path) -> None:
    """P-19 Phase A — re-running the concat after a no-op M3 run
    (everything idempotent-skipped) is a fast no-op: when the
    existing concat is at least as new as every per-record .ttl, the
    helper returns 0 without rewriting.
    """
    bffi_dir = tmp_path / "bffi"
    bffi_dir.mkdir()
    (bffi_dir / "a.ttl").write_text(
        "@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .\n<http://x/a> a bf:Work .\n",
        encoding="utf-8",
    )
    corpus = tmp_path / "bffi-corpus.ttl"

    first = _write_bffi_corpus(bffi_dir, corpus)
    assert first == 1

    # Bump concat mtime past every per-record .ttl so the freshness
    # check unambiguously declines the second rewrite even on a
    # filesystem with coarse mtime resolution.
    later = time.time() + 5
    os.utime(corpus, (later, later))
    bumped_mtime = corpus.stat().st_mtime

    second = _write_bffi_corpus(bffi_dir, corpus)
    assert second == 0
    # The bumped mtime survives — the helper short-circuited and
    # never rewrote.
    assert corpus.stat().st_mtime == bumped_mtime


# --- _emit_validation_tsv ---------------------------------------------------


def test_validation_tsv_extracts_sh_message_from_report(tmp_path: Path) -> None:
    """The TSV's middle column carries the human-readable ``sh:message``
    text extracted from rdflib's SHACL report — not the verbose
    rdflib boilerplate. Cataloguers can sort + filter on the actual
    violation cause."""
    rdflib_report = (
        "Validation Report\n"
        "Conforms: False\n"
        "Results (1):\n"
        "Constraint Violation in MinCountConstraintComponent "
        "(http://www.w3.org/ns/shacl#MinCountConstraintComponent):\n"
        "\tSeverity: sh:Violation\n"
        '\tSource Shape: [ sh:message Literal("bffi:Work must have '
        'skos:prefLabel in fi/sv/en.") ; sh:minCount Literal("1") ]\n'
        "\tFocus Node: <http://urn.fi/URN:NBN:fi:bib:work:abc>\n"
        "\tResult Path: skos:prefLabel"
    )
    rows = [
        ValidationRow(
            helmet_bib_id="b1234",
            output_file="b1234.ttl",
            conforms=False,
            report_text=rdflib_report,
            run_uuid="r1",
        )
    ]
    path = tmp_path / "_validation.tsv"
    _emit_validation_tsv(path, rows)
    lines = path.read_text().splitlines()
    assert lines[0] == "helmet_bib_id\tshape_message\toutput_file"
    bib_id, message, output_file = lines[1].split("\t")
    assert bib_id == "b1234"
    assert message == "bffi:Work must have skos:prefLabel in fi/sv/en."
    assert output_file == "b1234.ttl"


def test_validation_tsv_joins_multiple_violations_per_record(tmp_path: Path) -> None:
    """Two violations on one record produce one TSV row with both
    messages joined by ``" | "``."""
    rdflib_report = (
        "Validation Report\nConforms: False\nResults (2):\n"
        'Source Shape: [ sh:message Literal("violation A") ]\n'
        'Source Shape: [ sh:message Literal("violation B") ]\n'
    )
    rows = [
        ValidationRow(
            helmet_bib_id="b1",
            output_file="b1.ttl",
            conforms=False,
            report_text=rdflib_report,
            run_uuid="r",
        )
    ]
    path = tmp_path / "_validation.tsv"
    _emit_validation_tsv(path, rows)
    message_col = path.read_text().splitlines()[1].split("\t")[1]
    assert message_col == "violation A | violation B"


def test_validation_tsv_truncates_long_extracted_message(tmp_path: Path) -> None:
    """A 1000-char ``sh:message`` gets truncated with an ellipsis;
    full report stays in the JSONL companion."""
    rdflib_report = f'Source Shape: [ sh:message Literal("{"x" * 1000}") ]'
    rows = [
        ValidationRow(
            helmet_bib_id="b1",
            output_file="b1.ttl",
            conforms=False,
            report_text=rdflib_report,
            run_uuid="r",
        )
    ]
    path = tmp_path / "_validation.tsv"
    _emit_validation_tsv(path, rows)
    message_col = path.read_text().splitlines()[1].split("\t")[1]
    assert len(message_col) < 1000
    assert message_col.endswith("…")


def test_validation_tsv_falls_back_to_full_report_when_no_sh_message(tmp_path: Path) -> None:
    """When the SHACL report has no ``sh:message Literal("…")`` clause
    (rare but possible with constraint components that don't carry
    one), the TSV falls back to the truncated full report rather
    than emitting an empty middle column."""
    rdflib_report = "Validation Report\nConforms: False\nResults (1):\nSome obscure failure"
    rows = [
        ValidationRow(
            helmet_bib_id="b1",
            output_file="b1.ttl",
            conforms=False,
            report_text=rdflib_report,
            run_uuid="r",
        )
    ]
    path = tmp_path / "_validation.tsv"
    _emit_validation_tsv(path, rows)
    message_col = path.read_text().splitlines()[1].split("\t")[1]
    assert message_col != ""
    assert "Some obscure failure" in message_col


def test_validation_tsv_is_header_only_when_no_failures(tmp_path: Path) -> None:
    """Always-emit invariant: even when every record passed shape
    validation, the TSV is written with just the header. Cataloguer
    workflows wired to the artifact path never see a missing file."""
    path = tmp_path / "_validation.tsv"
    _emit_validation_tsv(path, [])
    assert path.read_text() == "helmet_bib_id\tshape_message\toutput_file\n"
