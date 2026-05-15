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
    from bffi_pipeline.stages.m9.runner import (
        AuthorityCandidate,
        AuthorityKind,
        EntityRequest,
    )

# Import these at runtime via late-binding helper to avoid circular
# imports at module-load time (runner.py imports this module).


def _finto_search_query(literal: str) -> str:
    """Append ``*`` for prefix match (Finto's default is exact match).

    Local mirror so this module doesn't depend on runner's identical
    helper.
    """
    from bffi_pipeline.stages.m9.runner import _finto_search_query as _real

    return _real(literal)


def _lexical_similarity(a: str, b: str) -> float:
    """Late-bound import to runner's similarity helper."""
    from bffi_pipeline.stages.m9.runner import lexical_similarity

    return lexical_similarity(a, b)


FINTO_BASE_URL: Final[str] = "https://api.finto.fi/rest/v1"

VOCAB_KANTO: Final[str] = "finaf"
VOCAB_YSO: Final[str] = "yso"
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


_KIND_TO_FINTO_VOCAB: Final[dict[str, str]] = {
    "person": VOCAB_KANTO,
    "corporate_body": VOCAB_KANTO,
    "subject": VOCAB_YSO,
    "genre_form": VOCAB_KAUNO,
    "music_form": VOCAB_MUSO,
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
        """Hit Finto's ``/search`` endpoint for ``request.kind``-mapped vocab; cache by day."""
        from bffi_pipeline.stages.m9.runner import AuthorityCandidate as _AuthorityCandidate

        vocab = _KIND_TO_FINTO_VOCAB.get(request.kind)
        if vocab is None:
            return []
        cache_key = (vocab, request.literal, self.today)
        if cache_key in self._cache:
            return self._cache[cache_key][:top_k]
        # Finto's `/search` endpoint defaults to exact-match against
        # prefLabel; cataloguer literals like "Puškin, Aleksandr" almost
        # never exact-match a KANTO entry like "Puškin, Aleksandr,
        # 1799-1837". Append `*` for prefix match — the lexical
        # similarity gate downstream still filters spurious matches.
        params = {
            "vocab": vocab,
            "query": _finto_search_query(request.literal),
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

        candidates: list[AuthorityCandidate] = []
        for item in payload.get("results", []):
            uri = item.get("uri")
            pref = item.get("prefLabel") or item.get("matchedPrefLabel") or ""
            if not uri:
                continue
            candidates.append(
                _AuthorityCandidate(
                    uri=str(uri),
                    pref_label=str(pref),
                    source_vocabulary=vocab,
                    lexical_similarity=_lexical_similarity(request.literal, str(pref)),
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
        from bffi_pipeline.stages.m9.runner import AuthorityCandidate as _AuthorityCandidate

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
        candidates: list[AuthorityCandidate] = []
        for item in payload.get("result", []) or []:
            viaf_id = item.get("viafid") or item.get("id")
            term = item.get("term") or item.get("displayForm") or ""
            if not viaf_id:
                continue
            uri = f"https://viaf.org/viaf/{viaf_id}"
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
