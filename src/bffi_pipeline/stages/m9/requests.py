"""M9 request building — graph walkers + per-target kind classifiers.

Two walkers:

- :func:`_iter_creator_requests` — yields one
  ``EntityRequest(kind="person")`` per primary contribution on each
  canonical Work. KANTO at tier-1 handles the bind.
- :func:`_iter_subject_requests` — yields one EntityRequest per
  unresolved ``bffi:subject`` / ``bffi:genreForm`` target, classified
  by URI fragment (Agent6XX → person / corporate_body), literal
  qualifier (fictional-character), then ``bf:source`` token routing.

Classifiers (``_classify_subject_source`` / ``_classify_subject_target``
/ ``_is_fictional_character_literal``) are exposed for tests that
build synthetic requests directly.

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from rdflib import Graph, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m9.schemas import AuthorityKind, EntityRequest

#: Maps the ``bf:source`` value that marc2bibframe2 emits on unresolved
#: 6XX targets to the reconciliation kind that selects the right Finto
#: vocabulary. ``bf:source`` is sometimes a literal (e.g. ``"yso/fin"``
#: from MARC ``$2 yso/fin``) and sometimes a URIRef (e.g.
#: ``<http://id.loc.gov/vocabulary/subjectSchemes/ysa>`` from MARC
#: ``$2 ysa``); we match against either by searching for the prefix
#: token anywhere in the string. Anything not matched defaults to
#: ``"subject"`` (YSO), the broadest of the three subject vocabularies
#: and the safest backstop when ``$2`` is missing or unrecognised.
#: ``ysa`` maps to ``"subject"`` so YSA-tagged terms route through
#: the YSO reconciliation path — YSO inherited the YSA concepts as
#: ``skos:prefLabel@fi`` during the 2014-2018 vocabulary merge.
#: ``allars`` and ``bella`` are the Swedish-language parallels to
#: YSO and KAUNO respectively; ``$2 allars`` routes to ``"subject"``
#: and ``$2 bella`` routes to ``"genre_form"`` (same kind as the
#: existing ``$2 kaunokki`` which substring-matches ``"kauno"``).
_SOURCE_TOKEN_TO_KIND: Final[tuple[tuple[str, AuthorityKind], ...]] = (
    ("yso", "subject"),
    ("ysa", "subject"),
    ("allars", "subject"),
    ("kauno", "genre_form"),
    ("bella", "genre_form"),
    ("muso", "music_form"),
    ("slm", "genre_form"),
)


def _classify_subject_source(source: str | None) -> AuthorityKind:
    """Map a ``bf:source`` value (literal text or URI string) to a
    reconciliation kind by token-substring match against the known
    vocabulary identifiers."""
    if source is None:
        return "subject"
    lowered = source.casefold()
    for token, kind in _SOURCE_TOKEN_TO_KIND:
        if token in lowered:
            return kind
    return "subject"


#: Subject-as-name URI patterns minted by marc2bibframe2 for MARC 6XX
#: subject fields that name a person / corporate body / meeting. Frag
#: ID convention: ``#Agent<MARC tag><sequence>-<index>``. MARC 600 →
#: Personal Name; MARC 610 → Corporate Body; MARC 611 → Meeting Name.
#: Detected from the URI fragment because canonical.ttl carries only
#: ``rdfs:label`` on these targets — the upstream ``bf:Person`` /
#: ``bf:Agent`` types and the ``bflc:marcKey`` ``"6XX..."`` pattern
#: don't survive the M3→M8 propagation.
_SUBJECT_AS_NAME_FRAGMENT_RE: Final[re.Pattern[str]] = re.compile(r"#Agent6(00|10|11)-\d+$")
_AGENT_FRAGMENT_TO_KIND: Final[dict[str, AuthorityKind]] = {
    "00": "person",  # MARC 600
    "10": "corporate_body",  # MARC 610
    "11": "corporate_body",  # MARC 611 (meetings) → KANTO conferences
}

#: Parenthetical qualifiers cataloguers attach to MARC 6XX person
#: labels to mark them as fictional characters. ``(fiktiivinen
#: hahmo)`` is the Finnish form, ``(fiktiv gestalt)`` the Swedish
#: parallel — both surfaced on the 200-record corpus smoke. Matched
#: case-insensitively because cataloguing-side capitalisation isn't
#: uniform; literal-trailing because the qualifier always comes after
#: the name (``"Nicholson, Dorothy (fiktiivinen hahmo)"``).
_FICTIONAL_CHARACTER_QUALIFIERS: Final[tuple[str, ...]] = (
    "(fiktiivinen hahmo)",
    "(fiktiv gestalt)",
)


def _is_fictional_character_literal(literal: str) -> bool:
    """Return True iff the cataloguer-supplied label ends with a
    fictional-character qualifier (Finnish or Swedish form)."""
    stripped = literal.rstrip().casefold()
    return any(stripped.endswith(q) for q in _FICTIONAL_CHARACTER_QUALIFIERS)


def _classify_subject_target(
    target: URIRef | None, source: str | None, literal: str | None = None
) -> AuthorityKind:
    """Decide kind for a subject-target node.

    Order:

    1. Fictional-character qualifier in the literal (``"X (fiktiivinen
       hahmo)"``) → ``fictional_character``. Highest priority — no
       authority carries fictional persons; routing to KANTO would
       just spend a Finto call to learn nothing.
    2. ``Agent6XX`` URI-fragment pattern from marc2bibframe2 → ``person``
       / ``corporate_body`` so tier-1 hits KANTO instead of YSO.
    3. Fall back to :func:`_classify_subject_source` (``bf:source``
       token routing).
    """
    if literal is not None and _is_fictional_character_literal(literal):
        return "fictional_character"
    if target is not None:
        match = _SUBJECT_AS_NAME_FRAGMENT_RE.search(str(target))
        if match is not None:
            return _AGENT_FRAGMENT_TO_KIND[match.group(1)]
    return _classify_subject_source(source)


def _iter_creator_requests(graph: Graph) -> Iterator[EntityRequest]:
    """Yield one creator-reconciliation request per canonical Work agent."""
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        for contrib in graph.objects(work, V.BFFI.contribution):
            if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
                continue
            for agent in graph.objects(contrib, V.BFFI.agent):
                if not isinstance(agent, URIRef):
                    continue
                for label in graph.objects(agent, V.RDFS.label):
                    if isinstance(label, RdfLiteral):
                        yield EntityRequest(
                            work_uri=str(work),
                            literal=str(label),
                            kind="person",
                        )
                        break


def _iter_subject_requests(graph: Graph) -> Iterator[EntityRequest]:
    """Yield reconciliation requests for unresolved ``bffi:subject`` /
    ``bffi:genreForm`` targets on canonical Works.

    Reconciles three target shapes (see :class:`SubjectTarget` in
    :mod:`bffi_pipeline.stages.m8`):

    - **Blank-node target** with ``rdfs:label`` + optional ``bf:source``:
      classic unresolved cataloguer-supplied subject.
    - **Local marc2bibframe2-minted URI** (e.g.
      ``http://urn.fi/.../#Place651-37``) carrying ``rdfs:label`` +
      ``bf:source`` — the dominant pattern for MARC ``$2 ysa`` time
      and place fields where the cataloguer didn't supply ``$0``.
    - **Pre-resolved authority URI** (e.g. ``yso/p1018``) carries no
      label / source on the canonical (Skosmos resolves from the
      loaded authority graph). These are skipped — already bound.

    Routing by ``bf:source`` (URI form like
    ``<.../subjectSchemes/ysa>`` or literal ``"yso/fin"``):

    - any ``yso*`` / ``ysa`` token → ``subject`` (YSO, with YSA-via-YSO inheritance)
    - any ``kauno*`` token → ``genre_form`` (KAUNO)
    - any ``muso*`` token → ``music_form`` (MUSO)
    - any ``slm`` token → ``genre_form`` (SLM)
    - missing / unknown → ``subject`` (YSO default)

    Deduplication is *not* applied here — the apply step caches per
    ``(kind, literal)`` lookups, so two canonical Works asking for the
    same subject only hit Finto once.
    """
    from bffi_pipeline.stages.m10.load_finto import graph_uri_for_uri

    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        for predicate in (V.BFFI.subject, V.BFFI.genreForm):
            for target in graph.objects(work, predicate):
                # Skip URIs that already resolve to an authority graph
                # we have loaded locally (YSO/KANTO/KAUNO/MUSO/SLM via
                # option 3b). They're already bound; their label
                # propagation in M8 was just fallback context for
                # Skosmos rendering, not a request for reconciliation.
                if isinstance(target, URIRef) and graph_uri_for_uri(str(target)) is not None:
                    continue
                label_lit: RdfLiteral | None = None
                for lab in graph.objects(target, V.RDFS.label):
                    if isinstance(lab, RdfLiteral):
                        label_lit = lab
                        break
                if label_lit is None:
                    continue
                source: str | None = None
                for src in graph.objects(target, V.BF.source):
                    if isinstance(src, URIRef):
                        source = str(src)
                        break
                    if isinstance(src, RdfLiteral):
                        source = str(src)
                        break
                target_uri = target if isinstance(target, URIRef) else None
                literal_str = str(label_lit)
                yield EntityRequest(
                    work_uri=str(work),
                    literal=literal_str,
                    kind=_classify_subject_target(target_uri, source, literal_str),
                    predicate_uri=str(predicate),
                )
