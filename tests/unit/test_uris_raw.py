"""Unit tests for the M3 raw-URI minters and arq:sha1 SPARQL function."""

from __future__ import annotations

import hashlib

from rdflib import Graph

from bffi_pipeline.uris import (
    mint_raw_expression_uri,
    mint_raw_work_uri,
    register_sparql_functions,
)

WORK_NS = "http://urn.fi/URN:NBN:fi:bib:work:"
EXPR_NS = "http://urn.fi/URN:NBN:fi:bib:expression:"


def test_mint_raw_work_uri_is_sha1_of_bf_work_uri() -> None:
    bf = "http://urn.fi/URN:NBN:fi:bib:raw/12345678#Work"
    expected = WORK_NS + hashlib.sha1(bf.encode("utf-8")).hexdigest()
    assert mint_raw_work_uri(bf) == expected


def test_raw_uri_minters_use_committed_namespaces() -> None:
    bf = "http://urn.fi/URN:NBN:fi:bib:raw/X#Work"
    assert mint_raw_work_uri(bf).startswith(WORK_NS)
    assert mint_raw_expression_uri(bf).startswith(EXPR_NS)


def test_raw_minters_are_stable_across_runs() -> None:
    bf = "http://urn.fi/URN:NBN:fi:bib:raw/abc#Work"
    assert mint_raw_work_uri(bf) == mint_raw_work_uri(bf)
    assert mint_raw_expression_uri(bf) == mint_raw_expression_uri(bf)


def test_raw_minters_are_sensitive_to_input() -> None:
    a = "http://urn.fi/URN:NBN:fi:bib:raw/A#Work"
    b = "http://urn.fi/URN:NBN:fi:bib:raw/B#Work"
    assert mint_raw_work_uri(a) != mint_raw_work_uri(b)


def test_arq_sha1_matches_python_sha1_in_sparql() -> None:
    """SPARQL CONSTRUCT using arq:sha1 must agree with mint_raw_work_uri."""
    register_sparql_functions()
    bf = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work"
    g = Graph()
    g.parse(
        data=f"<{bf}> a <http://id.loc.gov/ontologies/bibframe/Work> .",
        format="turtle",
    )
    q = """
    PREFIX bf:  <http://id.loc.gov/ontologies/bibframe/>
    PREFIX arq: <http://jena.apache.org/ARQ/function#>
    CONSTRUCT { ?w a <urn:bffi:Work> }
    WHERE {
      ?bfWork a bf:Work .
      BIND( IRI(CONCAT(
        "http://urn.fi/URN:NBN:fi:bib:work:", arq:sha1(STR(?bfWork))
      )) AS ?w )
    }
    """
    minted = {str(t[0]) for t in g.query(q)}
    assert minted == {mint_raw_work_uri(bf)}
