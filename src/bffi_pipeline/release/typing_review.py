"""P-52 Phase I — cataloguer-review surfaces for BFFI subclass typing
and aggregation components.

Generates two sidecars from a post-M8 canonical graph:

1. ``typing-summary.jsonl`` — one row per Helmet bib_id with the
   ``bffi:`` subclass types emitted on its Work / Expression /
   Manifestation. Lets cataloguers eyeball misclassifications and
   audit typing coverage.

2. ``aggregations.jsonl`` — one row per aggregating record listing
   parent prefLabel + component title / agent / reconciliation
   status. Lets cataloguers verify that compilation records'
   component lists match source MARC + spot M9 reconciliation misses.

Both sidecars are corpus-wide snapshots (not sampled). Driven by a
standalone HTML viewer ``typing-review.html`` (separate from the
sample-based ``cataloguer-review.html`` reviewer) that loads the
JSONL alongside it via a relative path. The viewer is shipped in
``gold/typing-review.html`` and copied next to the sidecars by the
``cataloguer-bundle`` dispatcher.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, TypedDict

from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS, RDF, Namespace
from rdflib.term import Node

from bffi_pipeline.provenance import vocab as V

_BFFI_NS: Final[str] = str(V.BFFI)


class TypingRow(TypedDict):
    """One row of ``typing-summary.jsonl`` — corpus typing for one record."""

    bib_id: str
    work_uri: str
    expression_uri: str
    manifestation_uri: str
    work_types: list[str]
    expression_types: list[str]
    manifestation_types: list[str]


class AggregationComponent(TypedDict):
    """One component in an aggregation row."""

    component_uri: str
    title: str | None
    agent_name: str | None
    agent_uri: str | None
    reconciliation_status: str


class AggregationRow(TypedDict):
    """One row of ``aggregations.jsonl`` — components of one aggregating record."""

    bib_id: str
    parent_work_uri: str
    parent_expression_uri: str
    parent_title: str | None
    component_count: int
    components: list[AggregationComponent]


# Local namespaces for graph walks (lazy to avoid imports up top)
_BF: Final[Namespace] = V.BF


def _short_class(uri: Node) -> str:
    """Strip the BFFI namespace prefix from a class URI for sidecar
    readability. ``bffi:MonographWork`` → ``"MonographWork"``."""
    s = str(uri)
    if s.startswith(_BFFI_NS):
        return s[len(_BFFI_NS) :]
    return s


def _is_bffi_class(uri: Node) -> bool:
    return isinstance(uri, URIRef) and str(uri).startswith(_BFFI_NS)


def _first_pref_label(graph: Graph, subject: URIRef | None) -> str | None:
    if subject is None:
        return None
    for label in graph.objects(subject, V.SKOS.prefLabel):
        return str(label)
    return None


def _first_rdfs_label(graph: Graph, subject: URIRef | None) -> str | None:
    if subject is None:
        return None
    for label in graph.objects(subject, V.RDFS.label):
        return str(label)
    return None


def _component_title(graph: Graph, component: URIRef) -> str | None:
    """Component title resolution: ``skos:prefLabel`` first (emitted
    by M3 when the source Hub had ``rdfs:label``), falling back to
    parsing ``$a`` from ``bflc:marcKey``. Many Helmet Hubs carry only
    marcKey, so the fallback is the common case for 730-derived
    components."""
    label = _first_pref_label(graph, component)
    if label is not None:
        return label
    for mk in graph.objects(component, V.BFLC.marcKey):
        text = str(mk)
        idx = text.find("$a")
        if idx < 0:
            continue
        after = text[idx + 2 :]
        next_delim = after.find("$")
        a_value = after if next_delim < 0 else after[:next_delim]
        a_value = a_value.strip().rstrip("/.,;:").strip()
        if a_value:
            return a_value
    return None


def _bib_id_for_manifestation(graph: Graph, manif: URIRef) -> str | None:
    """Pull the Helmet bib_id from a Manifestation via its
    ``dct:identifier`` literal (M8 sets this from the source 907 /
    bf:identifiedBy chain)."""
    for ident_literal in graph.objects(manif, DCTERMS.identifier):
        s = str(ident_literal)
        return s.lstrip(".") or None
    return None


def build_typing_summary(canonical_path: Path, output_path: Path) -> int:
    """Walk ``canonical_path`` and emit one ``TypingRow`` per
    Manifestation to ``output_path``. Returns row count.

    Order: sorted by bib_id for reproducible diffs across runs."""
    graph = Graph()
    graph.parse(canonical_path, format="turtle")
    rows: list[TypingRow] = []
    for manif in graph.subjects(RDF.type, V.BFFI.Manifestation):
        if not isinstance(manif, URIRef):
            continue
        bib_id = _bib_id_for_manifestation(graph, manif)
        if bib_id is None:
            continue
        expr_uris = [
            e for e in graph.objects(manif, V.BFFI.expressionManifested) if isinstance(e, URIRef)
        ]
        expr_uri = expr_uris[0] if expr_uris else None
        work_uri: URIRef | None = None
        if expr_uri is not None:
            work_uris = [
                w for w in graph.objects(expr_uri, V.BFFI.expressionOf) if isinstance(w, URIRef)
            ]
            work_uri = work_uris[0] if work_uris else None
        rows.append(
            TypingRow(
                bib_id=bib_id,
                work_uri=str(work_uri) if work_uri else "",
                expression_uri=str(expr_uri) if expr_uri else "",
                manifestation_uri=str(manif),
                work_types=sorted(
                    _short_class(t) for t in graph.objects(work_uri, RDF.type) if _is_bffi_class(t)
                )
                if work_uri
                else [],
                expression_types=sorted(
                    _short_class(t) for t in graph.objects(expr_uri, RDF.type) if _is_bffi_class(t)
                )
                if expr_uri
                else [],
                manifestation_types=sorted(
                    _short_class(t) for t in graph.objects(manif, RDF.type) if _is_bffi_class(t)
                ),
            )
        )
    rows.sort(key=lambda r: r["bib_id"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(output_path)
    return len(rows)


def _reconciliation_status_for_agent(graph: Graph, agent: URIRef | None) -> tuple[str, str | None]:
    """Read M9's reconciliation output from the agent node. Returns
    ``(status, authority_uri)``:

    - ``"reconciled"`` when the agent carries ``bffi:authorityHasMatch``
      / ``bffi:reconciledTo`` pointing at a non-raw URI (M9 bound it)
    - ``"unresolved"`` when no authority match exists
    - ``"no-agent"`` when the agent is None (component has no
      contribution chain — Phase G.bis didn't extract a $g)
    """
    if agent is None:
        return ("no-agent", None)
    # M9 attaches the matched authority via several possible
    # predicates depending on era; check the common ones.
    for predicate in (V.BFFI.authorityMatch, V.BFFI.reconciledTo, V.BF.identifiedBy):
        for target in graph.objects(agent, predicate):
            if isinstance(target, URIRef):
                s = str(target)
                if "yso" in s or "kanto" in s or "finaf" in s or "kaunokki" in s:
                    return ("reconciled", s)
    return ("unresolved", None)


def build_aggregation_report(canonical_path: Path, output_path: Path) -> int:
    """Walk ``canonical_path`` for aggregating records and emit one
    ``AggregationRow`` per aggregating Expression to ``output_path``.
    Returns row count.

    For each ``bffi:AggregatingExpression``:
    - Walk back to its parent Work via ``bffi:expressionOf``
    - Walk back to the Manifestation via inverse
      ``bffi:expressionManifested`` so we can attach a bib_id
    - For each ``bffi:aggregates`` component, collect title (skos:prefLabel)
      + agent (via bffi:contribution → bffi:agent → rdfs:label)
      + reconciliation status
    """
    graph = Graph()
    graph.parse(canonical_path, format="turtle")
    rows: list[AggregationRow] = []
    for parent_expr in graph.subjects(RDF.type, V.BFFI.AggregatingExpression):
        if not isinstance(parent_expr, URIRef):
            continue
        # Work back to a Manifestation for the bib_id.
        bib_id = ""
        for manif in graph.subjects(V.BFFI.expressionManifested, parent_expr):
            if isinstance(manif, URIRef):
                got = _bib_id_for_manifestation(graph, manif)
                if got:
                    bib_id = got
                    break
        work_uris = [
            w for w in graph.objects(parent_expr, V.BFFI.expressionOf) if isinstance(w, URIRef)
        ]
        parent_work_uri = str(work_uris[0]) if work_uris else ""
        parent_title = _first_pref_label(graph, parent_expr)
        components: list[AggregationComponent] = []
        for comp in graph.objects(parent_expr, V.BFFI.aggregates):
            if not isinstance(comp, URIRef):
                continue
            comp_title = _component_title(graph, comp)
            # First contribution → first agent → first label
            agent_uri: URIRef | None = None
            agent_name: str | None = None
            for contrib in graph.objects(comp, V.BFFI.contribution):
                for agent in graph.objects(contrib, V.BFFI.agent):
                    if isinstance(agent, URIRef):
                        agent_uri = agent
                        agent_name = _first_rdfs_label(graph, agent)
                    break
                break
            status, authority = _reconciliation_status_for_agent(graph, agent_uri)
            components.append(
                AggregationComponent(
                    component_uri=str(comp),
                    title=comp_title,
                    agent_name=agent_name,
                    agent_uri=authority or (str(agent_uri) if agent_uri else None),
                    reconciliation_status=status,
                )
            )
        components.sort(key=lambda c: c["component_uri"])
        rows.append(
            AggregationRow(
                bib_id=bib_id,
                parent_work_uri=parent_work_uri,
                parent_expression_uri=str(parent_expr),
                parent_title=parent_title,
                component_count=len(components),
                components=components,
            )
        )
    rows.sort(key=lambda r: r["bib_id"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(output_path)
    return len(rows)
