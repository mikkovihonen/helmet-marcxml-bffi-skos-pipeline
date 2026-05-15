"""Unit tests for stages/load (M10 phase 2).

All HTTP traffic goes through ``httpx.MockTransport``; no live Fuseki
is contacted. The integration test against a real ``docker compose up``
Fuseki lives separately and is a manual M5-Max smoke check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from bffi_pipeline.stages.m10.load import (
    LoadResult,
    SmokeResult,
    delete_graph,
    load_smoke_queries,
    lookup_helmet_id,
    render_helmet_lookup,
    run,
    run_ask,
    run_select,
    upload_graph,
)

# --- Mock transport helpers ---------------------------------------------


@dataclass
class _RecordedRequest:
    """One captured HTTP request for assertion."""

    method: str
    url: str
    content_type: str | None
    accept: str | None
    body: bytes
    params: dict[str, str]


@dataclass
class _Recorder:
    """Records every request and dispatches a canned response per (method, path)."""

    handlers: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = field(
        default_factory=dict
    )
    requests: list[_RecordedRequest] = field(default_factory=list)
    default_response: Callable[[httpx.Request], httpx.Response] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(
            _RecordedRequest(
                method=request.method,
                url=str(request.url),
                content_type=request.headers.get("content-type"),
                accept=request.headers.get("accept"),
                body=request.content,
                params=params,
            )
        )
        key = (request.method, request.url.path)
        handler = self.handlers.get(key, self.default_response)
        if handler is None:
            return httpx.Response(404, json={"error": f"no handler for {key}"})
        return handler(request)


def _client(recorder: _Recorder) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(recorder))


# --- upload_graph -------------------------------------------------------


def test_upload_graph_uses_put_with_text_turtle(tmp_path: Path) -> None:
    ttl = tmp_path / "in.ttl"
    ttl.write_text("@prefix ex: <http://example.org/> . ex:a ex:b ex:c .", encoding="utf-8")
    rec = _Recorder(default_response=lambda _: httpx.Response(204))
    with _client(rec) as c:
        upload_graph(
            c,
            fuseki_url="http://localhost:3030/bffi",
            graph_uri="http://urn.fi/URN:NBN:fi:bib:graph:bffi-works",
            ttl_paths=[ttl],
        )
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "PUT"
    assert req.url.startswith("http://localhost:3030/bffi/data?")
    assert req.params["graph"] == "http://urn.fi/URN:NBN:fi:bib:graph:bffi-works"
    assert req.content_type == "text/turtle"
    assert b"ex:a" in req.body


def test_upload_graph_appends_subsequent_files_via_post(tmp_path: Path) -> None:
    """Multi-file uploads: PUT first (clears graph), POST the rest (appends)."""
    a = tmp_path / "a.ttl"
    a.write_text("@prefix ex: <http://example.org/> . ex:a ex:b ex:c .", encoding="utf-8")
    b = tmp_path / "b.ttl"
    b.write_text("@prefix ex: <http://example.org/> . ex:d ex:e ex:f .", encoding="utf-8")

    rec = _Recorder(default_response=lambda _: httpx.Response(204))
    with _client(rec) as c:
        upload_graph(
            c,
            fuseki_url="http://localhost:3030/bffi",
            graph_uri="http://urn.fi/URN:NBN:fi:bib:graph:bffi-works",
            ttl_paths=[a, b],
        )
    assert [r.method for r in rec.requests] == ["PUT", "POST"]
    assert all(r.content_type == "text/turtle" for r in rec.requests)


def test_upload_graph_empty_paths_is_a_noop() -> None:
    rec = _Recorder()
    with _client(rec) as c:
        upload_graph(
            c,
            fuseki_url="http://localhost:3030/bffi",
            graph_uri="http://urn.fi/URN:NBN:fi:bib:graph:bffi-works",
            ttl_paths=[],
        )
    assert rec.requests == []


def test_upload_graph_propagates_http_errors(tmp_path: Path) -> None:
    ttl = tmp_path / "in.ttl"
    ttl.write_text("@prefix ex: <http://example.org/> . ex:a ex:b ex:c .", encoding="utf-8")
    rec = _Recorder(default_response=lambda _: httpx.Response(500, text="boom"))
    with _client(rec) as c, pytest.raises(httpx.HTTPStatusError):
        upload_graph(
            c,
            fuseki_url="http://localhost:3030/bffi",
            graph_uri="http://urn.fi/URN:NBN:fi:bib:graph:bffi-works",
            ttl_paths=[ttl],
        )


# --- delete_graph -------------------------------------------------------


def test_delete_graph_returns_true_on_204() -> None:
    rec = _Recorder(default_response=lambda _: httpx.Response(204))
    with _client(rec) as c:
        ok = delete_graph(
            c,
            fuseki_url="http://localhost:3030/bffi",
            graph_uri="http://urn.fi/URN:NBN:fi:bib:graph:bffi-works",
        )
    assert ok is True
    assert rec.requests[0].method == "DELETE"


def test_delete_graph_swallows_http_errors() -> None:
    """Rollback path must not raise on a follow-up error."""
    rec = _Recorder(default_response=lambda _: httpx.Response(500))
    with _client(rec) as c:
        ok = delete_graph(
            c,
            fuseki_url="http://localhost:3030/bffi",
            graph_uri="http://urn.fi/URN:NBN:fi:bib:graph:bffi-works",
        )
    assert ok is False


# --- run_ask / run_select ----------------------------------------------


def test_run_ask_parses_boolean_response() -> None:
    rec = _Recorder(default_response=lambda _: httpx.Response(200, json={"boolean": True}))
    with _client(rec) as c:
        ok = run_ask(c, fuseki_url="http://localhost:3030/bffi", query="ASK { ?s ?p ?o }")
    assert ok is True
    assert rec.requests[0].method == "POST"
    assert rec.requests[0].accept == "application/sparql-results+json"


def test_run_ask_returns_false_when_endpoint_says_so() -> None:
    rec = _Recorder(default_response=lambda _: httpx.Response(200, json={"boolean": False}))
    with _client(rec) as c:
        ok = run_ask(c, fuseki_url="http://localhost:3030/bffi", query="ASK { ?s ?p ?o }")
    assert ok is False


def test_run_select_parses_bindings() -> None:
    payload = {
        "head": {"vars": ["s", "o"]},
        "results": {
            "bindings": [
                {
                    "s": {"type": "uri", "value": "http://example.org/a"},
                    "o": {"type": "literal", "value": "label-a"},
                },
                {
                    "s": {"type": "uri", "value": "http://example.org/b"},
                    "o": {"type": "literal", "value": "label-b"},
                },
            ]
        },
    }
    rec = _Recorder(default_response=lambda _: httpx.Response(200, json=payload))
    with _client(rec) as c:
        rows = run_select(
            c, fuseki_url="http://localhost:3030/bffi", query="SELECT * WHERE { ?s ?p ?o }"
        )
    assert len(rows) == 2
    assert rows[0]["s"]["value"] == "http://example.org/a"


# --- load_smoke_queries -------------------------------------------------


def test_load_smoke_queries_splits_on_section_markers() -> None:
    queries = load_smoke_queries()
    names = [q.name for q in queries]
    assert "Skosify dual-typing" in names
    assert "Expressions linked to Works" in names
    assert "Helmet identifiers preserved on canonical Works" in names
    assert "Skosify-inferred SKOS inverses (narrower/broader)" in names
    assert len(queries) == 4
    for q in queries:
        assert q.query.startswith("PREFIX")
        assert "ASK" in q.query


def test_load_smoke_queries_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_smoke_queries(tmp_path / "missing.rq")


# --- lookup_helmet_id ---------------------------------------------------


def test_lookup_helmet_id_substitutes_helmet_id_into_query() -> None:
    captured_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # The query is form-urlencoded in the body.
        body = request.content.decode("utf-8")
        # crude parse — we only need to assert the helmet_id appears.
        captured_query["body"] = body
        return httpx.Response(
            200,
            json={"head": {"vars": []}, "results": {"bindings": []}},
        )

    rec = _Recorder(default_response=handler)
    with _client(rec) as c:
        rows = lookup_helmet_id(c, "12345678", fuseki_url="http://localhost:3030/bffi")
    assert rows == []
    assert "12345678" in captured_query["body"]


def test_lookup_helmet_id_renders_select_results_for_humans() -> None:
    rows = [
        {
            "canonicalWork": {"type": "uri", "value": "http://urn.fi/.../work:abc"},
            "canonicalLabel": {"type": "literal", "value": "Sota ja rauha"},
            "expression": {"type": "uri", "value": "http://urn.fi/.../expression:abc"},
            "expressionLabel": {"type": "literal", "value": "Война и мир"},
        }
    ]
    rendered = render_helmet_lookup(rows)
    assert "canonical Work: http://urn.fi/.../work:abc" in rendered
    assert "Sota ja rauha" in rendered
    assert "expression:" in rendered
    assert "Война и мир" in rendered


def test_lookup_helmet_id_renders_empty_for_no_matches() -> None:
    assert "no canonical Work found" in render_helmet_lookup([])


# --- run() orchestrator -------------------------------------------------


@pytest.fixture
def synthetic_corpus(tmp_path: Path) -> dict[str, Path]:
    skosified = tmp_path / "canonical-skosified.ttl"
    skosified.write_text(
        "@prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .\n"
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n"
        "<http://example.org/work/1> a bffi:Work, skos:Concept ;\n"
        '    skos:prefLabel "x" .\n',
        encoding="utf-8",
    )
    admin_vocab = tmp_path / "admin-vocab.ttl"
    admin_vocab.write_text(
        "@prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .\n"
        "<http://example.org/agent/1> a bffi:Agent .\n",
        encoding="utf-8",
    )
    provenance = tmp_path / "provenance.ttl"
    provenance.write_text(
        "@prefix prov: <http://www.w3.org/ns/prov#> .\n"
        "<http://example.org/activity/1> a prov:Activity .\n",
        encoding="utf-8",
    )
    return {"skosified": skosified, "admin": admin_vocab, "provenance": provenance}


def _smoke_handler_factory(
    pass_count: int,
    fail_count: int = 0,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that answers the first N ASKs with true, the rest with false."""
    state = {"asks_seen": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sparql"):
            state["asks_seen"] += 1
            value = state["asks_seen"] <= pass_count
            return httpx.Response(200, json={"boolean": value})
        return httpx.Response(204)

    return handler


def test_run_uploads_then_runs_smokes_and_succeeds_when_all_pass(
    synthetic_corpus: dict[str, Path],
) -> None:
    rec = _Recorder(default_response=_smoke_handler_factory(pass_count=4))
    with _client(rec) as c:
        result = run(
            skosified_path=synthetic_corpus["skosified"],
            admin_vocab_path=synthetic_corpus["admin"],
            provenance_path=synthetic_corpus["provenance"],
            fuseki_url="http://localhost:3030/bffi",
            client=c,
        )
    assert result.success is True
    assert result.rolled_back is False
    assert len(result.bffi_works_uploaded) == 2
    assert len(result.provenance_uploaded) == 1
    assert all(s.passed for s in result.smoke_results)
    methods = [r.method for r in rec.requests]
    # PUT bffi-works, POST admin-vocab, PUT provenance, then 4 POST sparql
    assert methods.count("PUT") == 2
    assert methods.count("POST") >= 4 + 1  # at least 4 ASKs + 1 admin vocab append


def test_run_rolls_back_when_a_smoke_query_fails(
    synthetic_corpus: dict[str, Path],
) -> None:
    rec = _Recorder(default_response=_smoke_handler_factory(pass_count=2, fail_count=2))
    with _client(rec) as c:
        result = run(
            skosified_path=synthetic_corpus["skosified"],
            admin_vocab_path=synthetic_corpus["admin"],
            provenance_path=synthetic_corpus["provenance"],
            fuseki_url="http://localhost:3030/bffi",
            client=c,
        )
    assert result.success is False
    assert result.rolled_back is True
    failed_count = sum(1 for s in result.smoke_results if not s.passed)
    assert failed_count >= 1
    assert "DELETE" in [r.method for r in rec.requests]


def test_run_skips_provenance_upload_when_file_missing(
    synthetic_corpus: dict[str, Path], tmp_path: Path
) -> None:
    rec = _Recorder(default_response=_smoke_handler_factory(pass_count=4))
    with _client(rec) as c:
        result = run(
            skosified_path=synthetic_corpus["skosified"],
            admin_vocab_path=synthetic_corpus["admin"],
            provenance_path=tmp_path / "missing-provenance.ttl",
            fuseki_url="http://localhost:3030/bffi",
            client=c,
        )
    assert result.provenance_uploaded == []  # no provenance file → no upload
    assert result.success is True


def test_run_raises_when_neither_skosified_nor_admin_vocab_exists(
    tmp_path: Path,
) -> None:
    rec = _Recorder()
    with _client(rec) as c, pytest.raises(FileNotFoundError):
        run(
            skosified_path=tmp_path / "missing.ttl",
            admin_vocab_path=tmp_path / "also-missing.ttl",
            provenance_path=tmp_path / "no-provenance.ttl",
            fuseki_url="http://localhost:3030/bffi",
            client=c,
        )


# --- LoadResult.render --------------------------------------------------


def test_load_result_render_announces_success() -> None:
    result = LoadResult(
        fuseki_url="http://localhost:3030/bffi",
        bffi_works_graph="http://example.org/g/works",
        provenance_graph="http://example.org/g/prov",
        bffi_works_uploaded=["a.ttl"],
        provenance_uploaded=["p.ttl"],
        smoke_results=[SmokeResult(name="dual-typing", passed=True)],
        success=True,
    )
    text = result.render()
    assert "M10 load complete" in text
    assert "PASS" in text


def test_load_result_render_announces_rollback_on_failure() -> None:
    result = LoadResult(
        fuseki_url="http://localhost:3030/bffi",
        bffi_works_graph="http://example.org/g/works",
        provenance_graph="http://example.org/g/prov",
        bffi_works_uploaded=["a.ttl"],
        smoke_results=[SmokeResult(name="dual-typing", passed=False, error="empty result")],
        rolled_back=True,
        success=False,
    )
    text = result.render()
    assert "M10 load FAILED" in text
    assert "FAIL" in text
    assert "ROLLED BACK" in text
