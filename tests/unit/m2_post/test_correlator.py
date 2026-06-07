"""P-50 Phase A — correlator (bflc:marcKey → source MARC field)."""

from __future__ import annotations

import textwrap

from rdflib import Graph, Literal, URIRef

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m2_post.correlator import correlate, parse_marc_key
from bffi_pipeline.stages.m2_post.token import MarcFieldIndex


def test_parse_marc_key_extracts_tag_and_subfields() -> None:
    tag, subs = parse_marc_key("7001 $aAndersson, Benny,$esäveltäjä")  # type: ignore[misc]
    assert tag == "700"
    assert subs == (("a", "Andersson, Benny,"), ("e", "säveltäjä"))


def test_parse_marc_key_handles_no_subfields() -> None:
    parsed = parse_marc_key("0001 ")
    assert parsed == ("000", ())


def test_parse_marc_key_returns_none_for_malformed() -> None:
    assert parse_marc_key("not-a-marc-key") is None


def _record(body: str, bib_id: str = "b1") -> str:
    return textwrap.dedent(
        f"""\
        <record xmlns="http://www.loc.gov/MARC21/slim">
          <controlfield tag="001">{bib_id}</controlfield>
          {body}
        </record>
        """
    )


def test_correlate_attaches_token_via_exact_subfield_match() -> None:
    """A bf:Agent with marcKey ``700 $aAndersson, Benny,$esäveltäjä``
    correlates to the 700 source field with the same subfields and
    gets ``bffi-prov:fromMarcField "b1:700:1"`` attached."""
    xml = _record(
        """
        <datafield tag="700" ind1="1" ind2=" ">
          <subfield code="a">Andersson, Benny,</subfield>
          <subfield code="e">säveltäjä</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    agent = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b1#Agent700-31")
    graph.add(
        (
            agent,
            V.BFLC.marcKey,
            Literal("7001 $aAndersson, Benny,$esäveltäjä"),
        )
    )
    results = correlate(graph, index)
    tokens = list(graph.objects(agent, V.fromMarcField))
    assert tokens == [Literal("b1:700:1")]
    assert len(results) == 1
    assert results[0].matched_token == "b1:700:1"
    assert results[0].matched_via == "exact"


def test_correlate_assigns_duplicate_content_in_source_order() -> None:
    """Two 650 source fields with identical subfields → two BIBFRAME
    entities with identical marcKey. The correlator assigns the first
    marcKey-bearing entity (by sort order) to the first source field;
    the second to the second. Result is deterministic across runs."""
    xml = _record(
        """
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">sodat</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">sodat</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    topic_a = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b1#Topic650-22")
    topic_b = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b1#Topic650-23")
    graph.add((topic_a, V.BFLC.marcKey, Literal("650 7$asodat")))
    graph.add((topic_b, V.BFLC.marcKey, Literal("650 7$asodat")))
    correlate(graph, index)
    tokens_a = sorted(str(t) for t in graph.objects(topic_a, V.fromMarcField))
    tokens_b = sorted(str(t) for t in graph.objects(topic_b, V.fromMarcField))
    # Two source fields, two entities — each got one distinct token.
    assert len(tokens_a) == 1
    assert len(tokens_b) == 1
    assert {tokens_a[0], tokens_b[0]} == {"b1:650:1", "b1:650:2"}


def test_correlate_falls_back_to_subfield_codes_when_values_drift() -> None:
    """marcKey value drift (ISBD punctuation, whitespace) shouldn't
    block the match. Tier 2 (same multiset of subfield codes) catches
    the case."""
    xml = _record(
        """
        <datafield tag="700" ind1="1" ind2=" ">
          <subfield code="a">Andersson, Benny</subfield>
          <subfield code="e">säveltäjä</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    agent = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b1#Agent700-31")
    # marcKey has the ISBD trailing comma; source $a doesn't. Tier 1
    # (exact) doesn't match; Tier 2 (same codes) does.
    graph.add(
        (
            agent,
            V.BFLC.marcKey,
            Literal("7001 $aAndersson, Benny,$esäveltäjä"),
        )
    )
    results = correlate(graph, index)
    tokens = list(graph.objects(agent, V.fromMarcField))
    assert tokens == [Literal("b1:700:1")]
    assert results[0].matched_via == "subfield-codes"


def test_correlate_audits_unmatched_when_tag_missing_in_source() -> None:
    """A marcKey with a tag that doesn't exist in the source MARC →
    no triple attached; audit row records the reason."""
    xml = _record("")  # no datafields
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    agent = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b1#Agent700-31")
    graph.add((agent, V.BFLC.marcKey, Literal("7001 $aGhost, A.")))
    results = correlate(graph, index)
    assert list(graph.objects(agent, V.fromMarcField)) == []
    assert results[0].matched_via == "unmatched"
    assert results[0].reason is not None
    assert "tag-700" in results[0].reason


def test_correlate_is_idempotent() -> None:
    """Re-running against an already-correlated graph yields the same
    triples (rdflib set-semantics) and the same audit log."""
    xml = _record(
        """
        <datafield tag="700" ind1="1" ind2=" ">
          <subfield code="a">Andersson, Benny,</subfield>
          <subfield code="e">säveltäjä</subfield>
        </datafield>
        """
    )
    index = MarcFieldIndex.from_marcxml_string(xml)
    graph = Graph()
    agent = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b1#Agent700-31")
    graph.add(
        (
            agent,
            V.BFLC.marcKey,
            Literal("7001 $aAndersson, Benny,$esäveltäjä"),
        )
    )
    correlate(graph, index)
    correlate(graph, index)  # second pass
    tokens = list(graph.objects(agent, V.fromMarcField))
    # Set-semantics in rdflib means duplicates collapse.
    assert tokens == [Literal("b1:700:1")]
