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


def test_skosify_run_does_not_emit_entity_level_dct_creator(tmp_path: Path) -> None:
    """The previous ``dct:creator`` / ``dct:contributor`` entity-level
    mirrors created confusing parallel "Tekijä" + "Rooli" rows on the
    Skosmos concept page (one row per agent, separately one row per
    role, with no visual indication of which role belongs to which
    agent). The mirrors were dropped; the cataloguer sees a single
    ``bffi:contribution`` row whose label is composed as
    ``"<agent> (<role>)"`` by ``_synthesise_contribution_labels``."""
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

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    # The entity-level mirror is GONE — Skosmos now reads the composed
    # contribution label via the bnode prefLabel one hop deep.
    assert (work, DCTERMS.creator, AGENT) not in skosified
    # The structured BFFI chain is preserved alongside (blank-node IDs
    # are rewritten on parse-round-trip, so look up by agent).
    chain_agents = {
        a
        for c in skosified.objects(work, V.BFFI.contribution)
        for a in skosified.objects(c, V.BFFI.agent)
    }
    assert AGENT in chain_agents


def test_skosify_run_does_not_emit_entity_level_dct_contributor(
    tmp_path: Path,
) -> None:
    """Same rationale as the dct:creator mirror — non-primary
    contributions no longer emit ``dct:contributor`` on the entity.
    Skosmos reads the composed contribution label via the bnode."""
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
    g.add((contrib, RDF.type, V.BFFI.Contribution))
    g.add((contrib, V.BFFI.agent, AGENT))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")

    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (expr, DCTERMS.contributor, AGENT) not in skosified
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
    # The ProvisionActivity bnode itself gets a composed
    # ``skos:prefLabel`` so Skosmos renders the bnode as
    # "Place : Agent, Date" instead of an unresolvable genid link.
    pa_labels = [
        lbl
        for pa_bnode in skosified.objects(manifestation, V.BFFI.provisionActivity)
        if isinstance(pa_bnode, BNode)
        for lbl in skosified.objects(pa_bnode, V.SKOS.prefLabel)
    ]
    assert Literal("London : Wise Publications, c1997", lang="fi") in pa_labels


def test_provision_activity_label_skipped_when_all_parts_missing(tmp_path: Path) -> None:
    """When the ProvisionActivity bnode has no simple* values, no
    composed label is emitted (would be empty)."""
    manifestation = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m1")
    pa = BNode()
    g = Graph()
    g.add((manifestation, RDF.type, V.BFFI.Manifestation))
    g.add((manifestation, V.BFFI.provisionActivity, pa))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    pa_labels = [
        lbl
        for pa_bnode in skosified.objects(manifestation, V.BFFI.provisionActivity)
        if isinstance(pa_bnode, BNode)
        for lbl in skosified.objects(pa_bnode, V.SKOS.prefLabel)
    ]
    assert pa_labels == []


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


def test_admin_metadata_stays_on_admin_node(tmp_path: Path) -> None:
    """AdminMetadata fields stay on the ``bffi:AdminMetadata`` resource
    and surface via that resource's own Skosmos concept page — the
    parent Work / Expression / Manifestation page carries just the
    ``bffi:adminMetadata`` link to the admin block, not the flattened
    fields. Cataloguers click through "Hallinnolliset metatiedot" to
    see the full admin block; the per-axis page stays focused on
    bibliographic content."""
    work = URIRef(WORK)
    admin = URIRef(ADMIN)
    conv = URIRef("http://urn.fi/URN:NBN:fi:bib:desc-conv/bffi-1.0.0")
    auth = URIRef("http://urn.fi/URN:NBN:fi:bib:auth/auto-merged")
    level = URIRef("http://urn.fi/URN:NBN:fi:bib:desc-level/minimum")
    modifier = URIRef("http://urn.fi/URN:NBN:fi:bib:agent/marc2bibframe2")
    enc_level = URIRef("http://urn.fi/URN:NBN:fi:bib:enc-level/auto")
    gen_date = Literal("2026-06-09T06:53:20+00:00")
    g = _build_canonical_graph()  # already has work + admin chain
    g.add((admin, DCTERMS.modified, Literal("2026-06-08T00:00:00+00:00")))
    g.add((admin, V.descriptionConventions, conv))
    g.add((admin, V.descriptionAuthentication, auth))
    g.add((admin, V.descriptionLevel, level))
    g.add((admin, V.descriptionModifier, modifier))
    g.add((admin, V.encodingLevel, enc_level))
    g.add((admin, V.BFFI.generationDate, gen_date))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    # Admin fields remain on the AdminMetadata resource, NOT on the
    # parent Work. The parent keeps only the bffi:adminMetadata link.
    assert (work, V.BFFI.adminMetadata, admin) in skosified
    assert (admin, DCTERMS.modified, Literal("2026-06-08T00:00:00+00:00")) in skosified
    assert (admin, V.descriptionAuthentication, auth) in skosified
    assert (admin, V.descriptionLevel, level) in skosified
    assert (admin, V.descriptionModifier, modifier) in skosified
    assert (admin, V.encodingLevel, enc_level) in skosified
    assert (admin, V.BFFI.generationDate, gen_date) in skosified
    # Pre-Option-B flattening is gone — admin fields no longer
    # mirror onto the parent Work.
    assert (work, DCTERMS.modified, Literal("2026-06-08T00:00:00+00:00")) not in skosified
    assert (work, V.descriptionLevel, level) not in skosified
    assert (work, V.descriptionAuthentication, auth) not in skosified


def test_role_predicates_no_longer_lifted_to_entity_level(tmp_path: Path) -> None:
    """The role-on-entity mirror was dropped — it created a parallel
    "Rooli" row on the Skosmos concept page with no visual pairing to
    the corresponding agent. The role stays on the contribution
    bnode where ``_synthesise_contribution_labels`` composes
    ``"<agent> (<role>)"`` as the bnode's ``skos:prefLabel``,
    surfacing the pair as a single "Tekijyys" row."""
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
    assert (work, V.BFFI.role, role) not in skosified
    # Role stays on the bnode for the round-trip + contribution-label
    # composition.
    chain_roles = {
        r
        for c in skosified.objects(work, V.BFFI.contribution)
        for r in skosified.objects(c, V.BFFI.role)
    }
    assert role in chain_roles


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
    emitted_contrib = next(
        c for c in skosified.objects(work, V.BFFI.contribution) if isinstance(c, BNode)
    )
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
    emitted_contrib = next(
        c for c in skosified.objects(work, V.BFFI.contribution) if isinstance(c, BNode)
    )
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
    emitted_contrib = next(
        c for c in skosified.objects(work, V.BFFI.contribution) if isinstance(c, BNode)
    )
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
    emitted_contrib = next(
        c for c in skosified.objects(work, V.BFFI.contribution) if isinstance(c, BNode)
    )
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
    emitted_contrib = next(
        c for c in skosified.objects(work, V.BFFI.contribution) if isinstance(c, BNode)
    )
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


# --- LoC countries bridge (L-12) -----------------------------------------


def _write_loc_countries_bridge_fixture(path: Path, *, code: str = "fi") -> None:
    """Mini-bridge fixture with one country, one skos:exactMatch, three
    prefLabels — enough to exercise the materialiser."""
    path.write_text(
        f"""
@prefix loc:  <http://id.loc.gov/vocabulary/countries/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix yso:  <http://www.yso.fi/onto/yso/> .

loc:{code}  skos:exactMatch yso:p94426 ;
            skos:prefLabel  "Suomi"@fi, "Finland"@sv, "Finland"@en ;
            skos:notation   "{code}" .
""",
        encoding="utf-8",
    )


def test_country_labels_materialised_from_bridge_on_referenced_uri(tmp_path: Path) -> None:
    """A ``bf:place`` reference to a LoC country URI gets multilingual
    prefLabels (plus exactMatch + notation) materialised on the URI
    itself from the bridge file."""
    bridge_path = tmp_path / "bridge.ttl"
    _write_loc_countries_bridge_fixture(bridge_path)

    manifestation = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m1")
    country = URIRef("http://id.loc.gov/vocabulary/countries/fi")
    pa = BNode()
    g = Graph()
    g.add((manifestation, RDF.type, V.BFFI.Manifestation))
    g.add((manifestation, V.BFFI.provisionActivity, pa))
    g.add((pa, V.BF.place, country))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output, loc_countries_bridge_path=bridge_path)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    pref_labels = set(skosified.objects(country, V.SKOS.prefLabel))
    assert Literal("Suomi", lang="fi") in pref_labels
    assert Literal("Finland", lang="sv") in pref_labels
    assert Literal("Finland", lang="en") in pref_labels
    assert (
        country,
        V.SKOS.exactMatch,
        URIRef("http://www.yso.fi/onto/yso/p94426"),
    ) in skosified
    assert (country, V.SKOS.notation, Literal("fi")) in skosified


def test_country_labels_skipped_for_unknown_code(tmp_path: Path) -> None:
    """A country URI absent from the bridge file is left bare — no
    pref/exactMatch/notation triples appear on it."""
    bridge_path = tmp_path / "bridge.ttl"
    _write_loc_countries_bridge_fixture(bridge_path, code="fi")

    manifestation = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m1")
    # Reference a country NOT in the bridge fixture.
    country = URIRef("http://id.loc.gov/vocabulary/countries/zz")
    pa = BNode()
    g = Graph()
    g.add((manifestation, RDF.type, V.BFFI.Manifestation))
    g.add((manifestation, V.BFFI.provisionActivity, pa))
    g.add((pa, V.BF.place, country))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output, loc_countries_bridge_path=bridge_path)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert list(skosified.objects(country, V.SKOS.prefLabel)) == []


def test_subject_typing_propagated_via_exact_match(tmp_path: Path) -> None:
    """Per b19845637 1910-luku review: when a raw subject URI carries
    a routable ``rdf:type`` AND a ``skos:exactMatch`` to a YSO URI,
    the Skosify pass copies the type onto the YSO URI so the
    round-trip can route the 6XX tag from the YSO URI alone (M9
    rebinding strips the raw URI; the YSO URI is what survives onto
    the canonical Work's ``bffi:subject`` predicate)."""
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:work:abc")
    raw_temporal = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b19845637#Temporal648-29")
    yso_uri = URIRef("http://www.yso.fi/onto/yso/p6191061919")

    g = _build_canonical_graph()
    g.add((work, V.BFFI.subject, raw_temporal))
    g.add((work, V.BFFI.subject, yso_uri))
    g.add((raw_temporal, RDF.type, V.BFFI.Temporal))
    g.add((raw_temporal, V.RDFS.label, Literal("1910-luku")))
    g.add((raw_temporal, V.SKOS.exactMatch, yso_uri))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (yso_uri, RDF.type, V.BFFI.Temporal) in skosified


def test_typed_subject_display_temporal_emits_dct_temporal(tmp_path: Path) -> None:
    """A ``bffi:Temporal``-typed subject target mirrors onto
    ``dct:temporal`` on the parent, so Skosmos shows it under
    "Temporal subject" distinct from the generic ``bffi:subject``
    row."""
    work = URIRef(WORK)
    target = URIRef("http://www.yso.fi/onto/yso/p6191061919")
    g = _build_canonical_graph()
    g.add((work, V.BFFI.subject, target))
    g.add((target, RDF.type, V.BFFI.Temporal))
    g.add((target, V.SKOS.prefLabel, Literal("1910-luku", lang="fi")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, DCTERMS.temporal, target) in skosified
    # The original bffi:subject link stays — round-trip and queries
    # that walk bffi:subject still see the target.
    assert (work, V.BFFI.subject, target) in skosified


def test_typed_subject_display_place_emits_geographic_coverage(tmp_path: Path) -> None:
    """A ``bffi:Place``-typed subject target mirrors onto
    ``bffi:geographicCoverage`` on the parent."""
    work = URIRef(WORK)
    target = URIRef("http://www.yso.fi/onto/yso/p94426")
    g = _build_canonical_graph()
    g.add((work, V.BFFI.subject, target))
    g.add((target, RDF.type, V.BFFI.Place))
    g.add((target, V.SKOS.prefLabel, Literal("Suomi", lang="fi")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, V.BFFI.geographicCoverage, target) in skosified
    assert (work, V.BFFI.subject, target) in skosified


def test_redundant_raw_subject_references_are_pruned(tmp_path: Path) -> None:
    """When a Work has both ``bffi:subject <raw-bib-URI>`` AND
    ``bffi:subject <yso-URI>`` AND the raw URI ``skos:exactMatch``-es
    the YSO URI, the raw flat triple is pruned from Skosify output
    so Skosmos shows the subject only once via the authority URI."""
    work = URIRef(WORK)
    raw = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b00000001#Topic650-21")
    yso = URIRef("http://www.yso.fi/onto/yso/p2849")
    g = _build_canonical_graph()
    g.add((work, V.BFFI.subject, raw))
    g.add((work, V.BFFI.subject, yso))
    g.add((raw, RDF.type, V.BFFI.Topic))
    g.add((raw, V.SKOS.exactMatch, yso))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    # Raw triple pruned from the flat bffi:subject list.
    assert (work, V.BFFI.subject, raw) not in skosified
    # YSO triple retained.
    assert (work, V.BFFI.subject, yso) in skosified
    # exactMatch link survives.
    assert (raw, V.SKOS.exactMatch, yso) in skosified


def test_raw_subject_without_authority_twin_is_retained(tmp_path: Path) -> None:
    """When a Work has only the raw subject reference (no authority
    twin reconciled by M9), the raw triple stays — pruning would
    leave the entity with NO subject of that kind."""
    work = URIRef(WORK)
    raw = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b00000001#Topic650-22")
    g = _build_canonical_graph()
    g.add((work, V.BFFI.subject, raw))
    g.add((raw, RDF.type, V.BFFI.Topic))
    g.add((raw, V.RDFS.label, Literal("kissaliivit", lang="fi")))
    # NO skos:exactMatch; NO YSO URI on the work.

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, V.BFFI.subject, raw) in skosified


def test_prune_does_not_drop_raw_when_authority_twin_is_on_other_predicate(
    tmp_path: Path,
) -> None:
    """The prune is per-predicate: a raw subject under ``bffi:subject``
    is NOT dropped just because the same raw URI's exactMatch
    appears under ``bffi:genreForm`` on the same Work. The two are
    semantically distinct rows."""
    work = URIRef(WORK)
    raw = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b00000001#Topic650-23")
    yso = URIRef("http://www.yso.fi/onto/yso/p1234")
    g = _build_canonical_graph()
    g.add((work, V.BFFI.subject, raw))
    g.add((work, V.BFFI.genreForm, yso))  # authority twin on DIFFERENT predicate
    g.add((raw, V.SKOS.exactMatch, yso))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    # Raw triple retained because the authority twin isn't on the same predicate.
    assert (work, V.BFFI.subject, raw) in skosified


def test_typed_subject_display_topic_stays_under_subject_only(tmp_path: Path) -> None:
    """A ``bffi:Topic``-typed (or untyped) subject target does NOT
    mirror onto typed predicates — it stays under the catch-all
    ``bffi:subject``."""
    work = URIRef(WORK)
    target = URIRef("http://www.yso.fi/onto/yso/p2849")
    g = _build_canonical_graph()
    g.add((work, V.BFFI.subject, target))
    g.add((target, RDF.type, V.BFFI.Topic))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, V.BFFI.subject, target) in skosified
    assert (work, DCTERMS.temporal, target) not in skosified
    assert (work, V.BFFI.geographicCoverage, target) not in skosified


def test_classification_display_flattens_portion_with_source_code(tmp_path: Path) -> None:
    """``bffi:classification → bffi:classificationPortion`` + ``bf:source
    → bffi:code`` chain flattens to a parent-level ``bffi:classificationPortion
    "<portion> (<source>)"`` literal."""
    work = URIRef(WORK)
    cls = BNode()
    src = BNode()
    g = _build_canonical_graph()
    g.add((work, V.BFFI.classification, cls))
    g.add((cls, RDF.type, V.BFFI.Classification))
    g.add((cls, V.BFFI.classificationPortion, Literal("78")))
    g.add((cls, V.BF.source, src))
    g.add((src, V.BFFI.code, Literal("ykl")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, V.BFFI.classificationPortion, Literal("78 (ykl)")) in skosified


def test_classification_display_falls_back_to_portion_only(tmp_path: Path) -> None:
    """When the Classification bnode has no ``bf:source`` chain, the
    flattened literal is just the portion (no parenthetical)."""
    work = URIRef(WORK)
    cls = BNode()
    g = _build_canonical_graph()
    g.add((work, V.BFFI.classification, cls))
    g.add((cls, V.BFFI.classificationPortion, Literal("820-2")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, V.BFFI.classificationPortion, Literal("820-2")) in skosified


def test_note_display_mirrors_label_as_skos_prefLabel_on_bnode(tmp_path: Path) -> None:
    """``bffi:note → bnode → rdfs:label`` chain mirrors the label
    onto ``skos:prefLabel`` on the same bnode so Skosmos renders
    the bnode value with readable text instead of a genid 404
    link. The original ``rdfs:label`` stays untouched — round-trip
    readers still see it."""
    work = URIRef(WORK)
    note = BNode()
    g = _build_canonical_graph()
    g.add((work, V.BFFI.note, note))
    g.add((note, RDF.type, V.BFFI.Note))
    g.add((note, V.RDFS.label, Literal("Linkki verkkoaineistoon", lang="fi")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    note_prefLabels = [
        lbl
        for note_bnode in skosified.objects(work, V.BFFI.note)
        if isinstance(note_bnode, BNode)
        for lbl in skosified.objects(note_bnode, V.SKOS.prefLabel)
    ]
    assert Literal("Linkki verkkoaineistoon", lang="fi") in note_prefLabels
    # The original rdfs:label stays.
    note_rdfsLabels = [
        lbl
        for note_bnode in skosified.objects(work, V.BFFI.note)
        if isinstance(note_bnode, BNode)
        for lbl in skosified.objects(note_bnode, V.RDFS.label)
    ]
    assert Literal("Linkki verkkoaineistoon", lang="fi") in note_rdfsLabels


def test_empty_bnode_values_pruned_from_skosify_output(tmp_path: Path) -> None:
    """M3 SPARQL CONSTRUCTs with unbound right-hand variables emit
    empty blank nodes (``bffi:title [ ]``, ``bffi:note [ ]``,
    ``bffi:role [ ]``). Skosmos renders these as rows with a
    non-resolvable genid link that 404s. The prune pass removes the
    parent's reference to the empty bnode so the row disappears
    from Skosmos."""
    work = URIRef(WORK)
    empty_title = BNode()
    empty_note = BNode()
    populated_note = BNode()
    g = _build_canonical_graph()
    # Empty bnodes — no outgoing triples at all.
    g.add((work, V.BFFI.title, empty_title))
    g.add((work, V.BFFI.note, empty_note))
    # Populated bnode — has rdfs:label, should stay.
    g.add((work, V.BFFI.note, populated_note))
    g.add((populated_note, RDF.type, V.BFFI.Note))
    g.add((populated_note, V.RDFS.label, Literal("Actual note text", lang="fi")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    # Empty bnode references are gone.
    empty_titles = [n for n in skosified.objects(work, V.BFFI.title) if isinstance(n, BNode)]
    empty_notes = [
        n
        for n in skosified.objects(work, V.BFFI.note)
        if isinstance(n, BNode) and not any(skosified.predicate_objects(n))
    ]
    assert empty_titles == []
    assert empty_notes == []
    # Populated bnode survives (and its label/prefLabel chain is intact).
    populated_note_labels = [
        lbl
        for n in skosified.objects(work, V.BFFI.note)
        if isinstance(n, BNode)
        for lbl in skosified.objects(n, V.SKOS.prefLabel)
    ]
    assert Literal("Actual note text", lang="fi") in populated_note_labels


def test_bnode_prefLabel_mirror_covers_tableOfContents_and_extent(tmp_path: Path) -> None:
    """``bffi:tableOfContents`` and ``bffi:extent`` both point at
    bnodes that carry their content as ``rdfs:label``. The
    Skosmos-mirror pass copies that label onto ``skos:prefLabel``
    on the bnode so Skosmos shows readable text inline instead of a
    genid 404 link."""
    work = URIRef(WORK)
    toc = BNode()
    ext = BNode()
    g = _build_canonical_graph()
    g.add((work, V.BFFI.tableOfContents, toc))
    g.add((toc, RDF.type, V.BFFI.TableOfContents))
    g.add((toc, V.RDFS.label, Literal("Sisältö: 1. luku, 2. luku, 3. luku", lang="fi")))
    g.add((work, V.BFFI.extent, ext))
    g.add((ext, RDF.type, V.BFFI.Extent))
    g.add((ext, V.RDFS.label, Literal("1 äänilevy", lang="fi")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    toc_prefLabels = [
        lbl
        for n in skosified.objects(work, V.BFFI.tableOfContents)
        if isinstance(n, BNode)
        for lbl in skosified.objects(n, V.SKOS.prefLabel)
    ]
    ext_prefLabels = [
        lbl
        for n in skosified.objects(work, V.BFFI.extent)
        if isinstance(n, BNode)
        for lbl in skosified.objects(n, V.SKOS.prefLabel)
    ]
    assert Literal("Sisältö: 1. luku, 2. luku, 3. luku", lang="fi") in toc_prefLabels
    assert Literal("1 äänilevy", lang="fi") in ext_prefLabels


def test_related_resource_display_lifts_associated_resource(tmp_path: Path) -> None:
    """``bffi:relation → bnode → bffi:associatedResource <target>``
    emits ``dct:relation <target>`` flat on the parent."""
    work = URIRef(WORK)
    rel = BNode()
    target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b00000001#Work740-1")
    g = _build_canonical_graph()
    g.add((work, V.BFFI.relation, rel))
    g.add((rel, RDF.type, V.BFFI.Relation))
    g.add(
        (
            rel,
            V.BFFI.relationship,
            URIRef("http://id.loc.gov/vocabulary/relationship/relatedwork"),
        )
    )
    g.add((rel, V.BFFI.associatedResource, target))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, DCTERMS.relation, target) in skosified


def test_title_part_display_lifts_part_number_and_name(tmp_path: Path) -> None:
    """``bffi:title → bnode → bffi:partNumber/partName`` chain emits
    parallel flat ``bffi:partNumber`` / ``bffi:partName`` literals on
    the parent so Skosmos renders the part info directly."""
    work = URIRef(WORK)
    title = BNode()
    g = _build_canonical_graph()
    g.add((work, V.BFFI.title, title))
    g.add((title, V.BFFI.mainTitle, Literal("Sota ja rauha")))
    g.add((title, V.BFFI.partNumber, Literal("2")))
    g.add((title, V.BFFI.partName, Literal("Andrei")))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert (work, V.BFFI.partNumber, Literal("2")) in skosified
    assert (work, V.BFFI.partName, Literal("Andrei")) in skosified


def test_language_labels_materialised_from_bridge_on_referenced_uri(tmp_path: Path) -> None:
    """Sibling to the country-bridge test: a ``bf:language`` reference
    to a LoC language URI gets multilingual prefLabels materialised
    on the URI itself from the languages bridge."""
    bridge_path = tmp_path / "languages-bridge.ttl"
    bridge_path.write_text(
        """
@prefix loc:  <http://id.loc.gov/vocabulary/languages/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix yso:  <http://www.yso.fi/onto/yso/> .

loc:fin  skos:exactMatch yso:p8856 ;
         skos:prefLabel  "suomen kieli"@fi, "finska"@sv, "Finnish language"@en ;
         skos:notation   "fin" .
""",
        encoding="utf-8",
    )

    work = URIRef(WORK)
    lang_uri = URIRef("http://id.loc.gov/vocabulary/languages/fin")
    g = _build_canonical_graph()
    g.add((work, V.BF.language, lang_uri))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output, loc_languages_bridge_path=bridge_path)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    pref_labels = set(skosified.objects(lang_uri, V.SKOS.prefLabel))
    assert Literal("suomen kieli", lang="fi") in pref_labels
    assert Literal("finska", lang="sv") in pref_labels
    assert Literal("Finnish language", lang="en") in pref_labels
    assert (
        lang_uri,
        V.SKOS.exactMatch,
        URIRef("http://www.yso.fi/onto/yso/p8856"),
    ) in skosified


def test_issuance_labels_materialised_from_bridge_on_referenced_uri(tmp_path: Path) -> None:
    """Sibling to the country / language bridges: a ``bf:issuance``
    reference to a LoC issuance URI gets multilingual prefLabels +
    MTS exactMatch materialised on the URI itself."""
    bridge_path = tmp_path / "issuance-bridge.ttl"
    bridge_path.write_text(
        """
@prefix loc:  <http://id.loc.gov/vocabulary/issuance/> .
@prefix mts:  <http://urn.fi/URN:NBN:fi:au:mts:> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

loc:mono  skos:exactMatch mts:m4371 ;
          skos:prefLabel  "yhtenä yksikkönä ilmestyvä aineisto"@fi,
                          "single unit"@en ;
          skos:notation   "mono" .
""",
        encoding="utf-8",
    )

    work = URIRef(WORK)
    iss_uri = URIRef("http://id.loc.gov/vocabulary/issuance/mono")
    g = _build_canonical_graph()
    g.add((work, V.BF.issuance, iss_uri))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output, loc_issuance_bridge_path=bridge_path)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    pref_labels = set(skosified.objects(iss_uri, V.SKOS.prefLabel))
    assert Literal("yhtenä yksikkönä ilmestyvä aineisto", lang="fi") in pref_labels
    assert Literal("single unit", lang="en") in pref_labels
    assert (
        iss_uri,
        V.SKOS.exactMatch,
        URIRef("http://urn.fi/URN:NBN:fi:au:mts:m4371"),
    ) in skosified


def test_language_labels_skipped_for_unknown_code(tmp_path: Path) -> None:
    """A language URI absent from the bridge file is left bare."""
    bridge_path = tmp_path / "languages-bridge.ttl"
    bridge_path.write_text(
        """
@prefix loc:  <http://id.loc.gov/vocabulary/languages/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

loc:fin  skos:prefLabel "suomen kieli"@fi .
""",
        encoding="utf-8",
    )

    work = URIRef(WORK)
    lang_uri = URIRef("http://id.loc.gov/vocabulary/languages/xyz")
    g = _build_canonical_graph()
    g.add((work, V.BF.language, lang_uri))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    run(canonical, output_path=output, loc_languages_bridge_path=bridge_path)

    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert list(skosified.objects(lang_uri, V.SKOS.prefLabel)) == []


def test_country_labels_pass_silent_when_bridge_file_missing(tmp_path: Path) -> None:
    """Bridge file missing → pass is a no-op; Skosify still completes."""
    manifestation = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:m1")
    country = URIRef("http://id.loc.gov/vocabulary/countries/fi")
    pa = BNode()
    g = Graph()
    g.add((manifestation, RDF.type, V.BFFI.Manifestation))
    g.add((manifestation, V.BFFI.provisionActivity, pa))
    g.add((pa, V.BF.place, country))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "skosified.ttl"
    result = run(
        canonical,
        output_path=output,
        loc_countries_bridge_path=tmp_path / "missing.ttl",
    )
    assert result.skipped_idempotent is False
    skosified = Graph()
    skosified.parse(str(output), format="turtle")
    assert list(skosified.objects(country, V.SKOS.prefLabel)) == []
