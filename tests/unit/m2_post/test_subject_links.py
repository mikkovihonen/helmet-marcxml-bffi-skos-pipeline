"""P-50 Phase C — subject reification tests.

Verifies the b10642122-class bug fix: when source MARC has $0-keyed
subjects, marc2bibframe2 emits ``<bf:Topic rdf:about=<yso-uri>>``
direct with no per-record entity. The subject-statement minter creates
a per-record W3C ``rdf:Statement`` reification anchoring each
occurrence's provenance token.
"""

from __future__ import annotations

import textwrap

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m2_post.correlator import correlate
from bffi_pipeline.stages.m2_post.token import MarcFieldIndex

BF = "http://id.loc.gov/ontologies/bibframe/"
WORK = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b1#Work")


def _record(body: str, bib_id: str = "b1") -> str:
    return textwrap.dedent(
        f"""\
        <record xmlns="http://www.loc.gov/MARC21/slim">
          <controlfield tag="001">{bib_id}</controlfield>
          {body}
        </record>
        """
    )


def _seed_work(graph: Graph) -> None:
    graph.add((WORK, RDF.type, URIRef(BF + "Work")))


def _reified_statements_for(graph: Graph, work: URIRef) -> list[URIRef]:
    """Return all rdf:Statement nodes whose rdf:subject is ``work``
    and rdf:predicate is bffi:subject — the P-50 reification shape."""
    out: list[URIRef] = []
    for stmt in graph.subjects(RDF.subject, work):
        if not isinstance(stmt, URIRef):
            continue
        if (stmt, RDF.type, RDF.Statement) not in graph:
            continue
        if (stmt, RDF.predicate, V.reifiedSubjectPredicate) not in graph:
            continue
        out.append(stmt)
    return out


def test_reified_statement_minted_for_yso_uri_keyed_subject() -> None:
    """The b10642122 case: source 650 with $0 yso/p13819 →
    marc2bibframe2 emits <bf:Topic rdf:about=yso/p13819>. M2-post
    mints a per-record rdf:Statement anchoring the provenance token."""
    xml = _record(
        """
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">yhteiskuntafilosofia</subfield>
          <subfield code="0">http://www.yso.fi/onto/yso/p13819</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    _seed_work(graph)
    yso = URIRef("http://www.yso.fi/onto/yso/p13819")
    graph.add((WORK, URIRef(BF + "subject"), yso))
    graph.add((yso, RDF.type, URIRef(BF + "Topic")))

    correlate(graph, index)

    # A reified statement was minted.
    statements = _reified_statements_for(graph, WORK)
    assert len(statements) == 1
    stmt = statements[0]
    # rdf:object points at the YSO URI; statement carries the token.
    assert next(graph.objects(stmt, RDF.object)) == yso
    tokens = list(graph.objects(stmt, V.fromMarcField))
    assert len(tokens) == 1
    assert str(tokens[0]) == "b1:650:1"


def test_subject_link_falls_back_to_a_label_when_no_dollar_zero() -> None:
    """Source 650 with no $0 — marc2bibframe2 emits a raw bib URI
    ``#Topic650-N``. The matcher's Tier 3 (raw URI fragment) finds
    the target; Tier 2 (label) also works."""
    xml = _record(
        """
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">sodat</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    _seed_work(graph)
    topic = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b1#Topic650-22")
    graph.add((WORK, URIRef(BF + "subject"), topic))
    graph.add((topic, RDF.type, URIRef(BF + "Topic")))
    graph.add((topic, RDFS.label, Literal("sodat")))

    correlate(graph, index)

    statements = _reified_statements_for(graph, WORK)
    assert len(statements) == 1
    assert next(graph.objects(statements[0], RDF.object)) == topic
    assert str(next(graph.objects(statements[0], V.fromMarcField))) == "b1:650:1"


def test_reified_statement_uri_is_deterministic_per_record_per_occurrence() -> None:
    """The URI format is ``…/subject-statement:<bib>:<tag>:<ord>`` so
    two runs against the same record produce the same statement URIs
    (no UUIDs; re-runs are idempotent on the canonical graph)."""
    xml = _record(
        """
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">sodat</subfield>
          <subfield code="0">http://www.yso.fi/onto/yso/p1234</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    _seed_work(graph)
    yso = URIRef("http://www.yso.fi/onto/yso/p1234")
    graph.add((WORK, URIRef(BF + "subject"), yso))

    correlate(graph, index)

    [stmt] = _reified_statements_for(graph, WORK)
    assert str(stmt) == "http://urn.fi/URN:NBN:fi:bib:subject-statement:b1:650:1"


def test_two_distinct_yso_subjects_produce_two_distinct_statements() -> None:
    """Two source 650s with different $0 URIs → two reified
    statements, each with its own per-occurrence token. This is the
    b10642122 shape: many $0-keyed subjects in one record."""
    xml = _record(
        """
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">yhteiskuntafilosofia</subfield>
          <subfield code="0">http://www.yso.fi/onto/yso/p13819</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">sivilisaatio</subfield>
          <subfield code="0">http://www.yso.fi/onto/yso/p7952</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    _seed_work(graph)
    for uri in (
        "http://www.yso.fi/onto/yso/p13819",
        "http://www.yso.fi/onto/yso/p7952",
    ):
        graph.add((WORK, URIRef(BF + "subject"), URIRef(uri)))

    correlate(graph, index)

    statements = _reified_statements_for(graph, WORK)
    assert len(statements) == 2
    tokens = sorted(str(t) for stmt in statements for t in graph.objects(stmt, V.fromMarcField))
    assert tokens == ["b1:650:1", "b1:650:2"]
