"""P-52 Phase I — typing + aggregations sidecar builder tests.

Verifies that :mod:`bffi_pipeline.release.typing_review` produces
JSONL rows that match a synthetic canonical's typing distribution
+ aggregation component structure.
"""

from __future__ import annotations

import json
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.release.typing_review import (
    build_aggregation_report,
    build_typing_summary,
)


def _build_canonical_fixture() -> Graph:
    """Synthetic canonical with three records — one plain MonographWork,
    one aggregating MusicWork, one CartographyWork."""
    g = Graph()
    for _i, (bib_id, work_types, expr_types, manif_types) in enumerate(
        [
            (
                "b00000001",
                [V.BFFI.Work, V.BFFI.MonographWork],
                [V.BFFI.Expression, V.BFFI.Text, V.BFFI.MonographExpression],
                [V.BFFI.Manifestation, V.BFFI.Print],
            ),
            (
                "b00000002",
                [V.BFFI.Work, V.BFFI.MusicWork, V.BFFI.AggregatingWork, V.BFFI.MonographWork],
                [V.BFFI.Expression, V.BFFI.NotatedMusic, V.BFFI.AggregatingExpression],
                [V.BFFI.Manifestation, V.BFFI.Print],
            ),
            (
                "b00000003",
                [V.BFFI.Work, V.BFFI.CartographyWork],
                [V.BFFI.Expression, V.BFFI.CartographyExpression],
                [V.BFFI.Manifestation, V.BFFI.Print],
            ),
        ],
        start=1,
    ):
        work = URIRef(f"urn:work/{bib_id}")
        expr = URIRef(f"urn:expr/{bib_id}")
        manif = URIRef(f"urn:manif/{bib_id}")
        for t in work_types:
            g.add((work, RDF.type, t))
        for t in expr_types:
            g.add((expr, RDF.type, t))
        for t in manif_types:
            g.add((manif, RDF.type, t))
        g.add((work, V.BFFI.hasExpression, expr))
        g.add((expr, V.BFFI.expressionOf, work))
        g.add((manif, V.BFFI.expressionManifested, expr))
        g.add((manif, DCTERMS.identifier, Literal(bib_id)))
    return g


def test_build_typing_summary_emits_one_row_per_manifestation(tmp_path: Path) -> None:
    """Every Manifestation gets exactly one row in the JSONL output,
    keyed by bib_id, with subclass types from all three axes."""
    canonical = tmp_path / "canonical.ttl"
    _build_canonical_fixture().serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "typing-summary.jsonl"

    n = build_typing_summary(canonical, output)
    assert n == 3
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    # Sorted by bib_id (reproducible across runs).
    assert [r["bib_id"] for r in rows] == ["b00000001", "b00000002", "b00000003"]
    # Aggregating record carries AggregatingWork on the Work axis.
    aggregating = next(r for r in rows if r["bib_id"] == "b00000002")
    assert "AggregatingWork" in aggregating["work_types"]
    assert "MusicWork" in aggregating["work_types"]
    assert "MonographWork" in aggregating["work_types"]
    assert "AggregatingExpression" in aggregating["expression_types"]
    assert "NotatedMusic" in aggregating["expression_types"]
    assert "Print" in aggregating["manifestation_types"]
    # Cartography record's Expression has CartographyExpression but not
    # Text (axis-isolation check).
    cartography = next(r for r in rows if r["bib_id"] == "b00000003")
    assert "CartographyExpression" in cartography["expression_types"]
    assert "Text" not in cartography["expression_types"]


def test_build_typing_summary_only_includes_bffi_namespace_types(tmp_path: Path) -> None:
    """Non-bffi: types (skos:Concept, owl:Class etc.) on the entities
    are filtered out — the sidecar focuses on BFFI subclass typing."""
    g = _build_canonical_fixture()
    work = URIRef("urn:work/b00000001")
    # Add a non-bffi: type that should NOT appear in the sidecar.
    g.add((work, RDF.type, V.SKOS.Concept))
    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "typing-summary.jsonl"

    build_typing_summary(canonical, output)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    first = next(r for r in rows if r["bib_id"] == "b00000001")
    assert "Concept" not in first["work_types"]
    assert all("/" not in t and ":" not in t for t in first["work_types"])


def test_build_aggregation_report_emits_one_row_per_aggregating_expression(
    tmp_path: Path,
) -> None:
    """Aggregating Expressions get one row each with parent title +
    enumerated components. Records without bffi:AggregatingExpression
    typing are omitted."""
    g = _build_canonical_fixture()
    # Add 2 components to the aggregating Expression of b00000002.
    parent_expr = URIRef("urn:expr/b00000002")
    g.add((parent_expr, V.SKOS.prefLabel, Literal("Aggregating parent title", lang="en")))
    for i, (title, agent_name) in enumerate(
        [("Song One", "Composer Alpha"), ("Song Two", "Composer Beta")],
        start=1,
    ):
        component = URIRef(f"urn:expr/comp-{i}")
        g.add((parent_expr, V.BFFI.aggregates, component))
        g.add((component, RDF.type, V.BFFI.Expression))
        g.add((component, V.SKOS.prefLabel, Literal(title)))
        contrib = URIRef(f"urn:contrib/comp-{i}")
        agent = URIRef(f"urn:agent/comp-{i}")
        g.add((component, V.BFFI.contribution, contrib))
        g.add((contrib, V.BFFI.agent, agent))
        g.add((agent, RDFS.label, Literal(agent_name)))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "aggregations.jsonl"

    n = build_aggregation_report(canonical, output)
    assert n == 1
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    [row] = rows
    assert row["bib_id"] == "b00000002"
    assert row["parent_title"] == "Aggregating parent title"
    assert row["component_count"] == 2
    titles = {c["title"] for c in row["components"]}
    assert titles == {"Song One", "Song Two"}
    agents = {c["agent_name"] for c in row["components"]}
    assert agents == {"Composer Alpha", "Composer Beta"}
    # All components have "no-agent" or "unresolved" status — the
    # synthetic agents have no authority bindings.
    statuses = {c["reconciliation_status"] for c in row["components"]}
    assert statuses == {"unresolved"}


def test_build_aggregation_report_handles_components_without_agents(
    tmp_path: Path,
) -> None:
    """A component with no ``bffi:contribution`` chain (Phase G.bis
    couldn't extract $g) gets reconciliation_status = 'no-agent'."""
    g = _build_canonical_fixture()
    parent_expr = URIRef("urn:expr/b00000002")
    component = URIRef("urn:expr/comp-1")
    g.add((parent_expr, V.BFFI.aggregates, component))
    g.add((component, RDF.type, V.BFFI.Expression))
    g.add((component, V.SKOS.prefLabel, Literal("Component without agent")))
    # NO contribution chain on the component.

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "aggregations.jsonl"

    build_aggregation_report(canonical, output)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    [row] = rows
    [comp] = row["components"]
    assert comp["agent_name"] is None
    assert comp["reconciliation_status"] == "no-agent"


def test_build_aggregation_report_emits_zero_rows_for_no_aggregating_records(
    tmp_path: Path,
) -> None:
    """Pipelines with no aggregating records produce an empty JSONL
    (the standalone HTML viewer's empty-state handler renders 'no
    aggregating records' instead of an empty card list)."""
    g = Graph()
    # Plain Work + Expression + Manifestation, no aggregating typing.
    work = URIRef("urn:work/plain")
    expr = URIRef("urn:expr/plain")
    manif = URIRef("urn:manif/plain")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((manif, RDF.type, V.BFFI.Manifestation))
    g.add((manif, DCTERMS.identifier, Literal("b00000001")))
    g.add((manif, V.BFFI.expressionManifested, expr))
    g.add((expr, V.BFFI.expressionOf, work))

    canonical = tmp_path / "canonical.ttl"
    g.serialize(destination=str(canonical), format="turtle")
    output = tmp_path / "aggregations.jsonl"

    n = build_aggregation_report(canonical, output)
    assert n == 0
    assert output.read_text(encoding="utf-8") == ""
