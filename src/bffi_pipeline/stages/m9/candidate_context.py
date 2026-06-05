"""M9 picker candidate-context enrichment.

After the authority client returns its candidate list, fetch per-URI
context from the local Fuseki authority graphs (loaded by
``bffi-pipeline load-finto``): ``skos:definition``, ``skos:scopeNote``,
``skos:altLabel``, broader prefLabels, and biographical fragments for
persons (birth / death dates, occupations).

Why Fuseki rather than Finto's ``/data`` REST endpoint:

- The KANTO / YSO / KAUNO / MUSO graphs are already loaded into the
  local Fuseki by the existing ``load-finto`` machinery, so no new
  external HTTP traffic and no api.finto.fi rate-limit pressure.
- One SPARQL ``VALUES`` query returns context for all N candidates of
  a picker fire in a single round-trip.

Used by the M9 picker (Phase B): the candidate-side disambiguating
signal that pairs with the Phase A Work-side context. When two
KANTO entries share a prefLabel (the "Koivisto, Ilkka" case — one
psychologist, one zoologist), the per-URI ``skos:scopeNote`` or
broader prefLabels are what tell the LLM which is which.

Missing data degrades cleanly: when Fuseki returns no rows for a URI
(e.g. KANTO snapshot doesn't carry the freshly-minted entry yet), the
candidate ends up with ``context=None`` and the picker prompt falls
back to its prefLabel-only render for that one row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import httpx

from bffi_pipeline.stages.m9.local_concept_resolver import (
    VOCAB_KAUNOKKI,
    VOCAB_SLM,
)
from bffi_pipeline.stages.m9.schemas import (
    VOCAB_KANTO,
    VOCAB_KAUNO,
    VOCAB_MUSO,
    VOCAB_YSO,
    AuthorityKind,
)

#: Source vocabulary → named-graph URI for the candidate-context
#: SPARQL. KANTO, YSO, KAUNO, MUSO, SLM are loaded by ``load-finto``
#: into the named graphs whose URIs are the vocab namespace prefix.
#: Mirrors :data:`bffi_pipeline.stages.m10.load_finto.FINTO_VOCABS`'s
#: ``graph_uri`` values for the same vocabs.
_VOCAB_TO_GRAPH: Final[dict[str, str]] = {
    VOCAB_KANTO: "http://urn.fi/URN:NBN:fi:au:finaf:",
    VOCAB_YSO: "http://www.yso.fi/onto/yso/",
    VOCAB_KAUNO: "http://www.yso.fi/onto/kauno/",
    VOCAB_KAUNOKKI: "http://urn.fi/URN:NBN:fi:au:kaunokki:",
    VOCAB_MUSO: "http://www.yso.fi/onto/muso/",
    VOCAB_SLM: "http://urn.fi/URN:NBN:fi:au:slm:",
}

#: Cap on per-URI alt-label / broader-label fanout in the SPARQL
#: result so a long-tail KANTO entry with two dozen variant labels
#: doesn't dilute the picker prompt. The first few hits carry the
#: disambiguating signal; more is noise.
_CONTEXT_FIELD_CAP: Final[int] = 8


@dataclass(frozen=True)
class CandidateContext:
    """One authority entry's disambiguating context.

    All fields are optional. The picker prompt's formatter omits
    empty / missing ones so a KANTO entry that carries only a
    birth date doesn't render placeholder rows for definition,
    scope_note, etc.

    ``field_of_activity_labels`` is the most reliable per-person
    discriminator KANTO carries — ``rdaa:P50100`` per authority entry.
    For "Hakala, Tommi the surgeon" it's ``["yleiskirurgia"]``; for
    "Hakala, Tommi the baritone" it's ``["taidemusiikki",
    "viihdemusiikki"]``. ``occupation_labels`` (RDA P50104) is a
    parallel signal that's more often empty because it points to MTS
    URIs and MTS isn't loaded into Fuseki today.

    Tuples + ``frozen=True`` so instances are hashable for cache-key
    composition (mirrors :class:`WorkContext`).
    """

    definition: str | None = None
    scope_note: str | None = None
    alt_labels: tuple[str, ...] = ()
    broader_labels: tuple[str, ...] = ()
    birth_date: str | None = None
    death_date: str | None = None
    field_of_activity_labels: tuple[str, ...] = ()
    occupation_labels: tuple[str, ...] = ()


class CandidateContextFetcher(Protocol):
    """Protocol implementations satisfy to fetch per-URI context."""

    def fetch(self, *, uris: list[str], kind: AuthorityKind) -> dict[str, CandidateContext]:
        """Return ``{uri: context}`` for each ``uri`` in ``uris``.

        URIs without context in the underlying graph are simply absent
        from the result — the caller is expected to fall back to
        ``context=None`` for those (graceful degrade)."""
        ...


def _quote_sparql_literal(value: str) -> str:
    """JSON-style string escaping is valid SPARQL string syntax."""
    return json.dumps(value, ensure_ascii=False)


def _build_context_query(uris: list[str], graph_uri: str) -> str:
    """Build the SPARQL SELECT that fetches every context field at once.

    One round-trip returns rows for all input URIs; rows are then
    aggregated per-URI in Python (a URI with three altLabels surfaces
    as three rows differing only in ``?altLabel``).

    Two SPARQL nuances that diverge from a "naive single-graph"
    shape because of how KANTO stores person context:

    * ``rdaa:P50100`` (field of activity) and ``rdaa:P50104``
      (occupation) can be either an inline language-tagged literal
      (``"yleiskirurgia"@fi`` — Hakala, Tommi the surgeon) or a URI
      reference to a concept in another vocabulary (``yso:p18434`` →
      "taidemusiikki" — Hakala, Tommi the baritone). The
      ``?fieldOfActivity`` / ``?occupation`` bindings capture either
      shape; the per-row aggregator decides at parse time.
    * When ``?fieldOfActivity`` or ``?occupation`` is a URI, its
      ``skos:prefLabel`` lives in *another named graph* (YSO / MUSO /
      MTS), not in finaf. The label lookup must therefore happen
      *outside* the ``GRAPH <finaf:>`` block — moving it inside would
      silently miss every URI-shaped occupation, which is the
      majority case for non-medical professions.

    Language preferences: prefer Finnish labels for the resolved
    fields; Skosmos has the corresponding Swedish/English labels too
    but a Finnish-cataloguer prompt should see Finnish first.
    """
    values_clause = " ".join(f"<{uri}>" for uri in uris)
    return (
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        "PREFIX rdaa: <http://rdaregistry.info/Elements/a/>\n"
        "PREFIX schema: <http://schema.org/>\n"
        "SELECT ?uri ?definition ?scopeNote ?altLabel "
        "?broaderLabel ?birthDate ?deathDate "
        "?fieldOfActivity ?fieldOfActivityLabel "
        "?occupation ?occupationLabel\n"
        "WHERE {\n"
        f"  VALUES ?uri {{ {values_clause} }}\n"
        f"  GRAPH <{graph_uri}> {{\n"
        # Anchor triple: every authority entry in finaf / yso / etc.
        # carries ``skos:inScheme``. Without an anchor that ALWAYS
        # binds, a GRAPH block containing only OPTIONALs returns ZERO
        # solutions for URIs where none of the OPTIONALs match — and
        # because GRAPH joins with the outer VALUES, those URIs get
        # silently dropped from the result set. The ``_anchor``
        # variable isn't projected; it's just there to give the GRAPH
        # pattern a triple to anchor on.
        "    ?uri skos:inScheme ?_anchor .\n"
        "    OPTIONAL { ?uri skos:definition ?definition . }\n"
        "    OPTIONAL { ?uri skos:scopeNote ?scopeNote . }\n"
        "    OPTIONAL { ?uri skos:altLabel ?altLabel . }\n"
        "    OPTIONAL {\n"
        "      ?uri skos:broader ?broader .\n"
        "      ?broader skos:prefLabel ?broaderLabel .\n"
        "    }\n"
        "    OPTIONAL { ?uri rdaa:P50121 ?birthDate . }\n"
        "    OPTIONAL { ?uri rdaa:P50120 ?deathDate . }\n"
        "    OPTIONAL { ?uri schema:birthDate ?birthDate . }\n"
        "    OPTIONAL { ?uri schema:deathDate ?deathDate . }\n"
        "    OPTIONAL { ?uri rdaa:P50100 ?fieldOfActivity . }\n"
        "    OPTIONAL { ?uri rdaa:P50104 ?occupation . }\n"
        "  }\n"
        "  # Cross-graph resolution: when ?fieldOfActivity / ?occupation\n"
        "  # is a URI, its prefLabel lives in YSO / MUSO / MTS, not in\n"
        "  # finaf. These OPTIONAL blocks must sit outside GRAPH <finaf:>\n"
        "  # so the matcher can hit any named graph.\n"
        "  #\n"
        "  # The BOUND+isIRI guards are CRITICAL — without them, an\n"
        "  # unbound ?fieldOfActivity (i.e. the candidate URI has no\n"
        "  # rdaa:P50100) makes the OPTIONAL match every Finnish\n"
        "  # prefLabel in every loaded graph (~290k rows in production),\n"
        "  # multiplied by the number of input URIs. That blows up to\n"
        "  # millions of phantom rows and OOMs the picker.\n"
        "  OPTIONAL {\n"
        "    FILTER (BOUND(?fieldOfActivity) && isIRI(?fieldOfActivity))\n"
        "    ?fieldOfActivity skos:prefLabel ?fieldOfActivityLabel .\n"
        '    FILTER (LANG(?fieldOfActivityLabel) = "fi")\n'
        "  }\n"
        "  OPTIONAL {\n"
        "    FILTER (BOUND(?occupation) && isIRI(?occupation))\n"
        "    ?occupation skos:prefLabel ?occupationLabel .\n"
        '    FILTER (LANG(?occupationLabel) = "fi")\n'
        "  }\n"
        "}\n"
    )


def _row_value(row: dict[str, Any], key: str) -> str | None:
    cell = row.get(key)
    if cell is None:
        return None
    val = cell.get("value")
    return str(val) if val else None


def _row_is_uri(row: dict[str, Any], key: str) -> bool:
    """Return True iff the binding for ``key`` is a SPARQL URI node.

    KANTO encodes ``rdaa:P50100`` (field of activity) and ``rdaa:P50104``
    (occupation) sometimes as inline language-tagged literals, sometimes
    as URIs that point into YSO/MUSO/MTS. The aggregator branches on
    this so a literal yields a direct label, while a URI is paired with
    the cross-graph ``prefLabel`` lookup carried in a sibling binding.
    """
    cell = row.get(key)
    if cell is None:
        return False
    return bool(cell.get("type") == "uri")


def _resolved_label_for(row: dict[str, Any], value_key: str, label_key: str) -> str | None:
    """Return the human-readable label for a literal-or-URI binding.

    - If the binding at ``value_key`` is an inline literal, return that
      literal directly.
    - If it's a URI, return the cross-graph ``prefLabel`` carried at
      ``label_key`` (``None`` when no Finnish prefLabel was found in
      any loaded graph — caller treats the field as absent).
    """
    if _row_is_uri(row, value_key):
        return _row_value(row, label_key)
    return _row_value(row, value_key)


def _add_capped(bucket: list[str], value: str | None) -> None:
    """Append ``value`` to ``bucket`` if it's set, non-duplicate, and
    the bucket isn't already at :data:`_CONTEXT_FIELD_CAP`."""
    if value and value not in bucket and len(bucket) < _CONTEXT_FIELD_CAP:
        bucket.append(value)


def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, CandidateContext]:
    """Aggregate the SPARQL row-per-cartesian-product output back into one
    :class:`CandidateContext` per URI.

    For ``field_of_activity`` and ``occupation``, the SPARQL emits
    either a literal binding (KANTO inline) or a (URI + cross-graph
    label) pair. :func:`_resolved_label_for` normalises the two
    shapes into a single label string per row.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        uri = _row_value(row, "uri")
        if uri is None:
            continue
        bucket = out.setdefault(
            uri,
            {
                "definition": None,
                "scope_note": None,
                "alt_labels": [],
                "broader_labels": [],
                "birth_date": None,
                "death_date": None,
                "field_of_activity_labels": [],
                "occupation_labels": [],
            },
        )
        if bucket["definition"] is None:
            bucket["definition"] = _row_value(row, "definition")
        if bucket["scope_note"] is None:
            bucket["scope_note"] = _row_value(row, "scopeNote")
        if bucket["birth_date"] is None:
            bucket["birth_date"] = _row_value(row, "birthDate")
        if bucket["death_date"] is None:
            bucket["death_date"] = _row_value(row, "deathDate")
        _add_capped(bucket["alt_labels"], _row_value(row, "altLabel"))
        _add_capped(bucket["broader_labels"], _row_value(row, "broaderLabel"))
        _add_capped(
            bucket["field_of_activity_labels"],
            _resolved_label_for(row, "fieldOfActivity", "fieldOfActivityLabel"),
        )
        _add_capped(
            bucket["occupation_labels"],
            _resolved_label_for(row, "occupation", "occupationLabel"),
        )

    return {
        uri: CandidateContext(
            definition=fields["definition"],
            scope_note=fields["scope_note"],
            alt_labels=tuple(fields["alt_labels"]),
            broader_labels=tuple(fields["broader_labels"]),
            birth_date=fields["birth_date"],
            death_date=fields["death_date"],
            field_of_activity_labels=tuple(fields["field_of_activity_labels"]),
            occupation_labels=tuple(fields["occupation_labels"]),
        )
        for uri, fields in out.items()
    }


def _graph_uri_for_genre_form(first_uri: str) -> str:
    """Pick the right genre/form graph by URI-prefix routing."""
    if first_uri.startswith(_VOCAB_TO_GRAPH[VOCAB_SLM]):
        return _VOCAB_TO_GRAPH[VOCAB_SLM]
    if first_uri.startswith(_VOCAB_TO_GRAPH[VOCAB_KAUNOKKI]):
        return _VOCAB_TO_GRAPH[VOCAB_KAUNOKKI]
    return _VOCAB_TO_GRAPH[VOCAB_KAUNO]


@dataclass
class FusekiCandidateContextFetcher:
    """Production fetcher: one SPARQL round-trip per (kind, uris) batch.

    Caches per-URI within the process so the same candidate URI
    surfacing on multiple records doesn't re-query Fuseki. The
    persistent on-disk cache lives in :mod:`picker_cache` (alongside
    the picker-decision cache) so cross-run replays don't re-fetch.
    """

    http_client: httpx.Client
    fuseki_url: str
    timeout_seconds: float = 5.0
    _cache: dict[str, CandidateContext] = field(default_factory=dict)

    def fetch(self, *, uris: list[str], kind: AuthorityKind) -> dict[str, CandidateContext]:
        """Return per-URI context, batched in one SPARQL query."""
        if not uris:
            return {}
        # Carve out cached vs missing.
        result: dict[str, CandidateContext] = {}
        missing: list[str] = []
        for uri in uris:
            cached = self._cache.get(uri)
            if cached is not None:
                result[uri] = cached
            else:
                missing.append(uri)
        if not missing:
            return result

        graph_uri = self._graph_uri_for_kind(kind, missing[0])
        if graph_uri is None:
            return result

        bindings = self._post_sparql(_build_context_query(missing, graph_uri))
        if bindings is None:
            return result
        aggregated = _aggregate_rows(bindings)
        for uri in missing:
            ctx = aggregated.get(uri)
            if ctx is not None:
                self._cache[uri] = ctx
                result[uri] = ctx
        return result

    def _graph_uri_for_kind(self, kind: AuthorityKind, first_uri: str) -> str | None:
        """Decide which named graph holds the kind's authority.

        Person / corporate-body → KANTO. Subject → YSO. Genre/form →
        KAUNO unless the URI is in the SLM / kaunokki prefix (then
        route to that graph). The picker fires after vocab selection,
        so the candidate URIs are homogeneous for a given call;
        ``first_uri`` is sampled to pick the right graph.
        """
        if kind in ("person", "corporate_body"):
            return _VOCAB_TO_GRAPH[VOCAB_KANTO]
        if kind == "subject":
            return _VOCAB_TO_GRAPH[VOCAB_YSO]
        if kind == "music_form":
            return _VOCAB_TO_GRAPH[VOCAB_MUSO]
        if kind == "genre_form":
            return _graph_uri_for_genre_form(first_uri)
        return None

    def _post_sparql(self, query: str) -> list[dict[str, Any]] | None:
        try:
            response = self.http_client.post(
                f"{self.fuseki_url.rstrip('/')}/sparql",
                data={"query": query},
                headers={"Accept": "application/sparql-results+json"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError, ValueError:
            return None
        bindings = payload.get("results", {}).get("bindings", [])
        if not isinstance(bindings, list):
            return None
        return bindings


@dataclass
class StubCandidateContextFetcher:
    """Test stub: returns a wired :class:`CandidateContext` per URI."""

    fixtures: dict[str, CandidateContext] = field(default_factory=dict)

    def fetch(self, *, uris: list[str], kind: AuthorityKind) -> dict[str, CandidateContext]:
        """Return wired contexts for the requested URIs (missing → absent)."""
        del kind  # The stub doesn't gate on kind — fixtures already kind-scoped.
        return {uri: self.fixtures[uri] for uri in uris if uri in self.fixtures}


__all__ = [
    "CandidateContext",
    "CandidateContextFetcher",
    "FusekiCandidateContextFetcher",
    "StubCandidateContextFetcher",
]
