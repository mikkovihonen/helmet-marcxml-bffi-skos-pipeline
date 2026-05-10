"""Stage 3b: download Finto vocab dumps and load them into Fuseki.

The Finto-hosted vocabularies (KANTO, YSO, KAUNO, MUSO, SLM) are the
authority sources M9 reconciles against. Surfacing the URIs as
labelled, clickable links in the bffi-works Skosmos UI requires the
vocab data to live in the same Fuseki Skosmos talks to. This stage
fetches the canonical Turtle dumps from ``api.finto.fi``, caches them
under ``BFFI_DATA_DIR/finto-dumps/``, and PUTs each into its
canonical concept-scheme named graph in Fuseki via the SPARQL Graph
Store Protocol — same plumbing the M10 ``upload_graph`` helper uses.

Idempotent across runs: a local dump younger than ``--max-age-days``
is reused without re-downloading; ``--force`` overrides. Skosmos's
per-vocab entries in ``config/skosmos-config.ttl`` point at the same
graph URIs this stage writes to, so labels light up immediately on
the next page load. Subsequent ``make refresh-finto`` invocations
re-pull fresh dumps and replace the named graphs in place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from urllib.parse import quote

import httpx

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.load import upload_graph

DEFAULT_USER_AGENT: Final[str] = (
    "bffi-pipeline/0.1 (+https://github.com/mikkovihonen/helmet-marcxml-bffi-skos-pipeline)"
)


@dataclass(frozen=True)
class FintoVocab:
    """A Finto-hosted vocabulary we surface in our Skosmos UI.

    ``graph_uri`` is the URI used both as the named-graph identifier
    in our Fuseki and as the ``void:uriSpace`` in
    ``config/skosmos-config.ttl``. The two are deliberately equal — the
    URI namespace IS the concept-scheme URI for every Finto vocab we
    consume.
    """

    vocab_id: str
    dump_url: str
    graph_uri: str
    languages: tuple[str, ...]


#: Canonical vocab list. Dump URLs verified via redirect-following on
#: ``https://api.finto.fi/rest/v1/<vocab>/data?format=text/turtle``;
#: URI namespaces verified against each dump's preamble. KANTO is
#: identified as ``finaf`` in Finto's API per spec § 9 (the ``finaf``
#: vocab serves the Finnish Authority File data displayed under the
#: KANTO brand).
FINTO_VOCABS: Final[tuple[FintoVocab, ...]] = (
    FintoVocab(
        vocab_id="yso",
        dump_url="https://api.finto.fi/download/yso/yso-skos.ttl",
        graph_uri="http://www.yso.fi/onto/yso/",
        languages=("fi", "sv", "en", "se"),
    ),
    FintoVocab(
        vocab_id="finaf",
        dump_url="https://api.finto.fi/download/finaf/finaf-skos.ttl",
        graph_uri="http://urn.fi/URN:NBN:fi:au:finaf:",
        languages=("fi",),
    ),
    FintoVocab(
        vocab_id="kauno",
        dump_url="https://api.finto.fi/download/kauno/kauno-skos.ttl",
        graph_uri="http://www.yso.fi/onto/kauno/",
        languages=("fi", "sv", "en"),
    ),
    FintoVocab(
        vocab_id="muso",
        dump_url="https://api.finto.fi/download/muso/muso-skos.ttl",
        graph_uri="http://www.yso.fi/onto/muso/",
        languages=("fi", "sv"),
    ),
    FintoVocab(
        vocab_id="slm",
        dump_url="https://api.finto.fi/download/slm/slm-skos.ttl",
        graph_uri="http://urn.fi/URN:NBN:fi:au:slm:",
        languages=("fi", "sv"),
    ),
    # MARC Code List for Relators — not Finto-hosted but loaded the
    # same way so Skosmos renders the bf:role URIs the M3
    # contributor-extraction cascade emits (e.g. relators/trl) as
    # labelled, clickable links. Served as RDF/XML; the download path
    # converts to Turtle on the fly. ~130 KB; English-only.
    FintoVocab(
        vocab_id="relators",
        dump_url="https://id.loc.gov/vocabulary/relators.rdf",
        graph_uri="http://id.loc.gov/vocabulary/relators/",
        languages=("en",),
    ),
)


@dataclass
class VocabResult:
    """Per-vocab outcome row reported in the CLI summary."""

    vocab_id: str
    dump_path: Path
    graph_uri: str
    bytes_downloaded: int  # 0 when the cached dump was reused
    cache_hit: bool
    triples_uploaded: bool


@dataclass
class FintoLoadSummary:
    """Aggregate result reported by the ``load-finto`` CLI."""

    fuseki_url: str
    results: list[VocabResult] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Finto vocab load summary (Fuseki: {self.fuseki_url})"]
        for r in self.results:
            mb = r.bytes_downloaded // (1024 * 1024)
            cache = "cached" if r.cache_hit else f"{mb} MB downloaded"
            uploaded = "uploaded" if r.triples_uploaded else "skipped"
            lines.append(f"  {r.vocab_id:6}  {cache:>20}  {uploaded:>10}  → {r.graph_uri}")
        return "\n".join(lines)


def _is_dump_fresh(path: Path, *, max_age_days: int, now: datetime) -> bool:
    """Return True iff ``path`` exists and its mtime is younger than
    ``max_age_days`` relative to ``now``."""
    if not path.exists():
        return False
    age_seconds = now.timestamp() - path.stat().st_mtime
    return age_seconds < max_age_days * 24 * 3600


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


_RDFXML_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"application/rdf+xml", "text/xml", "application/xml"}
)


def _download_dump(
    client: httpx.Client,
    vocab: FintoVocab,
    target_path: Path,
) -> int:
    """GET ``vocab.dump_url`` and write atomically to ``target_path``.

    Returns the number of bytes written. The Finto dump endpoints
    return 302s to ``/download/<vocab>/<vocab>-skos.ttl``; we follow
    them so callers get the real Turtle. ``raise_for_status`` surfaces
    HTTP errors loudly — these are deliberately not caught here, since
    a missing or rate-limited dump means the operator should retry
    rather than have the load proceed against stale data.

    LoC's ``id.loc.gov/vocabulary/relators.rdf`` ignores
    ``Accept: text/turtle`` and serves RDF/XML regardless. We detect
    that via the response Content-Type and re-serialize through rdflib
    so :func:`upload_graph` can use the same ``text/turtle`` upload
    path as every other vocab.
    """
    from rdflib import Graph

    response = client.get(vocab.dump_url, headers={"Accept": "text/turtle"})
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type in _RDFXML_CONTENT_TYPES:
        graph = Graph()
        graph.parse(data=response.content, format="xml")
        payload = graph.serialize(format="turtle").encode("utf-8")
    else:
        payload = response.content
    _atomic_write_bytes(target_path, payload)
    return len(payload)


def run(
    *,
    output_dir: Path | None = None,
    fuseki_url: str | None = None,
    max_age_days: int = 30,
    force: bool = False,
    vocabs: tuple[FintoVocab, ...] = FINTO_VOCABS,
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
) -> FintoLoadSummary:
    """Refresh the Finto-vocab named graphs in Fuseki.

    Per vocab: download the Turtle dump (unless a recent local copy
    exists and ``force`` is False), then PUT it into the corresponding
    named graph via Graph Store Protocol. The PUT replaces the graph
    so a re-run produces a clean graph rather than accumulating stale
    triples from old dumps.
    """
    settings = get_settings()
    base = output_dir or settings.data_dir
    fuseki = fuseki_url or settings.fuseki_url
    dumps_dir = base / "finto-dumps"
    dumps_dir.mkdir(parents=True, exist_ok=True)
    summary = FintoLoadSummary(fuseki_url=fuseki)
    timestamp = now or datetime.now(UTC)

    owned_client = http_client is None
    if owned_client:
        http_client = httpx.Client(
            timeout=httpx.Timeout(60.0, read=300.0),
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
    assert http_client is not None  # narrowing for mypy

    try:
        for vocab in vocabs:
            dump_path = dumps_dir / f"{vocab.vocab_id}-skos.ttl"
            cache_hit = not force and _is_dump_fresh(
                dump_path, max_age_days=max_age_days, now=timestamp
            )
            bytes_downloaded = 0 if cache_hit else _download_dump(http_client, vocab, dump_path)

            upload_graph(
                http_client,
                fuseki_url=fuseki,
                graph_uri=vocab.graph_uri,
                ttl_paths=[dump_path],
            )
            summary.results.append(
                VocabResult(
                    vocab_id=vocab.vocab_id,
                    dump_path=dump_path,
                    graph_uri=vocab.graph_uri,
                    bytes_downloaded=bytes_downloaded,
                    cache_hit=cache_hit,
                    triples_uploaded=True,
                )
            )
    finally:
        if owned_client:
            http_client.close()

    return summary


def graph_uri_for_uri(uri: str) -> str | None:
    """Return the Finto vocab graph URI a given resource URI belongs to,
    or ``None`` if it isn't from a known Finto namespace.

    Used by :mod:`bffi_pipeline.cli` and :mod:`bffi_pipeline.stages.reconcile`
    to route URIs to the right vocab when constructing Skosmos links —
    keeps the namespace mapping in one place rather than scattered
    string-prefix checks across stages.
    """
    for vocab in FINTO_VOCABS:
        if uri.startswith(vocab.graph_uri):
            return vocab.graph_uri
    return None


# Re-export the URL-encoding helper used in tests when constructing
# expected GSP request URLs.
quote_graph_param = quote


__all__ = [
    "DEFAULT_USER_AGENT",
    "FINTO_VOCABS",
    "FintoLoadSummary",
    "FintoVocab",
    "VocabResult",
    "graph_uri_for_uri",
    "run",
]
