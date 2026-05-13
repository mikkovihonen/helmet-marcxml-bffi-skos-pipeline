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

from bffi_pipeline.blocking import fold_label
from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.load import upload_graph

#: Predicate used to surface the canonical-fold of every concept's
#: ``skos:prefLabel`` and ``skos:altLabel``. Materialised at load time
#: per P-10 Phase C.1 so the resolver's tier-0 SPARQL can match against
#: a pre-folded literal — Fuseki has no ``fold_diacritics`` builtin, so
#: the alternative is folding every authority label per query, which is
#: orders of magnitude more expensive.
BFFI_FOLDED_LABEL_URI: Final[str] = "http://urn.fi/URN:NBN:fi:schema:bffi:foldedLabel"

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
    # YSO-Paikat (places) and YSO-Aika (time periods) share the YSO
    # concept namespace — ``http://www.yso.fi/onto/yso/p104995`` is
    # "Lontoo" in places, ``.../p6201062019`` is "2010-luku" in time,
    # ``.../p12279`` is "äidit" in YSO general topics. We load both
    # auxiliary dumps into the SAME Fuseki named graph as YSO so the
    # existing Skosmos ``:yso`` vocab entry (uriSpace + sparqlGraph
    # both equal to the YSO URI namespace) renders place + temporal
    # URIs as labelled clickable concepts without separate Skosmos
    # vocabs, and so M9 tier-0 finds their prefLabels via the same
    # SPARQL query as topic prefLabels. Cataloguer MARC ``$2 yso/fin``
    # tagging is the same across all three sub-vocabularies.
    FintoVocab(
        vocab_id="yso-paikat",
        dump_url="https://api.finto.fi/download/yso-paikat/yso-paikat-skos.ttl",
        graph_uri="http://www.yso.fi/onto/yso/",
        languages=("fi", "sv", "en"),
    ),
    FintoVocab(
        vocab_id="yso-aika",
        dump_url="https://api.finto.fi/download/yso-aika/yso-aika-skos.ttl",
        graph_uri="http://www.yso.fi/onto/yso/",
        languages=("fi", "sv"),
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
    # KAUNOKKI/BELLA — the legacy KAUNO thesaurus, with the Swedish
    # parallel labels under the Bella sub-vocab. Cataloguers tag
    # ``$2 kaunokki`` (Finnish form) or ``$2 bella`` (Swedish form) on
    # MARC 6XX for fiction material — the 200-record corpus smoke
    # surfaced ~10 Bella-tagged Swedish-language records that fell into
    # tier-0 ``no-candidate`` because the underlying labels live in
    # Kaunokki's graph (separate URI namespace from KAUNO), not loaded
    # by the original M11 3b pass. Separate Fuseki named graph because
    # the URI namespace (``http://urn.fi/URN:NBN:fi:au:kaunokki:``)
    # doesn't overlap KAUNO's (``http://www.yso.fi/onto/kauno/``).
    FintoVocab(
        vocab_id="kaunokki",
        dump_url="https://api.finto.fi/download/kaunokki/kaunokki-skos.ttl",
        graph_uri="http://urn.fi/URN:NBN:fi:au:kaunokki:",
        languages=("fi", "sv"),
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
    # Allärs — the Swedish General Thesaurus, Allmän tesaurus på
    # svenska. Cataloguers tag ``$2 allars`` on Swedish-language MARC
    # 6XX subjects (parallel to YSA/YSO on the Finnish side). The
    # 200-record corpus smoke surfaced 10 Allars-tagged entries; at
    # 800k scale Swedish-language records are a significant minority
    # of Helmet. Allars lives in its own URI namespace under
    # ``http://www.yso.fi/onto/allars/`` so it loads to a separate
    # Fuseki named graph; tier-0 ``subject`` routing adds Allars
    # between YSO (Finnish-first) and LCSH (English-last).
    FintoVocab(
        vocab_id="allars",
        dump_url="https://api.finto.fi/download/allars/allars-skos.ttl",
        graph_uri="http://www.yso.fi/onto/allars/",
        languages=("sv",),
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
    # LC Genre/Form Terms — Helmet cataloguers cite English genre/form
    # URIs (e.g. ``http://id.loc.gov/authorities/genreForms/gf2015026020``
    # for "Novels", ``.../gf2014026542`` for "Short stories") on MARC
    # 655 fields without further translation. Loading the LCGFT dump
    # both makes ``graph_uri_for_uri`` recognise those URIs (so M9
    # walkers correctly skip them as already-resolved) and gives
    # Skosmos a graph to render English labels from. Served gzipped;
    # the download path detects ``.gz`` and decompresses on the fly.
    # ~330 KB compressed; English-only.
    FintoVocab(
        vocab_id="lcgft",
        dump_url="https://id.loc.gov/download/authorities/genreForms.skosrdf.ttl.gz",
        graph_uri="http://id.loc.gov/authorities/genreForms/",
        languages=("en",),
    ),
    # LC Subject Headings — the general English subject thesaurus.
    # Cataloguers occasionally use ``$2 lcsh`` for topical subjects on
    # records copied or harmonised from English-language sources, with
    # the literal heading carried as ``rdfs:label``. Tier-0 routes
    # ``subject``-kind requests through both YSO and LCSH so those
    # English literals bind deterministically without a Finto API call.
    # Served gzipped; ~39 MB compressed (~250-500 MB uncompressed
    # depending on rdflib's serialisation density), the largest dump
    # after KANTO. English-only.
    FintoVocab(
        vocab_id="lcsh",
        dump_url="https://id.loc.gov/download/authorities/subjects.skosrdf.ttl.gz",
        graph_uri="http://id.loc.gov/authorities/subjects/",
        languages=("en",),
    ),
    # LC Children's Subject Headings — a subset of LCSH tuned for
    # juvenile-collection cataloguing. Cataloguers tag ``$2 lcsh``
    # for children's-collection records on translated English imports
    # ("Jukka Hukka (fiktiivinen hahmo)" → matching English form).
    # Same gzipped Turtle wire format as LCSH/LCGFT, ~1.8 MB
    # compressed. Separate URI namespace
    # (``http://id.loc.gov/authorities/childrensSubjects/``) so it
    # loads to its own Fuseki named graph; tier-0 ``subject`` routing
    # adds it after LCSH.
    FintoVocab(
        vocab_id="childrensSubjects",
        dump_url="https://id.loc.gov/download/authorities/childrensSubjects.skosrdf.ttl.gz",
        graph_uri="http://id.loc.gov/authorities/childrensSubjects/",
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


def materialise_folded_labels(dump_path: Path) -> int:
    """Parse ``dump_path``, add ``bffi:foldedLabel`` triples for every
    ``skos:prefLabel`` and ``skos:altLabel``, re-serialise atomically.

    Returns the number of new triples added (zero on a no-op re-run
    against an already-materialised dump). Idempotent — rdflib's
    ``Graph.add`` is a set operation, so re-materialising adds nothing
    new for labels that already carry a folded form.

    The fold composes :func:`bffi_pipeline.blocking.fold_label` (NFKC
    + diacritic-fold + casefold + whitespace-collapse + strip-trailing-
    date / role-marker). Same fold runs on the cataloguer literal in
    :class:`bffi_pipeline.stages.local_concept_resolver.FusekiConceptResolver`
    so the folded forms align byte-for-byte across load and lookup.
    """
    from rdflib import Graph, Literal, URIRef
    from rdflib.namespace import SKOS

    graph = Graph()
    graph.parse(source=str(dump_path), format="turtle")
    folded_pred = URIRef(BFFI_FOLDED_LABEL_URI)

    before = len(graph)
    for label_pred in (SKOS.prefLabel, SKOS.altLabel):
        # Snapshot the iterable; modifying the graph mid-iteration is
        # not guaranteed safe in rdflib's store backends.
        for subject, _, label in list(graph.triples((None, label_pred, None))):
            if not isinstance(label, Literal):
                continue
            folded_value = fold_label(str(label))
            if not folded_value:
                continue
            graph.add((subject, folded_pred, Literal(folded_value)))
    added = len(graph) - before

    payload = graph.serialize(format="turtle").encode("utf-8")
    _atomic_write_bytes(dump_path, payload)
    return added


_RDFXML_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"application/rdf+xml", "text/xml", "application/xml"}
)


def _download_dump(
    client: httpx.Client,
    vocab: FintoVocab,
    target_path: Path,
) -> int:
    """GET ``vocab.dump_url`` and write atomically to ``target_path``.

    Returns the number of bytes written (after any decompression). The
    Finto dump endpoints return 302s to ``/download/<vocab>/<vocab>-skos.ttl``;
    we follow them so callers get the real Turtle. ``raise_for_status``
    surfaces HTTP errors loudly — these are deliberately not caught
    here, since a missing or rate-limited dump means the operator should
    retry rather than have the load proceed against stale data.

    Two non-Turtle wire formats are normalised to Turtle on the way in:

    - **RDF/XML** — LoC's ``id.loc.gov/vocabulary/relators.rdf`` ignores
      ``Accept: text/turtle`` and serves RDF/XML regardless. Detected
      via the response Content-Type and re-serialised through rdflib.
    - **Gzipped Turtle** — LoC's bulk authority dumps
      (``id.loc.gov/download/authorities/*.skosrdf.ttl.gz``) are only
      published gzipped; the server sends ``Content-Encoding: identity``
      because the gzip is part of the payload format. Detected via the
      ``.gz`` URL suffix and decompressed before saving.

    Decompression / conversion happens once at download time so
    :func:`upload_graph` always sees ``text/turtle`` regardless of what
    the upstream wire format was.
    """
    import gzip

    from rdflib import Graph

    response = client.get(vocab.dump_url, headers={"Accept": "text/turtle"})
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type in _RDFXML_CONTENT_TYPES:
        graph = Graph()
        graph.parse(data=response.content, format="xml")
        payload = graph.serialize(format="turtle").encode("utf-8")
    elif vocab.dump_url.endswith(".gz"):
        payload = gzip.decompress(response.content)
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
    fold_pref_labels: bool = False,
) -> FintoLoadSummary:
    """Refresh the Finto-vocab named graphs in Fuseki.

    Per vocab: download the Turtle dump (unless a recent local copy
    exists and ``force`` is False), then PUT it into the corresponding
    named graph via Graph Store Protocol. The PUT replaces the graph
    so a re-run produces a clean graph rather than accumulating stale
    triples from old dumps.

    ``fold_pref_labels=True`` post-processes each downloaded dump by
    adding ``bffi:foldedLabel`` triples for every ``skos:prefLabel``
    and ``skos:altLabel`` (P-10 Phase C.1). The materialised triples
    are inert until the **resolver-side** feature flag
    ``BFFI_M9_TIER0_EXPANSION`` is also enabled — using the tier-0
    expansion path therefore needs **both** flags flipped on.

    **Default is False** because the 2026-05-13 Phase C bench attempt
    (see `docs/performance/2026-05-13-5k-m2-max-phase-c-attempt.md`)
    found Phase 1 SPARQL traffic ~doubled with the materialised
    predicate present, *without* an offsetting reduction in tier-2
    picker calls on the May 12 corpus. Until a clean re-bench
    demonstrates a net win, both flags stay default-off so
    ``load-finto`` runs and Fuseki query times match the post-Phase-A2
    baseline. Operators opting into tier-0 expansion flip
    ``--fold-pref-labels`` here AND ``BFFI_M9_TIER0_EXPANSION=1`` at
    reconcile time.
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
        # Connect / pool default to 60 s. Read 300 s for the long-poll
        # downloads (KANTO is ~183 MB, LCSH ~39 MB compressed). Write
        # 1800 s (30 min) because every PUT to Fuseki re-uploads the
        # full Turtle payload — at LCSH's ~465 MB decompressed size the
        # POST body alone can exceed the default 60 s write timeout.
        http_client = httpx.Client(
            timeout=httpx.Timeout(60.0, read=300.0, write=1800.0),
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
    assert http_client is not None  # narrowing for mypy

    try:
        # Download every dump first; group uploads by graph_uri so vocabs
        # that share a named graph (e.g. yso + yso-paikat both targeting
        # the YSO graph) get a single PUT (clears + loads the first dump)
        # followed by POSTs (appends each subsequent dump) per the
        # ``upload_graph`` multi-path protocol. Without this grouping the
        # second vocab's PUT would clobber the first.
        per_vocab_state: list[tuple[FintoVocab, Path, int, bool]] = []
        for vocab in vocabs:
            dump_path = dumps_dir / f"{vocab.vocab_id}-skos.ttl"
            cache_hit = not force and _is_dump_fresh(
                dump_path, max_age_days=max_age_days, now=timestamp
            )
            bytes_downloaded = 0 if cache_hit else _download_dump(http_client, vocab, dump_path)
            if fold_pref_labels and bytes_downloaded > 0:
                # Materialise bffi:foldedLabel on freshly-downloaded dumps.
                # Cached dumps are skipped to avoid re-parsing multi-hundred-
                # MB Turtle on every load. Operators flipping the flag for
                # the first time after Phase C ships should pass --force to
                # rebuild the cache.
                materialise_folded_labels(dump_path)
            per_vocab_state.append((vocab, dump_path, bytes_downloaded, cache_hit))

        groups: dict[str, list[Path]] = {}
        for vocab, dump_path, _bytes, _cache in per_vocab_state:
            groups.setdefault(vocab.graph_uri, []).append(dump_path)
        for graph_uri, dump_paths in groups.items():
            upload_graph(
                http_client,
                fuseki_url=fuseki,
                graph_uri=graph_uri,
                ttl_paths=dump_paths,
            )

        for vocab, dump_path, bytes_downloaded, cache_hit in per_vocab_state:
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
