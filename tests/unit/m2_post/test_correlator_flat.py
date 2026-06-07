"""P-50 Phase B — flat-literal entity correlator tests."""

from __future__ import annotations

import textwrap

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m2_post.correlator import correlate
from bffi_pipeline.stages.m2_post.token import MarcFieldIndex

BF = "http://id.loc.gov/ontologies/bibframe/"
BFLC = "http://id.loc.gov/ontologies/bflc/"


def _record(body: str, bib_id: str = "b1") -> str:
    return textwrap.dedent(
        f"""\
        <record xmlns="http://www.loc.gov/MARC21/slim">
          <controlfield tag="001">{bib_id}</controlfield>
          {body}
        </record>
        """
    )


def test_isbn_bnode_correlated_to_source_020() -> None:
    """A bf:Isbn bnode with rdf:value matching source 020 $a gets the
    token attached. ISBN comparison strips hyphens + non-alnum
    chars so source ``978-0-7119-7011-4`` matches BIBFRAME ``0711970114``
    when only the canonical-form bare digits propagated."""
    xml = _record(
        """
        <datafield tag="020" ind1=" " ind2=" ">
          <subfield code="a">0711970114</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    isbn = BNode()
    graph.add((isbn, RDF.type, URIRef(BF + "Isbn")))
    graph.add(
        (isbn, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#value"), Literal("0711970114"))
    )
    correlate(graph, index)
    assert Literal("b1:020:1") in set(graph.objects(isbn, V.fromMarcField))


def test_extent_bnode_correlated_to_source_300() -> None:
    """bf:Extent's rdfs:label combines source 300 $a + $b."""
    xml = _record(
        """
        <datafield tag="300" ind1=" " ind2=" ">
          <subfield code="a">597 sidor</subfield>
          <subfield code="b">illustrations</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    extent = BNode()
    graph.add((extent, RDF.type, URIRef(BF + "Extent")))
    graph.add((extent, RDFS.label, Literal("597 sidor illustrations")))
    correlate(graph, index)
    assert Literal("b1:300:1") in set(graph.objects(extent, V.fromMarcField))


def test_title_bnode_correlated_to_source_245() -> None:
    """bf:Title with bf:mainTitle "Foo" + bf:subtitle "Bar baz" matches
    source 245 $a "Foo" + $b "Bar baz" (concatenated)."""
    xml = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">Audition songs for female singers.</subfield>
          <subfield code="b">2</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    title = BNode()
    graph.add((title, RDF.type, URIRef(BF + "Title")))
    graph.add((title, URIRef(BF + "mainTitle"), Literal("Audition songs for female singers.")))
    graph.add((title, URIRef(BF + "subtitle"), Literal("2")))
    correlate(graph, index)
    assert Literal("b1:245:1") in set(graph.objects(title, V.fromMarcField))


def test_provision_activity_bnode_correlated_to_source_264() -> None:
    """bf:ProvisionActivity matches by simplePlace + simpleAgent + simpleDate
    against source 264 $a + $b + $c."""
    xml = _record(
        """
        <datafield tag="264" ind1=" " ind2="1">
          <subfield code="a">Malmö</subfield>
          <subfield code="b">Allhem</subfield>
          <subfield code="c">1961</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    prov = BNode()
    graph.add((prov, RDF.type, URIRef(BF + "ProvisionActivity")))
    graph.add((prov, URIRef(BFLC + "simplePlace"), Literal("Malmö")))
    graph.add((prov, URIRef(BFLC + "simpleAgent"), Literal("Allhem")))
    graph.add((prov, URIRef(BFLC + "simpleDate"), Literal("1961")))
    correlate(graph, index)
    assert Literal("b1:264:1") in set(graph.objects(prov, V.fromMarcField))


def test_note_bnode_correlated_to_source_5xx() -> None:
    """bf:Note with rdfs:label matches the source 5XX $a."""
    xml = _record(
        """
        <datafield tag="500" ind1=" " ind2=" ">
          <subfield code="a">Cover title.</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    note = BNode()
    graph.add((note, RDF.type, URIRef(BF + "Note")))
    graph.add((note, RDFS.label, Literal("Cover title.")))
    correlate(graph, index)
    assert Literal("b1:500:1") in set(graph.objects(note, V.fromMarcField))


def test_flat_pass_skips_entities_already_tokenised_by_marckey() -> None:
    """An entity that picked up a token from the Phase A marcKey pass
    should NOT be re-tokenised by Phase B (would risk double-counting
    against the shared pool). Sanity check the pre-tokenised guard."""
    xml = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">Foo</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    title = BNode()
    graph.add((title, RDF.type, URIRef(BF + "Title")))
    graph.add((title, URIRef(BF + "mainTitle"), Literal("Foo")))
    # Pretend Phase A already attached a token (simulated; in practice
    # bf:Title doesn't carry marcKey directly).
    graph.add((title, V.fromMarcField, Literal("b1:245:1")))
    correlate(graph, index)
    # Still exactly one token — no double-attach from Phase B.
    tokens = list(graph.objects(title, V.fromMarcField))
    assert tokens == [Literal("b1:245:1")]
