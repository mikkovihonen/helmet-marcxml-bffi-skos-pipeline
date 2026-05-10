"""Unit tests for ``stages/load_finto`` (option 3b — Finto vocab dumps
loaded into our local Fuseki for cross-vocabulary linking in Skosmos).

All HTTP goes through ``httpx.MockTransport``; api.finto.fi and Fuseki
are never contacted. The integration smoke against a real
``docker compose up`` Fuseki + live Finto API is a separate manual
check, mirrored in the M11 runbook checklist."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from bffi_pipeline.stages.load_finto import (
    FINTO_VOCABS,
    FintoVocab,
    VocabResult,
    _is_dump_fresh,
    graph_uri_for_uri,
    run,
)

# --- Mock transport helpers ---------------------------------------------


@dataclass
class _RecordedRequest:
    method: str
    url: str
    accept: str | None
    body: bytes
    params: dict[str, str]


@dataclass
class _Recorder:
    """Routes by URL host: api.finto.fi → dump bytes; localhost → GSP 204."""

    finto_payloads: dict[str, bytes] = field(default_factory=dict)
    """Keyed by full dump URL; value is the body returned for GET."""
    requests: list[_RecordedRequest] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            _RecordedRequest(
                method=request.method,
                url=str(request.url),
                accept=request.headers.get("accept"),
                body=request.content,
                params=dict(request.url.params),
            )
        )
        url = str(request.url)
        if request.method == "GET" and url in self.finto_payloads:
            return httpx.Response(200, content=self.finto_payloads[url])
        if request.method in {"PUT", "POST"} and "/data" in request.url.path:
            return httpx.Response(204)
        return httpx.Response(404, json={"error": f"no handler for {request.method} {url}"})


def _client(recorder: _Recorder) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(recorder), follow_redirects=True)


# --- Single-vocab fixture (small, isolated) -----------------------------


SLM = FintoVocab(
    vocab_id="slm",
    dump_url="https://api.finto.fi/download/slm/slm-skos.ttl",
    graph_uri="http://urn.fi/URN:NBN:fi:au:slm:",
    languages=("fi", "sv"),
)
SLM_DUMP = b"@prefix slm: <http://urn.fi/URN:NBN:fi:au:slm:> . slm:s1 a <skos:Concept> ."


# --- run() ---------------------------------------------------------------


def test_run_downloads_and_uploads_each_vocab_on_cold_cache(tmp_path: Path) -> None:
    """First invocation against an empty cache: one GET per vocab to
    api.finto.fi, then one PUT per vocab to Fuseki's GSP at the
    canonical concept-scheme URI."""
    rec = _Recorder(finto_payloads={SLM.dump_url: SLM_DUMP})
    with _client(rec) as c:
        summary = run(
            output_dir=tmp_path,
            fuseki_url="http://localhost:3030/bffi",
            vocabs=(SLM,),
            http_client=c,
        )
    methods = [r.method for r in rec.requests]
    assert methods == ["GET", "PUT"]
    put = rec.requests[1]
    assert put.params["graph"] == "http://urn.fi/URN:NBN:fi:au:slm:"
    assert put.body == SLM_DUMP
    assert summary.results == [
        VocabResult(
            vocab_id="slm",
            dump_path=tmp_path / "finto-dumps" / "slm-skos.ttl",
            graph_uri="http://urn.fi/URN:NBN:fi:au:slm:",
            bytes_downloaded=len(SLM_DUMP),
            cache_hit=False,
            triples_uploaded=True,
        )
    ]
    assert (tmp_path / "finto-dumps" / "slm-skos.ttl").read_bytes() == SLM_DUMP


def test_run_reuses_fresh_cache_without_redownloading(tmp_path: Path) -> None:
    """A dump younger than ``max_age_days`` is reused — no GET, just the
    PUT to Fuseki. Keeps daily ``make publish`` cheap."""
    dump_path = tmp_path / "finto-dumps" / "slm-skos.ttl"
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    dump_path.write_bytes(SLM_DUMP)
    rec = _Recorder()
    with _client(rec) as c:
        summary = run(
            output_dir=tmp_path,
            fuseki_url="http://localhost:3030/bffi",
            vocabs=(SLM,),
            max_age_days=30,
            http_client=c,
            now=datetime.now(UTC),
        )
    methods = [r.method for r in rec.requests]
    assert methods == ["PUT"]  # cached → only the upload happens
    assert summary.results[0].cache_hit
    assert summary.results[0].bytes_downloaded == 0


def test_run_force_redownloads_even_when_cache_is_fresh(tmp_path: Path) -> None:
    """``--force`` is the operator's escape hatch when Finto pushed a
    correction and we don't want to wait for ``max_age_days``."""
    dump_path = tmp_path / "finto-dumps" / "slm-skos.ttl"
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    dump_path.write_bytes(b"stale content")
    rec = _Recorder(finto_payloads={SLM.dump_url: SLM_DUMP})
    with _client(rec) as c:
        run(
            output_dir=tmp_path,
            fuseki_url="http://localhost:3030/bffi",
            vocabs=(SLM,),
            force=True,
            http_client=c,
        )
    methods = [r.method for r in rec.requests]
    assert methods == ["GET", "PUT"]
    assert dump_path.read_bytes() == SLM_DUMP  # stale content overwritten


def test_run_stale_cache_triggers_redownload(tmp_path: Path) -> None:
    """A dump older than ``max_age_days`` is treated as stale and refreshed."""
    dump_path = tmp_path / "finto-dumps" / "slm-skos.ttl"
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    dump_path.write_bytes(b"stale content")
    # Backdate mtime to 100 days ago.
    old = time.time() - 100 * 24 * 3600
    os.utime(dump_path, (old, old))
    rec = _Recorder(finto_payloads={SLM.dump_url: SLM_DUMP})
    with _client(rec) as c:
        summary = run(
            output_dir=tmp_path,
            fuseki_url="http://localhost:3030/bffi",
            vocabs=(SLM,),
            max_age_days=30,
            http_client=c,
        )
    methods = [r.method for r in rec.requests]
    assert methods == ["GET", "PUT"]
    assert summary.results[0].cache_hit is False
    assert dump_path.read_bytes() == SLM_DUMP


def test_run_propagates_finto_http_errors_loudly(tmp_path: Path) -> None:
    """A 5xx from api.finto.fi must abort the run rather than load
    against an empty graph — silently wiping a good Fuseki graph with
    nothing is a far worse failure than re-running tomorrow."""
    rec_500 = _Recorder()  # no payload registered → mock returns 404 by default
    with _client(rec_500) as c, pytest.raises(httpx.HTTPStatusError):
        run(
            output_dir=tmp_path,
            fuseki_url="http://localhost:3030/bffi",
            vocabs=(SLM,),
            http_client=c,
        )


def test_run_writes_to_canonical_named_graph_per_vocab(tmp_path: Path) -> None:
    """The PUT graph URI must equal ``vocab.graph_uri`` exactly — that's
    the same string Skosmos uses as ``skosmos:sparqlGraph`` in
    config/skosmos-config.ttl, so any divergence breaks the UI lookup."""
    yso = FintoVocab(
        vocab_id="yso",
        dump_url="https://example.test/yso.ttl",
        graph_uri="http://www.yso.fi/onto/yso/",
        languages=("fi", "sv", "en", "se"),
    )
    rec = _Recorder(finto_payloads={yso.dump_url: b"# yso payload"})
    with _client(rec) as c:
        run(
            output_dir=tmp_path,
            fuseki_url="http://localhost:3030/bffi",
            vocabs=(yso,),
            http_client=c,
        )
    put = next(r for r in rec.requests if r.method == "PUT")
    assert put.params["graph"] == "http://www.yso.fi/onto/yso/"


# --- _is_dump_fresh ------------------------------------------------------


def test_is_dump_fresh_returns_false_for_missing_file(tmp_path: Path) -> None:
    assert _is_dump_fresh(tmp_path / "missing.ttl", max_age_days=30, now=datetime.now(UTC)) is False


def test_is_dump_fresh_returns_true_for_recent_file(tmp_path: Path) -> None:
    p = tmp_path / "recent.ttl"
    p.write_bytes(b"x")
    assert _is_dump_fresh(p, max_age_days=30, now=datetime.now(UTC)) is True


# --- graph_uri_for_uri --------------------------------------------------


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("http://www.yso.fi/onto/yso/p16491", "http://www.yso.fi/onto/yso/"),
        ("http://www.yso.fi/onto/kauno/p1522", "http://www.yso.fi/onto/kauno/"),
        ("http://www.yso.fi/onto/muso/p123", "http://www.yso.fi/onto/muso/"),
        ("http://urn.fi/URN:NBN:fi:au:slm:s1073", "http://urn.fi/URN:NBN:fi:au:slm:"),
        ("http://urn.fi/URN:NBN:fi:au:finaf:000123", "http://urn.fi/URN:NBN:fi:au:finaf:"),
        ("http://id.loc.gov/vocabulary/relators/trl", "http://id.loc.gov/vocabulary/relators/"),
        ("http://example.org/something-unrelated", None),
    ],
)
def test_graph_uri_for_uri_routes_known_authority_namespaces(
    uri: str, expected: str | None
) -> None:
    assert graph_uri_for_uri(uri) == expected


# --- Shared graph URI grouping (yso + yso-paikat) -----------------------


def test_run_groups_vocabs_sharing_a_graph_uri_into_one_put_then_post(tmp_path: Path) -> None:
    """yso + yso-paikat both target ``http://www.yso.fi/onto/yso/``.
    The first dump in the group must PUT (clears + loads); subsequent
    dumps in the same group must POST (append). A second PUT would
    clobber the first vocab's triples and silently lose data.
    """
    yso = FintoVocab(
        vocab_id="yso",
        dump_url="https://example.test/yso.ttl",
        graph_uri="http://www.yso.fi/onto/yso/",
        languages=("fi", "sv", "en", "se"),
    )
    paikat = FintoVocab(
        vocab_id="yso-paikat",
        dump_url="https://example.test/yso-paikat.ttl",
        graph_uri="http://www.yso.fi/onto/yso/",
        languages=("fi", "sv", "en"),
    )
    yso_dump = b"@prefix yso: <http://www.yso.fi/onto/yso/> . yso:p1 a <skos:Concept> ."
    paikat_dump = b"@prefix yso: <http://www.yso.fi/onto/yso/> . yso:p2 a <skos:Concept> ."
    rec = _Recorder(
        finto_payloads={
            yso.dump_url: yso_dump,
            paikat.dump_url: paikat_dump,
        }
    )
    with _client(rec) as c:
        run(
            output_dir=tmp_path,
            fuseki_url="http://localhost:3030/bffi",
            vocabs=(yso, paikat),
            http_client=c,
        )
    methods = [r.method for r in rec.requests]
    assert methods == ["GET", "GET", "PUT", "POST"]
    # Both Fuseki uploads target the same graph URI.
    fuseki_requests = [r for r in rec.requests if r.method in {"PUT", "POST"}]
    for r in fuseki_requests:
        assert r.params["graph"] == "http://www.yso.fi/onto/yso/"


# --- Canonical vocab list sanity -----------------------------------------


def test_canonical_vocab_list_covers_expected_authority_vocabularies() -> None:
    """The hard-coded ``FINTO_VOCABS`` list is the project's source of
    truth for which authority vocabularies the pipeline knows about
    (despite the name, ``relators`` is LoC-hosted, not Finto-hosted —
    same load mechanism). A regression test fails loudly if the list
    grows or shrinks unintentionally."""
    assert {v.vocab_id for v in FINTO_VOCABS} == {
        "yso",
        "yso-paikat",
        "finaf",
        "kauno",
        "muso",
        "slm",
        "relators",
    }


# --- RDF/XML to Turtle conversion (LoC relators) ------------------------


def test_run_converts_rdfxml_dump_to_turtle_before_uploading(tmp_path: Path) -> None:
    """LoC's ``id.loc.gov/vocabulary/relators.rdf`` ignores
    ``Accept: text/turtle`` and serves RDF/XML. The download path must
    detect that via the response Content-Type and convert through
    rdflib so ``upload_graph`` can use the same ``text/turtle`` upload
    path as every other vocab."""
    relators = FintoVocab(
        vocab_id="relators",
        dump_url="https://example.test/relators.rdf",
        graph_uri="http://id.loc.gov/vocabulary/relators/",
        languages=("en",),
    )
    rdfxml_payload = (
        b"<?xml version='1.0' encoding='UTF-8'?>"
        b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        b"xmlns:skos='http://www.w3.org/2004/02/skos/core#'>"
        b"<rdf:Description rdf:about='http://id.loc.gov/vocabulary/relators/trl'>"
        b"<skos:prefLabel xml:lang='en'>Translator</skos:prefLabel>"
        b"</rdf:Description>"
        b"</rdf:RDF>"
    )

    @dataclass
    class _Recorder2:
        requests: list[_RecordedRequest] = field(default_factory=list)

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(
                _RecordedRequest(
                    method=request.method,
                    url=str(request.url),
                    accept=request.headers.get("accept"),
                    body=request.content,
                    params=dict(request.url.params),
                )
            )
            if request.method == "GET":
                return httpx.Response(
                    200,
                    content=rdfxml_payload,
                    headers={"content-type": "application/rdf+xml"},
                )
            return httpx.Response(204)  # PUT to GSP

    rec = _Recorder2()
    with httpx.Client(transport=httpx.MockTransport(rec), follow_redirects=True) as c:
        run(
            output_dir=tmp_path,
            fuseki_url="http://localhost:3030/bffi",
            vocabs=(relators,),
            http_client=c,
        )
    put = next(r for r in rec.requests if r.method == "PUT")
    # The body Fuseki receives should be Turtle, not RDF/XML.
    body = put.body
    is_turtle = body.startswith(b"@prefix") or b"a skos:Concept" in body or b"prefLabel" in body
    assert is_turtle
    assert b"<?xml" not in body
