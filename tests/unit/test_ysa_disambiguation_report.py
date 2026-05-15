"""Unit tests for ``stages/ysa_disambiguation_report``.

All HTTP traffic goes through ``httpx.MockTransport``; no live Fuseki.
Fixtures hand-craft canonical-graph shapes (one or more bf:Work nodes
with bff:subject targets carrying rdfs:label + bf:source) and stub
Fuseki responses with the disambiguated YSO prefLabel candidates.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m9.ysa_disambiguation_report import (
    CSV_COLUMNS,
    DisambiguationCandidate,
    DisambiguationRow,
    _helmet_bib_ids_for_work,
    _query_disambiguation_candidates,
    _source_tag_for_literal,
    run,
    walk_disambiguation_residue,
    write_csv,
)

# --- _query_disambiguation_candidates ----------------------------------


def _candidate_handler(
    rows: list[dict[str, Any]],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sparql")
        return httpx.Response(200, json={"results": {"bindings": rows}})

    return httpx.MockTransport(handler)


def _bind(uri: str, label: str, lang: str = "fi") -> dict[str, Any]:
    return {
        "uri": {"type": "uri", "value": uri},
        "label": {"type": "literal", "value": label, "xml:lang": lang},
    }


def test_query_returns_two_candidates_for_lapset() -> None:
    """The canonical Pattern I case: ``lapset`` resolves to two
    disambiguated YSO concepts. Verifies both are returned and the
    bare form is NOT included (FILTER excludes exact-match)."""
    transport = _candidate_handler(
        [
            _bind("http://www.yso.fi/onto/yso/p4354", "lapset (ikäryhmät)"),
            _bind("http://www.yso.fi/onto/yso/p2357", "lapset (perheenjäsenet)"),
        ]
    )
    with httpx.Client(transport=transport) as c:
        candidates = _query_disambiguation_candidates(c, "http://localhost:3030/bffi", "lapset")
    assert len(candidates) == 2
    assert candidates[0].uri.endswith("/p4354")
    assert candidates[0].pref_label == "lapset (ikäryhmät)"
    assert candidates[1].pref_label == "lapset (perheenjäsenet)"


def test_query_returns_empty_when_no_disambiguation_exists() -> None:
    """A literal that has no parenthetical-form match in YSO returns
    empty — the residue report should skip these (they're either real
    no-candidates or already-resolved)."""
    transport = _candidate_handler([])
    with httpx.Client(transport=transport) as c:
        out = _query_disambiguation_candidates(c, "http://localhost:3030/bffi", "Mumindalen")
    assert out == []


def test_query_sends_filter_excluding_exact_match() -> None:
    """The SPARQL FILTER must exclude the bare literal itself so the
    report doesn't surface concepts that ARE the bare form when one
    exists (defensive against future YSO additions)."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = parse_qs(request.content.decode("utf-8"))["query"][0]
        return httpx.Response(200, json={"results": {"bindings": []}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        _query_disambiguation_candidates(c, "http://localhost:3030/bffi", "lapset")
    assert 'strstarts(str(?label), "lapset (")' in captured["query"]
    assert 'str(?label) != "lapset"' in captured["query"]


# --- _helmet_bib_ids_for_work --------------------------------------------


def test_helmet_bib_ids_extracted_from_canonical() -> None:
    """``bf:identifiedBy`` URIs of the form
    ``<.../ident/helmet/<bib_id>>`` yield the bare bib ID; other
    identifier URIs are ignored."""
    g = Graph()
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:work:abc")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add(
        (
            work,
            V.BF.identifiedBy,
            URIRef("http://urn.fi/URN:NBN:fi:bib:graph:ident/helmet/2628274"),
        )
    )
    g.add(
        (
            work,
            V.BF.identifiedBy,
            URIRef("http://urn.fi/URN:NBN:fi:bib:graph:ident/helmet/1690010"),
        )
    )
    g.add(
        (
            work,
            V.BF.identifiedBy,
            URIRef("http://example.org/other-identifier"),  # non-Helmet, skipped
        )
    )
    assert _helmet_bib_ids_for_work(g, work) == ["1690010", "2628274"]  # sorted


def test_helmet_bib_ids_returns_empty_for_work_without_identifiers() -> None:
    g = Graph()
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:work:abc")
    g.add((work, RDF.type, V.BFFI.Work))
    assert _helmet_bib_ids_for_work(g, work) == []


# --- _source_tag_for_literal --------------------------------------------


def test_source_tag_read_from_uri_target() -> None:
    g = Graph()
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:work:abc")
    target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/test#Topic650-1")
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal("lapset")))
    g.add(
        (
            target,
            V.BF.source,
            URIRef("http://id.loc.gov/vocabulary/subjectSchemes/ysa"),
        )
    )
    tag = _source_tag_for_literal(g, work, V.BFFI.subject, "lapset")
    assert tag == "ysa"


def test_source_tag_falls_back_to_none_token_when_absent() -> None:
    g = Graph()
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:work:abc")
    target = BNode()
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal("lapset")))
    tag = _source_tag_for_literal(g, work, V.BFFI.subject, "lapset")
    assert tag == "(none)"


# --- walk_disambiguation_residue end-to-end -----------------------------


def _build_residue_graph() -> Graph:
    """Two canonical Works carrying the same Pattern I literal
    'lapset' tagged $2 ysa, one record each."""
    g = Graph()
    for bib_id in ("2099930", "2371438"):
        work = URIRef(f"http://urn.fi/URN:NBN:fi:bib:work:{bib_id}")
        g.add((work, RDF.type, V.BFFI.Work))
        g.add(
            (
                work,
                V.BF.identifiedBy,
                URIRef(f"http://urn.fi/URN:NBN:fi:bib:graph:ident/helmet/{bib_id}"),
            )
        )
        target = URIRef(f"http://urn.fi/URN:NBN:fi:bib:raw/{bib_id}#Topic650-1")
        g.add((work, V.BFFI.subject, target))
        g.add((target, V.RDFS.label, Literal("lapset")))
        g.add(
            (
                target,
                V.BF.source,
                URIRef("http://id.loc.gov/vocabulary/subjectSchemes/ysa"),
            )
        )
    return g


def test_walk_emits_one_row_per_bib_candidate_combination() -> None:
    """Two records x one literal x two candidates = 4 rows."""
    transport = _candidate_handler(
        [
            _bind("http://www.yso.fi/onto/yso/p4354", "lapset (ikäryhmät)"),
            _bind("http://www.yso.fi/onto/yso/p2357", "lapset (perheenjäsenet)"),
        ]
    )
    g = _build_residue_graph()
    with httpx.Client(transport=transport) as c:
        rows = list(walk_disambiguation_residue(g, c, fuseki_url="http://localhost:3030/bffi"))
    assert len(rows) == 4
    bib_ids = {r.helmet_bib_id for r in rows}
    assert bib_ids == {"2099930", "2371438"}
    case_types = {r.case_type for r in rows}
    assert case_types == {"ambiguous"}
    assert {r.n_candidates for r in rows} == {2}


def test_walk_classifies_missed_altlabel_when_exactly_one_candidate() -> None:
    """One disambiguated candidate means no real ambiguity — the
    cataloguer just needs to add $0 with the single available URI."""
    transport = _candidate_handler(
        [_bind("http://www.yso.fi/onto/yso/p8175", "sissit (suomalaiset sotilaat)")]
    )
    g = Graph()
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:work:abc")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add(
        (
            work,
            V.BF.identifiedBy,
            URIRef("http://urn.fi/URN:NBN:fi:bib:graph:ident/helmet/2628274"),
        )
    )
    target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/2628274#Topic650-1")
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal("sissit")))
    with httpx.Client(transport=transport) as c:
        rows = list(walk_disambiguation_residue(g, c, fuseki_url="http://localhost:3030/bffi"))
    assert len(rows) == 1
    assert rows[0].case_type == "missed-altlabel"
    assert rows[0].n_candidates == 1


def test_walk_skips_literals_with_no_disambiguation_candidates() -> None:
    """A literal that has NO matching disambiguated form (e.g. genuinely
    no-candidate residue like 'Mumindalen') should not appear in the
    report — that's a separate cataloguer-side problem."""
    transport = _candidate_handler([])  # always empty
    g = Graph()
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:work:abc")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add(
        (
            work,
            V.BF.identifiedBy,
            URIRef("http://urn.fi/URN:NBN:fi:bib:graph:ident/helmet/2628274"),
        )
    )
    target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/2628274#Topic650-1")
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal("Mumindalen")))
    g.add(
        (
            target,
            V.BF.source,
            URIRef("http://id.loc.gov/vocabulary/subjectSchemes/ysa"),
        )
    )
    with httpx.Client(transport=transport) as c:
        rows = list(walk_disambiguation_residue(g, c, fuseki_url="http://localhost:3030/bffi"))
    assert rows == []


def test_walk_dedupes_fuseki_queries_per_literal() -> None:
    """Two records share the same literal — we should issue exactly
    ONE Fuseki SPARQL round-trip, not one-per-record (corpus-scale
    matters: 800k records may share thousands of subject literals)."""
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "results": {
                    "bindings": [
                        _bind("http://www.yso.fi/onto/yso/p4354", "lapset (ikäryhmät)"),
                    ]
                }
            },
        )

    g = _build_residue_graph()
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        list(walk_disambiguation_residue(g, c, fuseki_url="http://localhost:3030/bffi"))
    assert calls["n"] == 1


# --- write_csv -----------------------------------------------------------


def test_write_csv_emits_stable_column_order_with_utf8_bom(tmp_path: Path) -> None:
    """UTF-8-with-BOM matters because Excel on macOS otherwise mangles
    Finnish characters in å / ä / ö. Column order is the committed
    contract — cataloguer-side scripts depend on it."""
    rows = [
        DisambiguationRow(
            helmet_bib_id="2628274",
            canonical_work_uri="http://urn.fi/URN:NBN:fi:bib:work:abc",
            source_tag="ysa",
            literal="lapset",
            case_type="ambiguous",
            n_candidates=2,
            candidate_uri="http://www.yso.fi/onto/yso/p4354",
            candidate_pref_label="lapset (ikäryhmät)",
        ),
    ]
    out = tmp_path / "report.csv"
    summary = write_csv(rows, out)
    text = out.read_bytes()
    assert text.startswith(b"\xef\xbb\xbf")  # BOM
    decoded = text.decode("utf-8-sig")
    reader = csv.reader(decoded.splitlines())
    assert next(reader) == list(CSV_COLUMNS)
    assert next(reader) == [
        "2628274",
        "http://urn.fi/URN:NBN:fi:bib:work:abc",
        "ysa",
        "lapset",
        "ambiguous",
        "2",
        "http://www.yso.fi/onto/yso/p4354",
        "lapset (ikäryhmät)",
    ]
    assert summary.rows_written == 1
    assert summary.distinct_literals == 1
    assert summary.ambiguous_literals == 1
    assert summary.missed_altlabel_literals == 0
    assert summary.helmet_bib_ids == {"2628274"}


def test_write_csv_atomic_write_does_not_clobber_on_partial_failure(tmp_path: Path) -> None:
    """Atomic write via .tmp + rename: a previous good CSV must
    survive intact if the new write doesn't complete. We can't easily
    simulate a crash, but we can verify the .tmp file disappears on
    success — proving the rename ran."""
    rows = [
        DisambiguationRow(
            helmet_bib_id="x",
            canonical_work_uri="y",
            source_tag="z",
            literal="l",
            case_type="ambiguous",
            n_candidates=2,
            candidate_uri="u",
            candidate_pref_label="lab",
        ),
    ]
    out = tmp_path / "report.csv"
    write_csv(rows, out)
    assert out.exists()
    assert not (tmp_path / "report.csv.tmp").exists()


def test_write_csv_handles_empty_iterable(tmp_path: Path) -> None:
    out = tmp_path / "report.csv"
    summary = write_csv([], out)
    text = out.read_text(encoding="utf-8-sig")
    # Header row only; no data rows.
    assert text.strip() == ",".join(CSV_COLUMNS)
    assert summary.rows_written == 0
    assert summary.distinct_literals == 0


# --- run() ---------------------------------------------------------------


def test_run_end_to_end_writes_report_for_residue_graph(tmp_path: Path) -> None:
    """End-to-end: parse canonical.ttl, walk, query Fuseki, write
    CSV. Uses MockTransport so no live Fuseki dependency."""
    canonical = tmp_path / "canonical.ttl"
    out = tmp_path / "ysa.csv"
    _build_residue_graph().serialize(destination=str(canonical), format="turtle")
    transport = _candidate_handler(
        [
            _bind("http://www.yso.fi/onto/yso/p4354", "lapset (ikäryhmät)"),
            _bind("http://www.yso.fi/onto/yso/p2357", "lapset (perheenjäsenet)"),
        ]
    )
    with httpx.Client(transport=transport) as c:
        summary = run(
            canonical_path=canonical,
            output_path=out,
            fuseki_url="http://localhost:3030/bffi",
            http_client=c,
        )
    assert out.exists()
    assert summary.distinct_literals == 1
    assert summary.ambiguous_literals == 1
    assert summary.helmet_bib_ids == {"2099930", "2371438"}


def test_disambiguation_candidate_is_hashable() -> None:
    """Frozen dataclass + hashability lets the row enrichment code
    set-dedupe candidates safely in future report extensions."""
    a = DisambiguationCandidate(uri="u", pref_label="l")
    b = DisambiguationCandidate(uri="u", pref_label="l")
    assert {a, b} == {a}
