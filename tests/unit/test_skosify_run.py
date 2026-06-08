"""Unit tests for stages/skosify_run (M10 phase 1).

Runs the real ``skosify.skosify`` (cheap; no network, no LLM) over a
tiny synthetic canonical Turtle to verify the dual-typing behaviour
the spec § 5 overlay-plus-inference approach commits to. The
config + overlay paths are committed in the repo and used as-is by
the test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m10.skosify_run import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OVERLAY_PATH,
    SKOSIFIED_FILENAME,
    run,
)

# --- Fixtures -------------------------------------------------------------


WORK = "http://urn.fi/URN:NBN:fi:bib:work:abc"
EXPR = "http://urn.fi/URN:NBN:fi:bib:expression:abc"
ADMIN = "http://urn.fi/URN:NBN:fi:bib:adminmeta/1"


def _build_canonical_graph(*, with_admin: bool = True) -> Graph:
    g = Graph()
    work = URIRef(WORK)
    expr = URIRef(EXPR)
    admin = URIRef(ADMIN)

    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Sota ja rauha", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))

    if with_admin:
        g.add((work, V.adminMetadata, admin))
        g.add((admin, RDF.type, V.AdminMetadata))
        g.add((admin, V.adminMetadataFor, work))
        g.add((admin, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    return g


@pytest.fixture
def canonical_path(tmp_path: Path) -> Path:
    path = tmp_path / "canonical.ttl"
    _build_canonical_graph().serialize(destination=str(path), format="turtle")
    return path


# --- Skosify dual-typing --------------------------------------------------


def test_skosify_run_dual_types_works_as_skos_concept(canonical_path: Path, tmp_path: Path) -> None:
    output = tmp_path / "skosified.ttl"
    result = run(canonical_path, output_path=output)

    assert result.skipped_idempotent is False
    assert output.is_file()
    assert result.dual_typed_works == 1

    g = Graph()
    g.parse(str(output), format="turtle")
    work = URIRef(WORK)
    types = set(g.objects(work, RDF.type))
    assert V.BFFI.Work in types  # BFFI typing preserved
    assert V.SKOS.Concept in types  # SKOS typing inferred via overlay


def test_skosify_run_dual_types_expressions_as_skos_concept(
    canonical_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "skosified.ttl"
    result = run(canonical_path, output_path=output)
    assert result.dual_typed_expressions == 1

    g = Graph()
    g.parse(str(output), format="turtle")
    expr = URIRef(EXPR)
    types = set(g.objects(expr, RDF.type))
    assert V.BFFI.Expression in types
    assert V.SKOS.Concept in types


def test_skosify_run_lifts_has_expression_to_skos_narrower(
    canonical_path: Path, tmp_path: Path
) -> None:
    """spec § 5: ``bffi:hasExpression rdfs:subPropertyOf skos:narrower``."""
    output = tmp_path / "skosified.ttl"
    result = run(canonical_path, output_path=output)
    assert result.inferred_narrower >= 1
    assert result.inferred_broader >= 1

    g = Graph()
    g.parse(str(output), format="turtle")
    work = URIRef(WORK)
    expr = URIRef(EXPR)
    # Both the original BFFI predicate AND the inferred SKOS predicate
    # must be present — the overlay does not destroy the BFFI side.
    assert (work, V.BFFI.hasExpression, expr) in g
    assert (work, V.SKOS.narrower, expr) in g
    assert (expr, V.BFFI.expressionOf, work) in g
    assert (expr, V.SKOS.broader, work) in g


def test_skosify_run_preserves_admin_metadata_block(canonical_path: Path, tmp_path: Path) -> None:
    """The cleanup_* options in bffi.cfg are off; AdminMetadata must survive."""
    output = tmp_path / "skosified.ttl"
    run(canonical_path, output_path=output)

    g = Graph()
    g.parse(str(output), format="turtle")
    work = URIRef(WORK)
    admin_blocks = list(g.objects(work, V.adminMetadata))
    assert len(admin_blocks) == 1
    block = admin_blocks[0]
    assert any(g.triples((block, V.descriptionAuthentication, V.AUTH_AUTO_MERGED)))


def test_skosify_run_uses_committed_overlay_and_config_paths(
    canonical_path: Path, tmp_path: Path
) -> None:
    """When --overlay-path / --config-path aren't passed, defaults apply."""
    output = tmp_path / "skosified.ttl"
    result = run(canonical_path, output_path=output)
    assert result.dual_typed_works == 1
    # Sanity: the constants point at real files.
    assert DEFAULT_OVERLAY_PATH.is_file()
    assert DEFAULT_CONFIG_PATH.is_file()


# --- Display-predicate synthesis (P-45) ---------------------------------


def test_skosify_run_synthesises_dct_creator_for_primary_contribution(tmp_path: Path) -> None:
    """BFFI 1.0.0 has no flat creator predicate — only the structured
    ``bffi:contribution → bffi:PrimaryContribution → bffi:agent`` chain.
    Skosify-time synthesis adds ``dct:creator`` on the Work pointing at
    the agent, so Skosmos's default template can render the author
    without traversing the blank-node chain. Canonical.ttl carries
    only the BFFI shape; the dct:* triple lives only in the
    Skosify-loaded output."""
    AGENT = URIRef("urn:agent/tolstoy")
    g = Graph()
    work = URIRef(WORK)
    expr = URIRef(EXPR)
    contrib = BNode()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Sota ja rauha", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib, V.BFFI.agent, AGENT))
    g.add((AGENT, V.RDFS.label, Literal("Tolstoy, Leo")))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")

    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    # Source canonical: NO dct:creator.
    assert (work, DCTERMS.creator, AGENT) not in g
    # Skosify output: dct:creator IS present.
    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, DCTERMS.creator, AGENT) in skosified
    # The structured BFFI chain is preserved alongside (blank-node IDs
    # are rewritten on parse-round-trip, so look up by agent).
    chain_agents = {
        a
        for c in skosified.objects(work, V.BFFI.contribution)
        for a in skosified.objects(c, V.BFFI.agent)
    }
    assert AGENT in chain_agents


def test_skosify_run_synthesises_dct_contributor_for_non_primary_contribution(
    tmp_path: Path,
) -> None:
    """Non-primary contributions (translators / illustrators / performers)
    on an Expression get a flat ``dct:contributor`` triple at Skosify
    time. The BFFI source has only a ``bffi:Contribution`` node (no
    ``bffi:PrimaryContribution`` typing)."""
    AGENT = URIRef("urn:agent/adrian-translator")
    g = Graph()
    work = URIRef(WORK)
    expr = URIRef(EXPR)
    contrib = BNode()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Sota ja rauha", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((expr, V.BFFI.contribution, contrib))
    # Note: NOT typed PrimaryContribution — base Contribution only.
    g.add((contrib, RDF.type, V.BFFI.Contribution))
    g.add((contrib, V.BFFI.agent, AGENT))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")

    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    # Non-primary → dct:contributor (NOT dct:creator).
    assert (expr, DCTERMS.contributor, AGENT) in skosified
    assert (expr, DCTERMS.creator, AGENT) not in skosified


# --- Display-flattener synthesis (Skosmos-improvement initiative) -------
#
# Skosmos's default concept-page template renders only direct outgoing
# triples on the subject. It does not walk blank-node chains or
# unlabeled predicates. The flatteners below lift one-hop structural
# data into flat predicates that the template DOES render. Every
# flattener emits ONLY into the Skosify-loaded artefact —
# canonical.ttl must stay BFFI 1.0.0-pure for the NLF-shippable
# surface. Each test verifies (a) the flat triple appears in the
# Skosify output and (b) the underlying BFFI structure survives.


def test_component_pref_labels_from_marckey_a_subfield(tmp_path: Path) -> None:
    """Aggregation-component Expressions carry ``bflc:marcKey`` but
    rarely a ``skos:prefLabel`` (M8 only labels canonical entities).
    The flattener parses ``$a`` from the marcKey and emits it as
    ``skos:prefLabel`` so Skosmos's navigation grid shows a title."""
    component = URIRef("http://urn.fi/URN:NBN:fi:bib:expression:component-1")
    g = Graph()
    g.add((component, RDF.type, V.BFFI.Expression))
    g.add((component, V.BFLC.marcKey, Literal("7300 $aFame /$gGore, Michael")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    pref_labels = set(skosified.objects(component, V.SKOS.prefLabel))
    assert Literal("Fame") in pref_labels
    # Original marcKey survives.
    assert (component, V.BFLC.marcKey, Literal("7300 $aFame /$gGore, Michael")) in skosified


def test_component_pref_label_skipped_when_already_present(tmp_path: Path) -> None:
    """If a component already has a ``skos:prefLabel`` from M8, the
    flattener leaves it alone — no duplicate / no overwrite."""
    component = URIRef("http://urn.fi/URN:NBN:fi:bib:expression:component-2")
    g = Graph()
    g.add((component, RDF.type, V.BFFI.Expression))
    g.add((component, V.BFLC.marcKey, Literal("7300 $aFame /$gGore, Michael")))
    g.add((component, V.SKOS.prefLabel, Literal("Picked Title")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    label_strings = {str(o) for o in skosified.objects(component, V.SKOS.prefLabel)}
    assert "Picked Title" in label_strings
    # The flattener did not duplicate the marcKey $a as a second
    # prefLabel — the pre-existing label suppressed the synthesis.
    assert "Fame" not in label_strings


def test_provision_activity_flattened_to_dct_publisher_date_spatial(tmp_path: Path) -> None:
    """``bffi:provisionActivity`` is a blank-node chain Skosmos cannot
    walk; the flattener lifts ``bflc:simpleAgent`` / ``simpleDate`` /
    ``simplePlace`` onto the parent Manifestation as ``dct:publisher``
    / ``dct:date`` / ``dct:spatial``."""
    manifestation = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m1")
    pa = BNode()
    g = Graph()
    g.add((manifestation, RDF.type, V.BFFI.Manifestation))
    g.add((manifestation, V.BFFI.provisionActivity, pa))
    g.add((pa, V.BFLC.simpleAgent, Literal("Wise Publications")))
    g.add((pa, V.BFLC.simpleDate, Literal("c1997")))
    g.add((pa, V.BFLC.simplePlace, Literal("London")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (manifestation, DCTERMS.publisher, Literal("Wise Publications")) in skosified
    assert (manifestation, DCTERMS.date, Literal("c1997")) in skosified
    assert (manifestation, DCTERMS.spatial, Literal("London")) in skosified


def test_title_variants_emitted_as_skos_alt_label(tmp_path: Path) -> None:
    """``bffi:title → bf:mainTitle`` variants that aren't the existing
    ``skos:prefLabel`` get lifted to ``skos:altLabel`` on the parent."""
    manifestation = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m2")
    title = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m2#title")
    g = Graph()
    g.add((manifestation, RDF.type, V.BFFI.Manifestation))
    g.add((manifestation, V.SKOS.prefLabel, Literal("Sota ja rauha")))
    g.add((manifestation, V.BFFI.title, title))
    g.add((title, V.BFFI.mainTitle, Literal("War and Peace")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    alt_labels = set(skosified.objects(manifestation, V.SKOS.altLabel))
    assert Literal("War and Peace") in alt_labels
    # The prefLabel must NOT be duplicated as altLabel.
    assert Literal("Sota ja rauha") not in alt_labels


def test_series_membership_emitted_as_dct_is_part_of(tmp_path: Path) -> None:
    """``bf:hasSeries`` resolves to a (often blank-node) Series; emit
    a flat ``dct:isPartOf`` so Skosmos shows the series row."""
    manifestation = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m3")
    series = BNode()
    g = Graph()
    g.add((manifestation, RDF.type, V.BFFI.Manifestation))
    g.add((manifestation, V.BF.hasSeries, series))
    g.add((series, RDF.type, V.BF.Series))
    g.add((series, V.RDFS.label, Literal("Tammen kultaiset kirjat")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    parts = set(skosified.objects(manifestation, DCTERMS.isPartOf))
    # The series node was a bnode; on round-trip the identity is
    # rewritten, so we look up by label-string match (skosify may
    # tag the label with a default-lang attribute).
    found = False
    for s in parts:
        label_strings = {str(o) for o in skosified.objects(s, V.RDFS.label)}
        if "Tammen kultaiset kirjat" in label_strings:
            found = True
            break
    assert found, f"dct:isPartOf → labeled Series not found; got {parts!r}"


def test_admin_metadata_highlights_emitted_on_parent(tmp_path: Path) -> None:
    """``dct:modified`` and ``bffi:descriptionConventions`` get lifted
    from the AdminMetadata block onto the parent Work."""
    work = URIRef(WORK)
    admin = URIRef(ADMIN)
    conv = URIRef("http://urn.fi/URN:NBN:fi:bib:desc-conv/bffi-1.0.0")
    g = _build_canonical_graph()  # already has work + admin chain
    g.add((admin, DCTERMS.modified, Literal("2026-06-08T00:00:00+00:00")))
    g.add((admin, V.descriptionConventions, conv))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, DCTERMS.modified, Literal("2026-06-08T00:00:00+00:00")) in skosified
    assert (work, DCTERMS.conformsTo, conv) in skosified


def test_role_predicates_lifted_from_contribution_blank_node(tmp_path: Path) -> None:
    """``?s bffi:contribution ?c . ?c bf:role ?role`` → emit
    ``?s bf:role ?role`` so Skosmos's concept page shows the role
    next to the dct:creator / dct:contributor rows."""
    work = URIRef(WORK)
    contrib = BNode()
    agent = URIRef("urn:agent/translator")
    role = URIRef("http://id.loc.gov/vocabulary/relators/trl")
    g = Graph()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Sota ja rauha", lang="fi")))
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.Contribution))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((contrib, V.BFFI.role, role))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, V.BFFI.role, role) in skosified


def test_role_mts_uri_enrichment_resolves_via_axis_collection(tmp_path: Path) -> None:
    """When a ``bffi:contribution`` carries a bnode role with a
    Finnish ``rdfs:label`` that matches an MTS concept in one of
    the four ``bffi:Role``-prescribed collections (m34 / m153 /
    m491 / m1157), the Skosify flattener emits a parallel
    ``bffi:role <mts:m...>`` triple on the contribution. The
    bnode form is preserved so the round-trip ``$e`` path stays
    intact.

    This is the canonical case for the role-redesign — the BFFI
    1.0.0 contract pins ``bffi:Role`` values to MTS, and Skosmos
    renders the MTS concept's Finnish / Swedish / English
    prefLabels natively from the loaded MTS graph.

    The fixture replicates the actual b10068004 shape: composer
    contribution on an Expression, ``"säveltäjä"`` cataloguer term.
    MTS' Work-axis ``m695`` is the canonical match (the term
    appears only in m34).
    """
    work = URIRef(WORK)
    expr = URIRef(EXPR)
    contrib = BNode()
    agent = URIRef("urn:agent/andersson")
    role_bnode = BNode()
    g = Graph()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Audition songs", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((expr, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.Contribution))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Andersson, Benny")))
    g.add((contrib, V.BFFI.role, role_bnode))
    g.add((role_bnode, RDF.type, V.BFFI.Role))
    g.add((role_bnode, V.RDFS.label, Literal("säveltäjä")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    # Find the contribution on the Expression (bnode IDs are
    # rewritten on parse-round-trip, so look up by agent identity).
    [emitted_contrib] = [
        c
        for c in skosified.objects(expr, V.BFFI.contribution)
        if agent in skosified.objects(c, V.BFFI.agent)
    ]
    role_uris = [
        r for r in skosified.objects(emitted_contrib, V.BFFI.role) if isinstance(r, URIRef)
    ]
    role_bnodes = [
        r for r in skosified.objects(emitted_contrib, V.BFFI.role) if not isinstance(r, URIRef)
    ]
    # The MTS Work-axis composer concept.
    assert URIRef("http://urn.fi/URN:NBN:fi:au:mts:m695") in role_uris
    # The cataloguer's bnode role survives alongside the URI.
    assert len(role_bnodes) == 1


def test_role_mts_uri_enrichment_silent_when_dump_missing(tmp_path: Path) -> None:
    """When the MTS dump path doesn't exist (CI environments,
    smoke tests), the flattener returns 0 matches and emits no
    URI triples. The bnode-with-label form remains untouched."""
    work = URIRef(WORK)
    contrib = BNode()
    role_bnode = BNode()
    g = Graph()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("X", lang="fi")))
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.Contribution))
    g.add((contrib, V.BFFI.role, role_bnode))
    g.add((role_bnode, V.RDFS.label, Literal("säveltäjä")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    # Point at a non-existent path so the flattener short-circuits.
    run(canonical, output_path=output, mts_dump_path=tmp_path / "no-such-mts.ttl")

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    [emitted_contrib] = list(skosified.objects(work, V.BFFI.contribution))
    role_uris = [
        r for r in skosified.objects(emitted_contrib, V.BFFI.role) if isinstance(r, URIRef)
    ]
    assert role_uris == []
    # bnode-with-label survives.
    role_bnodes = [
        r for r in skosified.objects(emitted_contrib, V.BFFI.role) if not isinstance(r, URIRef)
    ]
    assert len(role_bnodes) == 1


def test_contribution_label_composes_agent_and_role(tmp_path: Path) -> None:
    """The Tekijyys row in Skosmos shows a bnode value by default; emit
    ``"<agent-label> (<role-label>)"@fi`` on each ``bffi:contribution``
    bnode so the default template renders both pieces inline."""
    work = URIRef(WORK)
    contrib = BNode()
    agent = URIRef("urn:agent/andersson")
    g = Graph()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Audition songs for female singers")))
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.Contribution))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Andersson, Benny")))
    # marc2bibframe2-style free-text role bnode
    role_bnode = BNode()
    g.add((contrib, V.BFFI.role, role_bnode))
    g.add((role_bnode, RDF.type, V.BFFI.Role))
    g.add((role_bnode, V.RDFS.label, Literal("säveltäjä")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    [emitted_contrib] = list(skosified.objects(work, V.BFFI.contribution))
    pref_labels = list(skosified.objects(emitted_contrib, V.SKOS.prefLabel))
    assert any(
        str(lab) == "Andersson, Benny (säveltäjä)"
        and isinstance(lab, Literal)
        and lab.language == "fi"
        for lab in pref_labels
    ), f"expected fi-tagged composed prefLabel; got {pref_labels!r}"


def test_contribution_label_falls_back_to_agent_only_when_no_role(tmp_path: Path) -> None:
    """When the contribution has no ``bf:role``, the composed prefLabel
    is just the agent label (no trailing empty parentheses)."""
    work = URIRef(WORK)
    contrib = BNode()
    agent = URIRef("urn:agent/anon")
    g = Graph()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Untitled")))
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.Contribution))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Doe, Jane")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    [emitted_contrib] = list(skosified.objects(work, V.BFFI.contribution))
    label_strings = {str(lab) for lab in skosified.objects(emitted_contrib, V.SKOS.prefLabel)}
    assert "Doe, Jane" in label_strings
    # No "(...)" suffix when role is absent.
    assert not any("(" in s for s in label_strings)


def test_contribution_label_skipped_when_prefLabel_already_present(tmp_path: Path) -> None:
    """Idempotency: if a contribution already has a ``skos:prefLabel``
    (e.g. from an earlier pass or a hand-curated overlay), the
    flattener leaves it alone instead of accumulating duplicates."""
    work = URIRef(WORK)
    contrib = BNode()
    agent = URIRef("urn:agent/andersson")
    g = Graph()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Sota ja rauha")))
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.Contribution))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Andersson, Benny")))
    g.add((contrib, V.SKOS.prefLabel, Literal("Pre-existing label")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    [emitted_contrib] = list(skosified.objects(work, V.BFFI.contribution))
    label_strings = {str(lab) for lab in skosified.objects(emitted_contrib, V.SKOS.prefLabel)}
    assert label_strings == {"Pre-existing label"}


def test_contribution_label_prefers_catalogueur_text_over_uri_form_role(
    tmp_path: Path,
) -> None:
    """When both a URI relator and a bnode-with-label role coexist on
    the same Contribution (post-M3 enrichment shape), the composed
    prefLabel must use the cataloguer's free-text label — Finnish
    text matches the display language; the URI label is typically
    English."""
    work = URIRef(WORK)
    contrib = BNode()
    agent = URIRef("urn:agent/andersson")
    role_uri = URIRef("http://id.loc.gov/vocabulary/relators/cmp")
    role_bnode = BNode()
    g = Graph()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Audition songs")))
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.Contribution))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Andersson, Benny")))
    g.add((contrib, V.BFFI.role, role_uri))
    g.add((role_uri, V.RDFS.label, Literal("Composer")))  # English LoC label
    g.add((contrib, V.BFFI.role, role_bnode))
    g.add((role_bnode, V.RDFS.label, Literal("säveltäjä")))  # Cataloguer's Finnish

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    [emitted_contrib] = list(skosified.objects(work, V.BFFI.contribution))
    label_strings = {str(lab) for lab in skosified.objects(emitted_contrib, V.SKOS.prefLabel)}
    assert "Andersson, Benny (säveltäjä)" in label_strings
    assert "Andersson, Benny (Composer)" not in label_strings


def test_identifier_isbn_lifted_to_flat_bf_isbn(tmp_path: Path) -> None:
    """``bf:identifiedBy [ a bf:Isbn ; rdf:value "9789…" ]`` → emit a
    flat ``bf:isbn "9789…"`` on the Manifestation."""
    manifestation = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m4")
    idnode = BNode()
    g = Graph()
    g.add((manifestation, RDF.type, V.BFFI.Manifestation))
    g.add((manifestation, V.BFFI.identifiedBy, idnode))
    g.add((idnode, RDF.type, V.BF.Isbn))
    g.add((idnode, RDF.value, Literal("9789999999999")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (manifestation, V.BF.isbn, Literal("9789999999999")) in skosified


def test_identifier_local_bib_id_not_lifted_to_avoid_duplication(tmp_path: Path) -> None:
    """``bf:Local`` (Helmet bib ID) already surfaces as
    ``dct:identifier`` from the canonical Manifestation construct.
    The flattener must skip ``bf:Local`` to avoid duplicating the
    bib ID across two flat predicates on the same page."""
    manifestation = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m5")
    idnode = BNode()
    g = Graph()
    g.add((manifestation, RDF.type, V.BFFI.Manifestation))
    g.add((manifestation, V.BFFI.identifiedBy, idnode))
    g.add((idnode, RDF.type, V.BFFI.Local))
    g.add((idnode, RDF.value, Literal("b12345678")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    # No flat predicate emitted for Local (it isn't in the mapping
    # table).
    locals_flat = set(skosified.predicates(manifestation, Literal("b12345678")))
    assert V.BF.isbn not in locals_flat
    assert V.BF.issn not in locals_flat


# --- Round-trip guard: display predicates must not leak ------------------
#
# The marc-roundtrip cardinal rule (see docs/bffi_limitations.md): the
# converter MUST consult only ``bffi:`` / ``bf:`` structural predicates
# for bibliographic-content decisions. The Skosify-stage display
# flatteners emit Dublin Core (``dct:publisher`` / ``dct:date`` /
# ``dct:spatial`` / ``dct:isPartOf`` / ``dct:modified`` /
# ``dct:conformsTo``) and ``bf:isbn`` flat companions — all of which
# duplicate data that's already in the canonical BFFI chain. If the
# round-trip converter started reading these instead, it would
# silently consume display-layer denormalisations as the source of
# truth, defeating the round-trip's "BFFI is sufficient" verification.


def test_marc_roundtrip_converter_does_not_read_display_flatteners() -> None:
    """Guard: marc_roundtrip/converter.py must not reference the
    Skosify display flatteners as input. They're display-only and
    live exclusively in canonical-skosified.ttl, not canonical.ttl."""
    repo_root = Path(__file__).resolve().parents[2]
    converter = repo_root / "src" / "bffi_pipeline" / "marc_roundtrip" / "converter.py"
    src = converter.read_text(encoding="utf-8")
    # Strip comments + docstrings before scanning — references inside
    # documentation about the rule itself are fine.
    code_lines: list[str] = []
    in_doc = False
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_doc = not in_doc
            if stripped.count('"""') == 2 or stripped.count("'''") == 2:
                in_doc = False
            continue
        if in_doc:
            continue
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)

    forbidden = [
        r"DCTERMS\.publisher",
        r"DCTERMS\.spatial",
        r"DCTERMS\.isPartOf",
        r"DCTERMS\.conformsTo",
        # bf:isbn (flat) is a Skosify synthesis only.
        r"BF\.isbn\b",
        r"BF\.audioIssueNumber\b",
        r"BF\.systemNumber\b",
    ]
    for pat in forbidden:
        assert not re.search(pat, code), (
            f"marc_roundtrip/converter.py references display-only "
            f"predicate {pat!r}; the converter must only consult "
            f"BFFI-canonical structural predicates per the round-trip "
            f"cardinal rule (see docs/bffi_limitations.md)."
        )


# --- Idempotency ---------------------------------------------------------


def test_skosify_run_skips_when_output_is_newer_than_inputs(
    canonical_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "skosified.ttl"
    first = run(canonical_path, output_path=output)
    assert first.skipped_idempotent is False

    second = run(canonical_path, output_path=output)
    assert second.skipped_idempotent is True
    assert second.output_triples > 0  # summary still computed


def test_skosify_run_force_re_runs_even_when_output_is_fresh(
    canonical_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "skosified.ttl"
    run(canonical_path, output_path=output)
    forced = run(canonical_path, output_path=output, force=True)
    assert forced.skipped_idempotent is False


# --- Failure modes -------------------------------------------------------


def test_skosify_run_raises_when_canonical_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run(tmp_path / "missing.ttl", output_path=tmp_path / "out.ttl")


# --- Constants -----------------------------------------------------------


def test_skosified_filename_constant() -> None:
    assert SKOSIFIED_FILENAME == "canonical-skosified.ttl"
