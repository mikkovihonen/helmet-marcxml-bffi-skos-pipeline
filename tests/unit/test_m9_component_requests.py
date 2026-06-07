"""P-52 Phase H tests — M9 creator-request iteration walks aggregation
component agents.

The ``_iter_creator_requests`` walker yields one ``EntityRequest`` per
agent the M9 reconciler should bind to an authority. Phase H extends
the walker to include component agents (synthesised by Phase G.bis
from each ``bf:Hub``'s ``bflc:marcKey`` ``$g`` subfield).
"""

from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m9.requests import _iter_creator_requests


def _build_minimal_canonical(work_uri: str = "urn:work/A") -> tuple[Graph, URIRef]:
    """A canonical-shaped graph with one Work + one Expression and a
    Helmet identifier on a Manifestation (so ``_collect_work_context``
    finds at least one bib_id)."""
    g = Graph()
    work = URIRef(work_uri)
    expr = URIRef("urn:expr/A")
    manif = URIRef("urn:manif/A")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Parent compilation title", lang="en")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((manif, RDF.type, V.BFFI.Manifestation))
    g.add((manif, V.BFFI.expressionManifested, expr))
    return g, work


def test_iter_creator_requests_yields_component_agents_from_aggregating_expression() -> None:
    """Each ``bffi:aggregates → component → bffi:contribution →
    bffi:agent → rdfs:label`` chain produces one EntityRequest with
    ``kind=person`` and ``work_uri`` pointing at the parent
    canonical Work."""
    g, work = _build_minimal_canonical()
    expr = next(g.objects(work, V.BFFI.hasExpression))
    components_and_agents = [
        ("urn:expr/comp-1", "Gore, Michael"),
        ("urn:expr/comp-2", "Schönberg, Claude-Michel"),
    ]
    for comp_uri, agent_label in components_and_agents:
        component = URIRef(comp_uri)
        g.add((expr, V.BFFI.aggregates, component))
        g.add((component, RDF.type, V.BFFI.Expression))
        contrib = URIRef(f"{comp_uri}/contrib")
        agent = URIRef(f"{comp_uri}/agent")
        g.add((component, V.BFFI.contribution, contrib))
        g.add((contrib, V.BFFI.agent, agent))
        g.add((agent, RDFS.label, Literal(agent_label)))

    requests = list(_iter_creator_requests(g))
    literals = {r.literal for r in requests}
    assert literals == {"Gore, Michael", "Schönberg, Claude-Michel"}
    for r in requests:
        assert r.kind == "person"
        assert r.work_uri == str(work)


def test_iter_creator_requests_skips_non_aggregating_expressions() -> None:
    """A Work with a plain (non-aggregating) Expression and no
    ``bffi:aggregates`` edges yields zero component requests."""
    g, _work = _build_minimal_canonical()
    # Add a primary contribution that DOES yield (to confirm only the
    # component path is silent).
    work = next(g.subjects(RDF.type, V.BFFI.Work))
    contrib_pri = URIRef("urn:contrib/pri")
    agent = URIRef("urn:agent/primary")
    g.add((work, V.BFFI.contribution, contrib_pri))
    g.add((contrib_pri, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib_pri, V.BFFI.agent, agent))
    g.add((agent, RDFS.label, Literal("Primary Author")))

    requests = list(_iter_creator_requests(g))
    assert len(requests) == 1
    assert requests[0].literal == "Primary Author"


def test_iter_creator_requests_emits_one_per_component_agent_pair() -> None:
    """Two components with the same agent label still yield two
    requests; the picker cache (downstream) handles dedup by
    cache-key. The iterator's job is to enumerate occurrences."""
    g, _work = _build_minimal_canonical()
    work = next(g.subjects(RDF.type, V.BFFI.Work))
    expr = next(g.objects(work, V.BFFI.hasExpression))
    for i in range(2):
        component = URIRef(f"urn:expr/comp-{i}")
        g.add((expr, V.BFFI.aggregates, component))
        g.add((component, RDF.type, V.BFFI.Expression))
        contrib = URIRef(f"urn:expr/comp-{i}/contrib")
        agent = URIRef(f"urn:expr/comp-{i}/agent")
        g.add((component, V.BFFI.contribution, contrib))
        g.add((contrib, V.BFFI.agent, agent))
        g.add((agent, RDFS.label, Literal("Shared Composer")))

    requests = list(_iter_creator_requests(g))
    assert [r.literal for r in requests] == ["Shared Composer", "Shared Composer"]
