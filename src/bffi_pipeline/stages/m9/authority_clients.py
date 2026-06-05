"""M9 authority-lookup HTTP clients.

Three implementations share the :class:`AuthorityClient` Protocol:

- :class:`FintoSkosmosClient` — production client for Finto's REST
  API (https://api.finto.fi/rest/v1). Per-day cached.
- :class:`ViafClient` — VIAF AutoSuggest fallback for persons /
  corporate bodies that didn't match KANTO.
- :class:`StubAuthorityClient` — test fixture; returns pre-baked
  candidate lists per ``(kind, literal)``.

P-38 Phase B: extracted from m9/runner.py to keep the runner focused
on the reconcile orchestration. No logic change — moves only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

import httpx

if TYPE_CHECKING:
    from bffi_pipeline.stages.m9.schemas import (
        AuthorityCandidate,
        AuthorityKind,
        EntityRequest,
    )

# P-38 Phase D: schemas / lexical helpers live in their own siblings
# now, so authority_clients can import them at module load time without
# pulling in the rest of m9/runner.py's heavy graph (LangChain etc.).
# Kept as late-binding functions for backwards-compatibility — the
# wrapper signature stays identical to the pre-refactor shape.


def _finto_search_query(literal: str) -> str:
    """Append ``*`` for prefix match (Finto's default is exact match).

    Local mirror that forwards to the canonical helper in
    :mod:`bffi_pipeline.stages.m9.schemas`.
    """
    from bffi_pipeline.stages.m9.schemas import _finto_search_query as _real

    return _real(literal)


def _lexical_similarity(a: str, b: str) -> float:
    """Forwards to :func:`bffi_pipeline.stages.m9.lexical.lexical_similarity`."""
    from bffi_pipeline.stages.m9.lexical import lexical_similarity

    return lexical_similarity(a, b)


FINTO_BASE_URL: Final[str] = "https://api.finto.fi/rest/v1"

VOCAB_KANTO: Final[str] = "finaf"
VOCAB_YSO: Final[str] = "yso"
#: YSO-Paikat (places) and YSO-Aika (time periods) are SEPARATE Finto
#: REST vocabularies even though they share the YSO concept namespace.
#: A query like ``?vocab=yso&query=Suomi*`` returns derivative concepts
#: like "Suomi-koulut" and "suomirock", NOT the country
#: ``yso/p94426``. The country lives behind ``vocab=yso-paikat``;
#: time periods behind ``vocab=yso-aika``. Subject reconciliation
#: queries all three vocabs and merges results so place + temporal
#: cataloguer literals get the right candidates.
VOCAB_YSO_PAIKAT: Final[str] = "yso-paikat"
VOCAB_YSO_AIKA: Final[str] = "yso-aika"
VOCAB_KAUNO: Final[str] = "kauno"
VOCAB_MUSO: Final[str] = "muso"
VOCAB_VIAF: Final[str] = "viaf"

DEFAULT_TOP_K: Final[int] = 10


class AuthorityClient(Protocol):
    """Protocol all authority lookups satisfy."""

    def query(
        self, *, request: EntityRequest, top_k: int = DEFAULT_TOP_K
    ) -> list[AuthorityCandidate]:
        """Return up to ``top_k`` candidates for ``request`` from this authority."""
        ...


#: Per ``AuthorityKind``, the tuple of Finto vocab ids to query and
#: merge. Subject queries all three YSO sub-vocabularies because
#: cataloguer 6XX subjects in Helmet routinely mix topical, place,
#: and temporal forms with no ``$2`` discrimination — the merged
#: candidate list lets the lexical-similarity + picker tiers pick
#: the right one regardless of sub-vocab origin.
_KIND_TO_FINTO_VOCABS: Final[dict[str, tuple[str, ...]]] = {
    "person": (VOCAB_KANTO,),
    "corporate_body": (VOCAB_KANTO,),
    "subject": (VOCAB_YSO, VOCAB_YSO_PAIKAT, VOCAB_YSO_AIKA),
    "genre_form": (VOCAB_KAUNO,),
    "music_form": (VOCAB_MUSO,),
}


@dataclass
class FintoSkosmosClient:
    """Real client for Finto's REST API (https://api.finto.fi/rest/v1).

    Caches results per ``(vocab, query, date)`` per spec § 6
    so re-runs within the day don't hammer the public service. Inject
    ``http_client`` (an ``httpx.Client``) so tests can use
    ``httpx.MockTransport`` to assert on the request shape and feed
    canned JSON.
    """

    http_client: httpx.Client
    base_url: str = FINTO_BASE_URL
    today: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())
    _cache: dict[tuple[str, str, str], list[AuthorityCandidate]] = field(default_factory=dict)

    def query(
        self,
        *,
        request: EntityRequest,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[AuthorityCandidate]:
        """Hit Finto's ``/search`` for each ``request.kind``-mapped
        vocab; merge + dedup; cache per-vocab by day.

        When the kind maps to multiple vocabs (e.g. ``subject`` →
        ``yso + yso-paikat + yso-aika``), each vocab's call is cached
        independently so YSO-Paikat lookups for a name like "Suomi"
        survive across runs even when the corpus-wide YSO topical
        cache wasn't useful. Per-vocab dedup is applied as in the
        single-vocab path: same URI from different prefLabel surface
        forms collapses to the first hit (Finto's relevance order).
        """
        vocabs = _KIND_TO_FINTO_VOCABS.get(request.kind)
        if not vocabs:
            return []
        # Tag candidates with their source vocab via the
        # ``source_vocabulary`` field. Per-vocab cache key so a fresh
        # vocab download (load-finto) invalidates that slice cleanly.
        merged: list[AuthorityCandidate] = []
        seen_uris: set[str] = set()
        for vocab in vocabs:
            per_vocab_top_k = max(1, top_k)
            for cand in self._query_one_vocab(
                vocab=vocab,
                literal=request.literal,
                top_k=per_vocab_top_k,
            ):
                if cand.uri in seen_uris:
                    continue
                seen_uris.add(cand.uri)
                merged.append(cand)
                if len(merged) >= top_k:
                    return merged
        return merged

    def _query_one_vocab(
        self,
        *,
        vocab: str,
        literal: str,
        top_k: int,
    ) -> list[AuthorityCandidate]:
        """Hit a single Finto vocab's ``/search`` and return per-URI-
        deduped candidates. Per-day in-memory cache per (vocab, literal)
        so YSO + YSO-Paikat + YSO-Aika at three calls per request stay
        amortised across the run."""
        from bffi_pipeline.stages.m9.schemas import AuthorityCandidate as _AuthorityCandidate

        cache_key = (vocab, literal, self.today)
        if cache_key in self._cache:
            return self._cache[cache_key][:top_k]
        # Finto's `/search` endpoint defaults to exact-match against
        # prefLabel; cataloguer literals like "Puškin, Aleksandr" almost
        # never exact-match a KANTO entry like "Puškin, Aleksandr,
        # 1799-1837". Append `*` for prefix match — the lexical
        # similarity gate downstream still filters spurious matches.
        params = {
            "vocab": vocab,
            "query": _finto_search_query(literal),
            "lang": "fi",
            "maxhits": str(top_k),
        }
        try:
            response = self.http_client.get(f"{self.base_url}/search", params=params, timeout=10.0)
            response.raise_for_status()
        except httpx.HTTPError:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []

        # Finto's /search returns one row per matched lexical variant —
        # an authority entry matched via both prefLabel and altLabel
        # surfaces as two hits with the same URI. Dedupe by URI here so
        # the cataloguer's picker view + the LLM prompt see each
        # authority once. Keep the first occurrence: Finto orders by
        # relevance, so the first hit's pref_label is the strongest
        # match label.
        candidates: list[AuthorityCandidate] = []
        seen_uris: set[str] = set()
        for item in payload.get("results", []):
            uri = item.get("uri")
            pref = item.get("prefLabel") or item.get("matchedPrefLabel") or ""
            if not uri:
                continue
            uri_str = str(uri)
            if uri_str in seen_uris:
                continue
            seen_uris.add(uri_str)
            candidates.append(
                _AuthorityCandidate(
                    uri=uri_str,
                    pref_label=str(pref),
                    source_vocabulary=vocab,
                    lexical_similarity=_lexical_similarity(literal, str(pref)),
                )
            )
        self._cache[cache_key] = candidates
        return candidates[:top_k]


@dataclass
class ViafClient:
    """VIAF lookup. Falls back here only when KANTO returned no person/corporate-body match.

    Phase 1 ships the same shape as :class:`FintoSkosmosClient`; the
    actual VIAF AutoSuggest endpoint is wired in phase 2 alongside the
    CLI subcommand. Tests inject a ``StubAuthorityClient`` instead.
    """

    http_client: httpx.Client
    base_url: str = "https://www.viaf.org/viaf/AutoSuggest"

    def query(
        self,
        *,
        request: EntityRequest,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[AuthorityCandidate]:
        """Hit VIAF's AutoSuggest endpoint; only persons / corporate bodies route here."""
        from bffi_pipeline.stages.m9.schemas import AuthorityCandidate as _AuthorityCandidate

        if request.kind not in {"person", "corporate_body"}:
            return []
        try:
            response = self.http_client.get(
                self.base_url,
                params={"query": request.literal},
                timeout=10.0,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        # Dedupe by URI for the same reason as the Finto path: VIAF's
        # AutoSuggest can surface one entity via multiple alias hits.
        candidates: list[AuthorityCandidate] = []
        seen_uris: set[str] = set()
        for item in payload.get("result", []) or []:
            viaf_id = item.get("viafid") or item.get("id")
            term = item.get("term") or item.get("displayForm") or ""
            if not viaf_id:
                continue
            uri = f"https://viaf.org/viaf/{viaf_id}"
            if uri in seen_uris:
                continue
            seen_uris.add(uri)
            candidates.append(
                _AuthorityCandidate(
                    uri=uri,
                    pref_label=str(term),
                    source_vocabulary=VOCAB_VIAF,
                    lexical_similarity=_lexical_similarity(request.literal, str(term)),
                )
            )
        return candidates[:top_k]


@dataclass
class StubAuthorityClient:
    """Test stub: returns a pre-baked candidate list per (kind, literal)."""

    fixtures: dict[tuple[AuthorityKind, str], list[AuthorityCandidate]] = field(
        default_factory=dict
    )

    def query(
        self,
        *,
        request: EntityRequest,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[AuthorityCandidate]:
        """Look up a wired candidate list for ``(kind, literal)``; default to empty."""
        return list(self.fixtures.get((request.kind, request.literal), []))[:top_k]
