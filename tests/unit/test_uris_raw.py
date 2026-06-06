"""Unit tests for the M3 raw-URI minters and arq:sha1 SPARQL function."""

from __future__ import annotations

import hashlib

from rdflib import Graph

from bffi_pipeline.uris import (
    mint_raw_expression_uri,
    mint_raw_manifestation_uri,
    mint_raw_work_uri,
    register_sparql_functions,
)

WORK_NS = "http://urn.fi/URN:NBN:fi:bib:work:"
EXPR_NS = "http://urn.fi/URN:NBN:fi:bib:expression:"
MANI_NS = "http://urn.fi/URN:NBN:fi:bib:manifestation:"


def test_mint_raw_work_uri_is_sha1_of_bf_work_uri() -> None:
    bf = "http://urn.fi/URN:NBN:fi:bib:raw/12345678#Work"
    expected = WORK_NS + hashlib.sha1(bf.encode("utf-8")).hexdigest()
    assert mint_raw_work_uri(bf) == expected


def test_raw_uri_minters_use_committed_namespaces() -> None:
    bf = "http://urn.fi/URN:NBN:fi:bib:raw/X#Work"
    bf_inst = "http://urn.fi/URN:NBN:fi:bib:raw/X#Instance"
    assert mint_raw_work_uri(bf).startswith(WORK_NS)
    assert mint_raw_expression_uri(bf).startswith(EXPR_NS)
    assert mint_raw_manifestation_uri(bf_inst).startswith(MANI_NS)


def test_raw_minters_are_stable_across_runs() -> None:
    bf = "http://urn.fi/URN:NBN:fi:bib:raw/abc#Work"
    bf_inst = "http://urn.fi/URN:NBN:fi:bib:raw/abc#Instance"
    assert mint_raw_work_uri(bf) == mint_raw_work_uri(bf)
    assert mint_raw_expression_uri(bf) == mint_raw_expression_uri(bf)
    assert mint_raw_manifestation_uri(bf_inst) == mint_raw_manifestation_uri(bf_inst)


def test_raw_minters_are_sensitive_to_input() -> None:
    a = "http://urn.fi/URN:NBN:fi:bib:raw/A#Work"
    b = "http://urn.fi/URN:NBN:fi:bib:raw/B#Work"
    assert mint_raw_work_uri(a) != mint_raw_work_uri(b)
    a_inst = "http://urn.fi/URN:NBN:fi:bib:raw/A#Instance"
    b_inst = "http://urn.fi/URN:NBN:fi:bib:raw/B#Instance"
    assert mint_raw_manifestation_uri(a_inst) != mint_raw_manifestation_uri(b_inst)


def test_mint_raw_manifestation_uri_is_sha1_of_bf_instance_uri() -> None:
    """Manifestation minter follows the same hash-the-source-URI pattern
    as the Work / Expression minters — just keyed off the
    ``bf:Instance`` URI instead of the ``bf:Work`` URI."""
    bf_inst = "http://urn.fi/URN:NBN:fi:bib:raw/b25806610#Instance"
    expected = MANI_NS + hashlib.sha1(bf_inst.encode("utf-8")).hexdigest()
    assert mint_raw_manifestation_uri(bf_inst) == expected


def test_raw_minters_keep_work_and_manifestation_uri_spaces_distinct() -> None:
    """The two raw minters hash slightly different inputs (``#Work`` vs
    ``#Instance``) and target different namespaces — so a record's
    Work URI must never collide with its Manifestation URI."""
    bf_work = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Work"
    bf_inst = "http://urn.fi/URN:NBN:fi:bib:raw/10000001#Instance"
    work = mint_raw_work_uri(bf_work)
    mani = mint_raw_manifestation_uri(bf_inst)
    assert work != mani
    assert work.startswith(WORK_NS)
    assert mani.startswith(MANI_NS)


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
