"""M8 BFFI graph → per-Work canonical-mint inputs.

Walks the combined BFFI + BIBFRAME graph and returns one
:class:`CanonicalWorkInputs` per ``bffi:Work`` containing everything
the mint pass needs: creator anchor (per the P-34 three-tier
fallback), pref-label set, helmet identifiers, subject / genre-form
targets, primary + expression contributions, expression URIs +
labels.

P-38 Phase D: extracted from m8/runner.py. No logic change.
"""

from __future__ import annotations

import hashlib
from typing import Final

from rdflib import BNode, Graph, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF
from rdflib.term import Node

from bffi_pipeline.blocking import fold_label
from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m8.schemas import (
    CanonicalWorkInputs,
    ContributionTarget,
    ExpressionContribution,
    MintAnchorKind,
    SubjectTarget,
)

#: Translator-role markers that should NOT anchor a canonical Work
#: (P-34 R3 mitigation). A translator is intellectually wrong as
#: anchor — the original author is the right anchor, but they're
#: missing from these records. Until P-34 sub-option (2) ships a
#: cataloguer-curated rule table, translator-only records stay in
#: mint-failures.
_TRANSLATOR_ROLE_URIS: Final[frozenset[str]] = frozenset(
    {"http://id.loc.gov/vocabulary/relators/trl"}
)

#: Free-text role markers ($e subfield) that map to "translator".
#: Match is case-insensitive on the cataloguer-supplied label.
#: Covers fi / sv / en / de; extend as Helmet data surfaces other
#: forms.
_TRANSLATOR_ROLE_LABELS: Final[frozenset[str]] = frozenset(
    {"kääntäjä", "translator", "översättare", "übersetzer"}
)

#: URI namespace prefix for the P-34 Phase B anonymous-work
#: synthetic anchor. Two records that share normalized title +
#: content-type URI + language URI produce the same anchor and
#: therefore the same canonical Work URI. The cataloguer-facing
#: convention is documented in the P-34 plan's "Phase B" section:
#: see docs/plans/completed/p-34-m8-mint-anonymous-main-entry-works.md.
_ANONYMOUS_WORK_ANCHOR_PREFIX: Final[str] = "http://urn.fi/URN:NBN:fi:bib:anonymous-work-anchor/"


def _first_pref_label(graph: Graph, subject: URIRef) -> str | None:
    for o in graph.objects(subject, V.SKOS.prefLabel):
        if isinstance(o, RdfLiteral):
            return str(o)
    return None


def _all_pref_labels(graph: Graph, subject: URIRef) -> list[tuple[str, str | None]]:
    """Return the full set of ``(text, lang)`` prefLabels on ``subject``,
    sorted deterministically so re-runs of M8 produce byte-identical
    canonical.ttl. Untagged literals (``lang is None``) sort before any
    language-tagged literal to keep the key total-order safe."""
    return sorted(
        (
            (str(o), o.language)
            for o in graph.objects(subject, V.SKOS.prefLabel)
            if isinstance(o, RdfLiteral)
        ),
        key=lambda t: (t[1] or "", t[0]),
    )


def _primary_agent_uri(graph: Graph, work: URIRef) -> str | None:
    for contrib in graph.objects(work, V.BFFI.contribution):
        if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
            continue
        for ag in graph.objects(contrib, V.BFFI.agent):
            if isinstance(ag, URIRef):
                return str(ag)
    return None


def _is_translator_role(graph: Graph, contrib: URIRef | BNode) -> bool:
    """True if ``contrib`` carries a translator role (URI or label form)."""
    for role in graph.objects(contrib, V.BF.role):
        if isinstance(role, URIRef) and str(role) in _TRANSLATOR_ROLE_URIS:
            return True
        for label in graph.objects(role, V.RDFS.label):
            if str(label).strip().casefold() in _TRANSLATOR_ROLE_LABELS:
                return True
    return False


def _first_contribution_agent_uri(graph: Graph, work: URIRef) -> str | None:
    """P-34 sub-option (1) fallback for records with no ``bffi:PrimaryContribution``.

    Walks every contribution agent reachable from ``work``:
    - ``bffi:Work → bffi:contribution → bffi:agent`` (the rare case
      where a Work-level non-primary contribution exists).
    - ``bffi:Work → bffi:hasExpression → bffi:Expression →
      bffi:contribution → bffi:agent`` (the common Helmet case
      where 700-fielded contributors are routed to the Expression
      by ``sparql/bf_to_bffi_expression.rq``).

    Skips translator-only contributors (the intellectually-wrong
    anchor) and returns the lexicographically-smallest remaining
    agent URI for a deterministic mint key.

    Returns ``None`` only when the record has zero non-translator
    contributions — a truly-anonymous record. Those stay in
    ``canonical-mint-failures.jsonl`` until P-34 sub-option (2)
    ships a title-only fallback or a cataloguer-curated rule table.

    The lexicographic pick is arbitrary but deterministic;
    ``bffi-prov:mintAnchor = bib:auth/first-contributor-anchored``
    is emitted on the canonical so downstream code can distinguish
    editor-anchored Works from primary-author-anchored ones.
    """
    candidates: list[str] = []
    # Work-level contributions first (rare; covers Work-level non-
    # primary contributions and test fixtures that build them).
    candidates.extend(_collect_contribution_agent_uris(graph, work))
    # Expression-level contributions — the common Helmet case for
    # MARC 700 editors / contributors routed to ?exprURI by
    # bf_to_bffi_expression.rq.
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if isinstance(expr, URIRef):
            candidates.extend(_collect_contribution_agent_uris(graph, expr))
    if not candidates:
        return None
    return min(candidates)


def _anonymous_work_anchor_uri(graph: Graph, work: URIRef) -> str | None:
    """P-34 Phase B fallback when no creator (primary or non-primary) is found.

    Synthesises a deterministic anchor URI from three BFFI predicates
    already extracted by M3:

    - **Title** — ``skos:prefLabel`` on the Work (sourced from MARC 245$a
      + $b). Required; if absent the record stays in mint-failures via
      Phase A's ``missing_inputs=["pref_label"]`` path. Normalised via
      :func:`bffi_pipeline.blocking.fold_label` (NFKC + diacritic-fold
      + casefold + whitespace-collapse + strip-trailing-decoration) so
      catalographically-equivalent titles converge.
    - **Content type** — ``bffi:content`` URI on the linked
      ``bffi:Expression`` (LoC contentTypes vocab; sourced from MARC
      leader/06 + 336$a$b$2). E.g.
      ``<http://id.loc.gov/vocabulary/contentTypes/txt>``.
    - **Language** — ``bffi:language`` URI on the linked
      ``bffi:Expression`` (LoC languages vocab; sourced from MARC
      008/35-37 + 041$a). E.g.
      ``<http://id.loc.gov/vocabulary/languages/fin>``.

    Returns ``None`` when the title is missing (Phase A's existing
    mint-failure path catches that case). Content-type and language
    can both be absent — the anchor still produces a deterministic
    URI, just on a smaller key.
    """
    title = _first_pref_label(graph, work)
    if title is None:
        return None
    title_normalised = fold_label(title)
    content_uri = ""
    language_uri = ""
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        if not content_uri:
            for ct in graph.objects(expr, V.BFFI.content):
                if isinstance(ct, URIRef):
                    content_uri = str(ct)
                    break
        if not language_uri:
            for lang in graph.objects(expr, V.BFFI.language):
                if isinstance(lang, URIRef):
                    language_uri = str(lang)
                    break
        if content_uri and language_uri:
            break
    key = f"{title_normalised}|{content_uri}|{language_uri}"
    digest = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"{_ANONYMOUS_WORK_ANCHOR_PREFIX}{digest}"


def _collect_contribution_agent_uris(graph: Graph, subject: URIRef) -> list[str]:
    """Return non-translator ``bffi:contribution → bffi:agent`` URIs on ``subject``.

    Helper for :func:`_first_contribution_agent_uri`; pulled out so
    the Work-level + Expression-level walks share the same
    contribution-iteration + translator-blocklist logic without
    tripping the branch-count linter.
    """
    out: list[str] = []
    for contrib in graph.objects(subject, V.BFFI.contribution):
        if not isinstance(contrib, (URIRef, BNode)):
            continue
        if _is_translator_role(graph, contrib):
            continue
        for ag in graph.objects(contrib, V.BFFI.agent):
            if isinstance(ag, URIRef):
                out.append(str(ag))
                break
    return out


def _primary_contribution_targets(graph: Graph, work: URIRef) -> list[ContributionTarget]:
    """Collect ``PrimaryContribution → agent → rdfs:label`` triples on ``work``.

    Deduplicates by ``agent_uri`` because marc2bibframe2's MARC-100 lift
    can emit the same primary contribution N times for an aggregate
    record (one per included Work). One propagated contribution is
    enough for M9 reconciliation.

    Returns an empty list when the Work has no PrimaryContribution with
    a URI agent and rdfs:label — those records are surfaced as M8
    conflicts and don't reach M9 anyway.
    """
    out: list[ContributionTarget] = []
    seen: set[str] = set()
    for contrib in graph.objects(work, V.BFFI.contribution):
        if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
            continue
        for agent in graph.objects(contrib, V.BFFI.agent):
            if not isinstance(agent, URIRef):
                continue
            agent_uri = str(agent)
            if agent_uri in seen:
                continue
            label: str | None = None
            for lab in graph.objects(agent, V.RDFS.label):
                if isinstance(lab, RdfLiteral):
                    label = str(lab)
                    break
            if label is None:
                continue
            seen.add(agent_uri)
            out.append(ContributionTarget(agent_uri=agent_uri, agent_label=label))
    return out


def _helmet_identifiers(graph: Graph, work: URIRef) -> list[tuple[str, str]]:
    """Return [(ident_uri, helmet_bib_id), ...] for ``work``'s Helmet identifiers.

    M2 mints the BIBFRAME `bf:identifiedBy` object as a blank node;
    M3's SPARQL CONSTRUCT preserves it as a blank node into the BFFI
    graph. Blank nodes can't be re-emitted as URIs on the canonical
    Work because rdflib's bnode IDs aren't stable across processes.
    For blank-node identifiers, mint a deterministic URI from the
    bib_id (``<graph_base>ident/helmet/<bib_id>``) so M8 produces a
    byte-stable canonical.ttl across runs.
    """
    out: list[tuple[str, str]] = []
    for ident in graph.objects(work, V.BF.identifiedBy):
        sources = set(graph.objects(ident, V.BF.source))
        if V.HELMET_SOURCE_URI not in sources:
            continue
        for value in graph.objects(ident, RDF.value):
            if not isinstance(value, RdfLiteral):
                continue
            bib_id = str(value)
            ident_uri = (
                str(ident)
                if isinstance(ident, URIRef)
                else f"{get_settings().graph_base}ident/helmet/{bib_id}"
            )
            out.append((ident_uri, bib_id))
            break
    return out


def _read_subject_label_and_source(graph: Graph, target: Node) -> tuple[str | None, str | None]:
    """Pull ``rdfs:label`` and ``bf:source`` off a subject target.

    Both shapes coexist in real data. ``rdfs:label`` is always a literal.
    ``bf:source`` is a URIRef (`<…/subjectSchemes/ysa>`) on
    marc2bibframe2's lift of MARC ``$2 ysa`` time-period and place
    fields, but a literal (`"yso/fin"`) on the lift of MARC ``$2
    yso/fin`` topical fields. We accept either, returning the URI form
    as a string so the source-classification helper downstream can
    pattern-match against either shape uniformly.
    """
    label: str | None = None
    for lab in graph.objects(target, V.RDFS.label):
        if isinstance(lab, RdfLiteral):
            label = str(lab)
            break
    source: str | None = None
    for src in graph.objects(target, V.BF.source):
        if isinstance(src, URIRef):
            source = str(src)
            break
        if isinstance(src, RdfLiteral):
            source = str(src)
            break
    return label, source


def _subject_targets(graph: Graph, work: URIRef, predicate: URIRef) -> list[SubjectTarget]:
    """Walk ``work``'s ``bffi:subject`` / ``bffi:genreForm`` targets.

    Returns a :class:`SubjectTarget` per object. Three shapes coexist
    in production data:

    1. **Pre-resolved authority URI** (cataloguer supplied MARC ``$0``):
       a URIRef in a public namespace like ``yso/`` or ``slm:``, with
       no ``rdfs:label`` / ``bf:source`` of its own. Just propagate
       the URI; M10's Skosmos resolves the label from the loaded
       authority graph.
    2. **Local marc2bibframe2-minted URI** (e.g. ``Place651-37`` for a
       MARC 651 ``$2 ysa``): a URIRef in the per-record ``bib:raw/``
       namespace, carrying its own ``rdfs:label`` ("Venäjä") and
       ``bf:source`` (``<…/subjectSchemes/ysa>``) in the source
       BIBFRAME. Capture all three so M9 can reconcile.
    3. **Blank-node target** (the M3 SPARQL CONSTRUCT also
       occasionally emits these for fields without ``$0`` or local
       URI minting): label + source on the blank node, no URI.

    Targets we can't classify (no URI and no label) are skipped.
    """
    out: list[SubjectTarget] = []
    for obj in graph.objects(work, predicate):
        label, source = _read_subject_label_and_source(graph, obj)
        if isinstance(obj, URIRef):
            uri = str(obj)
            # Cases 1 + 2: URIRef target. If label / source are
            # present (case 2: local YSA-style), carry them through
            # so M9 can reconcile against a Finto authority. Without
            # them (case 1: cataloguer-resolved YSO URI), just the
            # URI.
            out.append(SubjectTarget(uri=uri, label=label, source=source))
            continue
        # Case 3: blank node. Need at least a label or source to be useful.
        if label is None and source is None:
            continue
        out.append(SubjectTarget(label=label, source=source))
    return out


def _expression_labels(graph: Graph, work: URIRef) -> list[tuple[str, str, str | None]]:
    """Collect ``skos:prefLabel`` literals on each ``bffi:hasExpression`` target.

    Returns ``[(expression_uri, label_text, lang), ...]``; multiple labels
    per Expression (different languages) produce multiple entries. M8
    re-asserts these on the canonical so Skosmos's UI shows labelled
    Expressions in the Work → Expression hierarchy view.
    """
    out: list[tuple[str, str, str | None]] = []
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        for label in graph.objects(expr, V.SKOS.prefLabel):
            if isinstance(label, RdfLiteral):
                out.append((str(expr), str(label), label.language))
    return out


def _read_role(graph: Graph, contrib: Node) -> tuple[str | None, str | None]:
    """Return ``(role_uri, role_label)`` from a contribution's ``bf:role``.

    Two shapes coexist: a controlled-vocabulary URI (M3 cascade emits
    these as ``<relators/cnd>`` etc.); a blank node typed ``bf:Role``
    with an ``rdfs:label`` (marc2bibframe2's lift of MARC $e free-text).
    URI form takes precedence; the first one wins.
    """
    for role in graph.objects(contrib, V.BF.role):
        if isinstance(role, URIRef):
            return str(role), None
        for lab in graph.objects(role, V.RDFS.label):
            if isinstance(lab, RdfLiteral):
                return None, str(lab)
    return None, None


def _read_agent(graph: Graph, contrib: Node) -> tuple[str | None, str | None]:
    """Return ``(agent_uri, agent_label)`` from a contribution's ``bffi:agent``."""
    for agent in graph.objects(contrib, V.BFFI.agent):
        agent_uri = str(agent) if isinstance(agent, URIRef) else None
        agent_label: str | None = None
        for lab in graph.objects(agent, V.RDFS.label):
            if isinstance(lab, RdfLiteral):
                agent_label = str(lab)
                break
        return agent_uri, agent_label
    return None, None


def _expression_contributions(graph: Graph, work: URIRef) -> list[ExpressionContribution]:
    """Collect non-primary ``bffi:Contribution`` blocks on each Expression
    linked to ``work`` via ``bffi:hasExpression``.

    For each: extracts the ``bf:role`` (URI or blank-node-with-label)
    and the agent (URI or blank-node-with-label). A contribution
    typed as ``bffi:PrimaryContribution`` is filtered out — those go
    on the canonical Work via :func:`mint._propagate_primary_contributions`.
    Contributions whose agent carries neither a URI nor a label are
    dropped (no information to propagate).
    """
    out: list[ExpressionContribution] = []
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        for contrib in graph.objects(expr, V.BFFI.contribution):
            if V.BFFI.PrimaryContribution in set(graph.objects(contrib, RDF.type)):
                continue
            role_uri, role_label = _read_role(graph, contrib)
            agent_uri, agent_label = _read_agent(graph, contrib)
            if agent_uri is None and agent_label is None:
                continue
            out.append(
                ExpressionContribution(
                    expression_uri=str(expr),
                    role_uri=role_uri,
                    role_label=role_label,
                    agent_uri=agent_uri,
                    agent_label=agent_label,
                )
            )
    return out


def extract_work_metadata(graph: Graph) -> dict[str, CanonicalWorkInputs]:
    """Walk the combined BFFI + BIBFRAME graph and return per-Work merge inputs."""
    out: dict[str, CanonicalWorkInputs] = {}
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        subjects = _subject_targets(graph, work, V.BFFI.subject)
        genres = _subject_targets(graph, work, V.BFFI.genreForm)
        contributions = _primary_contribution_targets(graph, work)
        # P-34: pick the canonical-mint creator anchor via three
        # fall-through paths. Each successful path writes a different
        # ``bffi-prov:mintAnchor`` predicate on the canonical so
        # cataloguer review + dashboard filters can split on the
        # anchor kind.
        #
        # 1. Standard: ``bffi:PrimaryContribution → bffi:agent``
        #    (records with MARC 1XX → primary author).
        # 2. Phase A: lex-min non-translator ``bffi:contribution``
        #    agent on Work or linked Expression (records with MARC
        #    700 editors but no 1XX — anonymous-main-entry).
        # 3. Phase B: synthetic ``anonymous-work-anchor:<sha1>`` URI
        #    keyed on (normalised title, content-type URI, language
        #    URI). Truly-anonymous records (no contributors at all,
        #    or only translator-role contributors).
        #
        # ``mint_anchor=None`` only when ``pref_label`` is also
        # missing — those stay in mint-failures.
        creator_uri = _primary_agent_uri(graph, work)
        mint_anchor: MintAnchorKind | None = "primary" if creator_uri else None
        if creator_uri is None:
            fallback = _first_contribution_agent_uri(graph, work)
            if fallback is not None:
                creator_uri = fallback
                mint_anchor = "first-contributor"
        if creator_uri is None:
            anon = _anonymous_work_anchor_uri(graph, work)
            if anon is not None:
                creator_uri = anon
                mint_anchor = "anonymous-work"
        out[str(work)] = CanonicalWorkInputs(
            work_uri=str(work),
            creator_uri=creator_uri,
            pref_label=_first_pref_label(graph, work),
            mint_anchor=mint_anchor,
            expression_uris=sorted(
                str(expr)
                for expr in graph.objects(work, V.BFFI.hasExpression)
                if isinstance(expr, URIRef)
            ),
            helmet_identifiers=_helmet_identifiers(graph, work),
            subject_targets=subjects,
            genre_form_targets=genres,
            contribution_targets=contributions,
            expression_labels=_expression_labels(graph, work),
            pref_labels=_all_pref_labels(graph, work),
            expression_contributions=_expression_contributions(graph, work),
        )
    return out
