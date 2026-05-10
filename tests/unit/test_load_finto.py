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
        if request.method == "PUT" and "/data" in request.url.path:
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
        ("http://example.org/something-unrelated", None),
    ],
)
def test_graph_uri_for_uri_routes_known_finto_namespaces(uri: str, expected: str | None) -> None:
    assert graph_uri_for_uri(uri) == expected


# --- Canonical vocab list sanity -----------------------------------------


def test_canonical_vocab_list_covers_the_five_finto_vocabularies() -> None:
    """The hard-coded ``FINTO_VOCABS`` list is the project's source of
    truth for which Finto vocabularies the pipeline knows about. A
    regression test fails loudly if the list grows or shrinks
    unintentionally."""
    assert {v.vocab_id for v in FINTO_VOCABS} == {"yso", "finaf", "kauno", "muso", "slm"}
