"""M8 canonical-Work minting — turn a union-find group into a Turtle block.

Every helper in this module either:
1. Writes triples on the canonical Work / Expression / AdminMetadata
   (propagation passes), or
2. Selects metadata from the member raw Works to seed those triples
   (modifier + earliest-converted-at selectors).

The top-level orchestrator (:func:`apply.apply_merge`) drives the
per-group walk; this module's job is to keep each propagation
concern (subjects, expressions, primary contributions, mint-anchor
tag, AdminMetadata block) cohesive.

P-38 Phase D: extracted from m8/runner.py. No logic change.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal as LiteralType
from typing import TypeGuard

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.provenance.logger import model_agent_uri
from bffi_pipeline.stages.m8.schemas import (
    CanonicalEntry,
    CanonicalWorkInputs,
    ContributionTarget,
    HelmetMapEntry,
    JudgeDecisionRow,
    MintAnchorKind,
    SubjectTarget,
)

#: Prefix for M3-minted raw bib URIs. The manifestation propagation
#: pass follows these into the canonical graph so per-record subgraphs
#: (Hubs, etc.) reach the round-trip converter intact.
_RAW_BIB_URI_PREFIX: str = "http://urn.fi/URN:NBN:fi:bib:raw/"


def _admin_metadata_uri(canonical_uri: str) -> URIRef:
    digest = hashlib.sha1(canonical_uri.encode("utf-8")).hexdigest()
    return URIRef(f"{get_settings().graph_base}adminmeta/{digest}")


def _propagate_manifestations(g: Graph, raw_graph: Graph) -> int:
    """Copy ``bffi:Manifestation`` subgraphs from M3 per-record output
    into the canonical M8 graph verbatim.

    Manifestations are 1:1 with Helmet bib records and never merge
    (unlike Works, which the union-find collapses across editions).
    So the propagation is a straight passthrough: every triple whose
    subject is a Manifestation gets copied, plus every triple whose
    subject is a blank node reachable from a Manifestation (the
    ``bf:identifiedBy`` blank nodes carrying ``rdf:value`` /
    ``bf:source``). The ``bffi:expressionManifested`` link still
    resolves because :func:`_propagate_expressions` keeps every
    member Expression URI alive on the canonical graph; M8 only
    rewrites Expression → Work links, not the Expression URIs
    themselves.

    Returns the triple count copied (for observability).
    """
    visited: set[URIRef | BNode] = set()
    queue: list[URIRef | BNode] = [
        s
        for s in raw_graph.subjects(RDF.type, V.BFFI.Manifestation)
        if isinstance(s, URIRef | BNode)
    ]
    count = 0
    while queue:
        node = queue.pop()
        if node in visited:
            continue
        visited.add(node)
        for p, o in raw_graph.predicate_objects(node):
            g.add((node, p, o))
            count += 1
            # Follow blank nodes (the usual case: bf:title, bf:Note,
            # bf:Local identifiers, etc.) AND raw-bib URIs that hang off
            # the Manifestation — most notably ``#Hub730-N`` URIs reached
            # via ``bffi:relation → bffi:Relation → bffi:associatedResource``.
            # Raw-bib URIs are per-record (M3 mints them with the bib_id
            # in the prefix) so they only appear in one record's output;
            # following them is safe and copies the Hub's full bf:title
            # subgraph into the canonical graph for the round-trip
            # converter to read.
            if o not in visited and (
                isinstance(o, BNode)
                or (isinstance(o, URIRef) and str(o).startswith(_RAW_BIB_URI_PREFIX))
            ):
                queue.append(o)
    # Materialise the inverse ``bffi:manifestationOfExpression`` triple
    # on each Expression so Skosmos's Expression page surfaces a
    # clickable backlink to its Manifestation. BFFI 1.0.0 declares
    # ``owl:inverseOf`` between the two predicates but Skosmos does not
    # run an OWL reasoner — without the materialised triple the
    # Expression page has no path to its Manifestations because all
    # the link triples are stored on the Manifestation side.
    for manif in raw_graph.subjects(RDF.type, V.BFFI.Manifestation):
        for expr in raw_graph.objects(manif, V.BFFI.expressionManifested):
            g.add((expr, V.BFFI.manifestationOfExpression, manif))
            count += 1
    return count


#: Expression-side predicates M3 emits on each Expression that the
#: canonical Work's hand-rolled propagation in :func:`_propagate_expressions`
#: doesn't carry forward. Keeping the canonical Expression aligned with
#: what M3 produced (rather than rebuilding from a sparse seed) means
#: the round-trip MARC reconstruction has access to language, content
#: type, note text, title block, classification, etc. Without this
#: passthrough those triples die at the M8 boundary even though M3
#: emitted them per-record.
_EXPRESSION_PASSTHROUGH_PREDICATES: tuple[URIRef, ...] = (
    V.BFFI.language,
    V.BFFI.content,
    V.BFFI.note,
    V.BFFI.title,
    V.BFFI.summary,
    V.BFFI.classification,
    V.BFFI.marcKey,
    V.BFFI.uniformTitleHub,
    V.BFFI.variantTitle,
)


#: Prefix for LoC vocabulary URIs whose ``rdfs:label`` triples must
#: survive the M3 → canonical boundary so the round-trip converter
#: can render MARC 336 / 337 / 338 ``$a`` (content / media / carrier
#: type human-readable labels) — and other 040-side vocab labels.
_LOC_VOCAB_URI_PREFIX: str = "http://id.loc.gov/vocabulary/"


#: BIBFRAME subject classes that the round-trip converter routes
#: on (``rdf:type`` on the subject URI = MARC 6XX tag selector):
#: ``bf:Place`` → 651, ``bf:Temporal`` → 648, ``bf:Person`` → 600,
#: ``bf:Organization`` → 610, ``bf:Meeting`` → 611, ``bf:Topic`` → 650.
_SUBJECT_TYPING_PREDICATES: tuple[URIRef, ...] = (
    V.BF.Topic,
    V.BF.Place,
    V.BF.Temporal,
    V.BF.Person,
    V.BF.Organization,
    V.BF.Meeting,
)


def _propagate_subject_typing(g: Graph, raw_graph: Graph) -> int:
    """Copy ``rdf:type`` triples on bffi:subject target URIs from the
    raw graph into canonical.

    When a cataloguer types ``$0 http://www.yso.fi/onto/yso/p104990``
    on a source MARC 651, marc2bibframe2 emits the URI as
    ``<bf:Place rdf:about="…/p104990">``. The ``a bf:Place`` triple
    is the only signal that survives the loss of source MARC tag —
    the URI's own namespace (plain ``yso/``) doesn't reveal the
    geographic intent. M3 routes the typing triple per-record; this
    pass forwards it into canonical so the round-trip converter's
    ``_subject_marc_tag`` can read it and pick 648/651/600/610/611.

    Filtered to the six BIBFRAME subject classes the converter
    routes on — other rdf:type triples on subject URIs (e.g.
    ``madsrdf:Topic``) aren't relevant.

    Returns triples copied.
    """
    count = 0
    routable: set[URIRef] = set(_SUBJECT_TYPING_PREDICATES)
    subject_uris: set[URIRef] = set()
    for _s, _p, o in raw_graph.triples((None, V.BFFI.subject, None)):
        if isinstance(o, URIRef):
            subject_uris.add(o)
    for uri in subject_uris:
        for t in raw_graph.objects(uri, RDF.type):
            if isinstance(t, URIRef) and t in routable:
                g.add((uri, RDF.type, t))
                count += 1
    return count


def _propagate_loc_vocab_labels(g: Graph, raw_graph: Graph) -> int:
    """Copy ``rdfs:label`` triples on LoC vocabulary URIs from the M3
    per-record graph into the canonical graph.

    marc2bibframe2 attaches the Finnish source-MARC ``$a`` label
    directly to the LoC URI as ``rdfs:label`` (e.g.
    ``<…/contentTypes/txt> rdfs:label "teksti"``); without this
    propagation the label dies at the M8 boundary because the LoC
    URI is neither a blank node nor a raw-bib URI, so neither of the
    other propagation passes follows it. Copying only ``rdfs:label``
    on LoC vocab URIs keeps the data flow surgical — no risk of
    pulling in unrelated triples.

    Returns triples copied.
    """
    count = 0
    seen: set[tuple[URIRef, str]] = set()
    for s, _p, o in raw_graph.triples((None, V.RDFS.label, None)):
        if not (isinstance(s, URIRef) and str(s).startswith(_LOC_VOCAB_URI_PREFIX)):
            continue
        if not isinstance(o, Literal):
            continue
        key = (s, str(o))
        if key in seen:
            continue
        seen.add(key)
        g.add((s, V.RDFS.label, o))
        count += 1
    return count


def _propagate_raw_agent_identifiers(g: Graph, raw_graph: Graph) -> int:
    """Copy ``bf:identifiedBy`` subgraphs on raw-bib agent URIs
    (``#Agent100-N`` / ``#Agent700-N`` / ``#Agent710-N`` / ``#Agent711-N``)
    from the M3 per-record graph into the canonical graph.

    The canonical Expression's contribution rebuild in
    :func:`_propagate_expression_contributions` (and the Work's
    primary-contribution rebuild) reuses the source agent URI verbatim
    whenever the schema carries one, so any triples about that URI in
    the canonical graph end up attached to the rebuilt canonical
    agent. M3 emits the agent's ASTERI / FINAF identifier as
    ``bf:identifiedBy → bf:Identifier → rdf:value + bf:source →
    bf:Source → bf:code "FI-ASTERI-N"``; this copies the full chain
    (including the reachable blank-node Identifier + Source) so the
    round-trip converter can emit ``$0 (FI-ASTERI-N)NNNNNN`` on the
    100 / 700 / 710 row.

    Returns triples copied.
    """
    count = 0
    visited_bnodes: set[BNode] = set()
    for agent in raw_graph.subjects(V.BF.identifiedBy, None):
        if not (isinstance(agent, URIRef) and str(agent).startswith(_RAW_BIB_URI_PREFIX)):
            continue
        for ident in raw_graph.objects(agent, V.BF.identifiedBy):
            g.add((agent, V.BF.identifiedBy, ident))
            count += 1
            # Walk the Identifier blank-node subgraph (rdf:value,
            # bf:source → bf:Source → bf:code) verbatim. Re-uses the
            # same bnode-walk discipline as _propagate_manifestations.
            if isinstance(ident, BNode):
                queue: list[BNode] = [ident]
                while queue:
                    node = queue.pop()
                    if node in visited_bnodes:
                        continue
                    visited_bnodes.add(node)
                    for p2, o2 in raw_graph.predicate_objects(node):
                        g.add((node, p2, o2))
                        count += 1
                        if isinstance(o2, BNode) and o2 not in visited_bnodes:
                            queue.append(o2)
    return count


def _propagate_expression_passthrough(g: Graph, raw_graph: Graph) -> int:
    """Copy a curated list of Expression-side predicates from the M3
    per-record output into the canonical M8 graph, including reachable
    blank-node subgraphs (notes carry ``bf:Note`` typing + ``rdf:value``,
    titles carry ``bf:Title`` + ``bf:mainTitle``, etc.).

    Mirrors :func:`_propagate_manifestations`'s passthrough discipline
    — Expressions are preserved 1:1 across the M3 → M8 boundary by
    URI hash, so the data has a stable destination.

    Returns triples copied (observability)."""
    count = 0
    visited_bnodes: set[BNode] = set()
    expressions = [
        e for e in raw_graph.subjects(RDF.type, V.BFFI.Expression) if isinstance(e, URIRef)
    ]
    visited_uris: set[URIRef] = set()
    for expr in expressions:
        for predicate in _EXPRESSION_PASSTHROUGH_PREDICATES:
            for obj in raw_graph.objects(expr, predicate):
                g.add((expr, predicate, obj))
                count += 1
                if _is_propagatable_subject(obj):
                    count += _copy_subgraph(g, raw_graph, obj, visited_bnodes, visited_uris)
    return count


def _is_propagatable_subject(node: object) -> TypeGuard[BNode | URIRef]:
    """True for blank nodes and per-record raw-bib URIs that the
    Expression passthrough should follow. Raw-bib URIs (e.g.
    ``#Hub240-N``) carry per-record subgraphs that the other
    propagation passes don't reach."""
    if isinstance(node, BNode):
        return True
    return isinstance(node, URIRef) and str(node).startswith(_RAW_BIB_URI_PREFIX)


def _copy_subgraph(
    g: Graph,
    raw_graph: Graph,
    seed: BNode | URIRef,
    visited_bnodes: set[BNode],
    visited_uris: set[URIRef],
) -> int:
    """Copy ``seed``'s reachable subgraph (predicates + nested
    propagatable objects) from ``raw_graph`` into ``g``. Mutates
    the visited sets in place so callers can dedupe across multiple
    seeds. Returns triples copied."""
    count = 0
    queue: list[BNode | URIRef] = [seed]
    while queue:
        node = queue.pop()
        if isinstance(node, BNode):
            if node in visited_bnodes:
                continue
            visited_bnodes.add(node)
        elif node in visited_uris:
            continue
        else:
            visited_uris.add(node)
        for p2, o2 in raw_graph.predicate_objects(node):
            g.add((node, p2, o2))
            count += 1
            if (
                _is_propagatable_subject(o2)
                and o2 not in visited_bnodes
                and (not isinstance(o2, URIRef) or o2 not in visited_uris)
            ):
                queue.append(o2)
    return count


def _propagate_subject_targets(
    g: Graph,
    *,
    canonical_uri: URIRef,
    members: list[CanonicalWorkInputs],
    predicate: URIRef,
    attr: LiteralType["subject_targets", "genre_form_targets"],
) -> None:
    """Emit ``predicate`` triples on ``canonical_uri`` from each member's targets.

    Three target shapes (see :class:`SubjectTarget` docstring):

    1. **Pre-resolved authority URI**: emit ``<canonical> predicate <uri>``;
       no further triples on the URI (Skosmos resolves labels from the
       loaded authority graph). Dedup across members by URI.
    2. **Local marc2bibframe2-minted URI** (URI present, label/source
       carried locally — MARC ``$2 ysa`` time/place fields):
       propagate the URI AND re-emit its ``rdfs:label`` + ``bf:source``
       so M9 has the metadata to reconcile against. Dedup by URI.
    3. **Blank-node target**: mint a deterministic blank node, dedup
       by ``(label, source)`` so the same cataloguer subject string
       from N raw Works produces one blank node on the canonical;
       M9 phase 3 reconciles each once.
    """
    seen_uris: set[str] = set()
    seen_blank_keys: set[tuple[str | None, str | None]] = set()
    blank_targets: list[SubjectTarget] = []
    for member in members:
        for target in getattr(member, attr):
            if target.uri is not None:
                if target.uri in seen_uris:
                    continue
                seen_uris.add(target.uri)
                uri_node = URIRef(target.uri)
                g.add((canonical_uri, predicate, uri_node))
                # Case 2: the URI carries its own label / source. Copy
                # them onto the canonical so M9 can reconcile (and
                # Skosmos has fallback labels for the local URI even
                # when no authority binding lands).
                if target.label is not None:
                    g.add((uri_node, V.RDFS.label, Literal(target.label)))
                if target.source is not None:
                    if target.source.startswith(("http://", "https://")):
                        g.add((uri_node, V.BF.source, URIRef(target.source)))
                    else:
                        g.add((uri_node, V.BF.source, Literal(target.source)))
                continue
            key = (target.label, target.source)
            if key in seen_blank_keys:
                continue
            seen_blank_keys.add(key)
            blank_targets.append(target)
    # Order blank-node emission deterministically AND mint stable BNode
    # identifiers from a hash of (canonical, predicate, label, source) so
    # canonical.ttl is byte-stable across runs. rdflib's default
    # BNode() uses a process-local counter, which would otherwise leak
    # non-determinism into the serialised file.
    blank_targets.sort(key=lambda t: (t.label or "", t.source or ""))
    predicate_str = str(predicate)
    for target in blank_targets:
        digest = hashlib.sha1(
            "|".join(
                (
                    str(canonical_uri),
                    predicate_str,
                    target.label or "",
                    target.source or "",
                )
            ).encode("utf-8")
        ).hexdigest()
        node = BNode(f"sub{digest}")
        g.add((canonical_uri, predicate, node))
        if target.label is not None:
            g.add((node, V.RDFS.label, Literal(target.label)))
        if target.source is not None:
            g.add((node, V.BF.source, Literal(target.source)))


def _propagate_expressions(
    g: Graph,
    *,
    canonical_uri: URIRef,
    members: list[CanonicalWorkInputs],
) -> None:
    """Re-assert Expression typing + ``bffi:hasExpression`` /
    ``expressionOf`` + prefLabel + non-primary Contribution blocks.

    The typing and link triples make M10's Skosify dual-type
    Expressions as ``skos:Concept``. The prefLabel literal is what
    Skosmos surfaces in the Work → Expression hierarchy view; without
    it the UI renders Expressions with empty labels.

    Non-primary contributions (M3 cascade-emitted + 700-fielded
    translators / illustrators / performers) are dedup'd across
    members by ``(expr_uri, agent, role)`` and re-emitted on the
    canonical Expression with deterministic SHA-1 blank-node IDs so
    canonical.ttl stays byte-stable across re-runs.
    """
    seen_exprs: set[str] = set()
    seen_labels: set[tuple[str, str, str | None]] = set()
    seen_contribs: set[tuple[str, str, str, str, str]] = set()
    for member in members:
        for expr_uri in member.expression_uris:
            if expr_uri in seen_exprs:
                continue
            seen_exprs.add(expr_uri)
            expr = URIRef(expr_uri)
            g.add((expr, RDF.type, V.BFFI.Expression))
            g.add((canonical_uri, V.BFFI.hasExpression, expr))
            g.add((expr, V.BFFI.expressionOf, canonical_uri))
        for expr_uri, label_text, lang in member.expression_labels:
            key = (expr_uri, label_text, lang)
            if key in seen_labels:
                continue
            seen_labels.add(key)
            literal = Literal(label_text, lang=lang) if lang else Literal(label_text)
            g.add((URIRef(expr_uri), V.SKOS.prefLabel, literal))
        for ec in member.expression_contributions:
            key_t = (
                ec.expression_uri,
                ec.agent_uri or "",
                ec.agent_label or "",
                ec.role_uri or "",
                ec.role_label or "",
            )
            if key_t in seen_contribs:
                continue
            seen_contribs.add(key_t)
            digest = hashlib.sha1("|".join(key_t).encode("utf-8")).hexdigest()
            contrib_node = BNode(f"econ{digest}")
            expr = URIRef(ec.expression_uri)
            g.add((expr, V.BFFI.contribution, contrib_node))
            g.add((contrib_node, RDF.type, V.BFFI.Contribution))
            if ec.role_uri is not None:
                g.add((contrib_node, V.BF.role, URIRef(ec.role_uri)))
            elif ec.role_label is not None:
                # Free-text role from the cataloguer's $e ("johtaja" /
                # "cembalo" / etc.) — re-emit the marc2bibframe2 shape
                # `bf:role [a bf:Role; rdfs:label "..."]` so Skosmos
                # surfaces the cataloguer-supplied role text alongside
                # any controlled-vocabulary URIs other contributions
                # carry.
                role_node = BNode(f"erol{digest}")
                g.add((contrib_node, V.BF.role, role_node))
                g.add((role_node, RDF.type, V.BF.Role))
                g.add((role_node, V.RDFS.label, Literal(ec.role_label)))
            agent_node: URIRef | BNode
            if ec.agent_uri is not None:
                agent_node = URIRef(ec.agent_uri)
            else:
                agent_node = BNode(f"eag{digest}")
                g.add((agent_node, RDF.type, V.BFFI.Agent))
            g.add((contrib_node, V.BFFI.agent, agent_node))
            if ec.agent_label is not None:
                g.add((agent_node, V.RDFS.label, Literal(ec.agent_label)))


def _propagate_primary_contributions(
    g: Graph,
    *,
    canonical_uri: URIRef,
    members: list[CanonicalWorkInputs],
) -> None:
    """Emit one ``PrimaryContribution → agent → rdfs:label`` block per absorbed agent.

    Deduplicates by ``agent_uri`` across all absorbed members so a
    multi-Work merge group produces one contribution per distinct
    creator. The blank-node identifier is derived from a SHA-1 of
    ``(canonical_uri, agent_uri)`` so canonical.ttl stays byte-stable
    across runs (matching the determinism rule the subject-propagation
    block follows).

    The agent's ``rdfs:label`` is re-asserted on the canonical so the
    M9 walker doesn't need to reach back into per-record BFFI Turtles
    to resolve labels.
    """
    seen_agents: set[str] = set()
    flat: list[ContributionTarget] = []
    for member in members:
        for target in member.contribution_targets:
            if target.agent_uri in seen_agents:
                continue
            seen_agents.add(target.agent_uri)
            flat.append(target)
    flat.sort(key=lambda t: t.agent_uri)
    for target in flat:
        digest = hashlib.sha1(f"{canonical_uri}|{target.agent_uri}".encode()).hexdigest()
        contrib = BNode(f"con{digest}")
        agent = URIRef(target.agent_uri)
        g.add((canonical_uri, V.BFFI.contribution, contrib))
        g.add((contrib, RDF.type, V.BFFI.PrimaryContribution))
        g.add((contrib, V.BFFI.agent, agent))
        g.add((agent, V.RDFS.label, Literal(target.agent_label)))


def _emit_mint_anchor(g: Graph, canonical_uri: URIRef, mint_anchor: MintAnchorKind | None) -> None:
    """Emit ``<canonical_uri> bffi-prov:mintAnchor <kind-uri>`` for a P-34 anchor.

    Mapping (kept here so the kind-name → URI lookup lives next to
    the emission, not duplicated in :func:`_emit_canonical_work`):
    """
    mapping = {
        "primary": V.MINT_ANCHOR_PRIMARY_AUTHOR,
        "first-contributor": V.MINT_ANCHOR_FIRST_CONTRIBUTOR,
        "anonymous-work": V.MINT_ANCHOR_ANONYMOUS_WORK,
    }
    if mint_anchor is None:
        return
    g.add((canonical_uri, V.mintAnchor, mapping[mint_anchor]))


def _emit_canonical_work(
    g: Graph,
    *,
    canonical_uri: URIRef,
    pref_label: str | None,
    members: list[CanonicalWorkInputs],
    helmet_entries: dict[str, HelmetMapEntry],
    description_modifier_uri: URIRef,
    description_change_date: datetime,
    mint_anchor: MintAnchorKind | None = None,
) -> tuple[CanonicalEntry, str]:
    """Add the canonical Work + AdminMetadata to ``g``. Returns (map row, merged_at)."""
    g.add((canonical_uri, RDF.type, V.BFFI.Work))
    g.add((canonical_uri, RDF.type, V.SKOS.Concept))
    # P-34: surface which input slot resolved the canonical-Work mint
    # key. The anchor URI distinguishes editor-anchored canonical Works
    # (anonymous-main-entry MARC records, no 1XX) from the standard
    # primary-author-anchored case. Cataloguer review + dashboard
    # filters can split on this.
    _emit_mint_anchor(g, canonical_uri, mint_anchor)
    union_pref_labels: set[tuple[str, str | None]] = set()
    for member in members:
        union_pref_labels.update(member.pref_labels)
    if union_pref_labels:
        for text, lang in sorted(union_pref_labels, key=lambda t: (t[1] or "", t[0])):
            literal = Literal(text, lang=lang) if lang else Literal(text)
            g.add((canonical_uri, V.SKOS.prefLabel, literal))
    elif pref_label is not None:
        # Fallback for synthetic test fixtures that don't populate
        # ``pref_labels`` — production always does, via ``_all_pref_labels``.
        g.add((canonical_uri, V.SKOS.prefLabel, Literal(pref_label)))

    # P-45 commit 2: ``bf:identifiedBy`` + ``dct:identifier`` no longer
    # union onto the canonical Work — the bib_id moved to Manifestation
    # (each Manifestation is 1:1 with one Helmet bib record and never
    # merges). Tooling that walks bib_ids from a Work follows
    #   Work → bffi:hasExpression → Expression
    #        ← bffi:expressionManifested ← Manifestation
    #        → bf:identifiedBy / dct:identifier
    # We still collect the bib_id list for downstream consumers:
    # AdminMetadata (cataloguer-facing summary) + canonical-map.jsonl
    # (forensic audit log of which raws got absorbed).
    seen_bib_ids: set[str] = set()
    helmet_bib_ids_ordered: list[str] = []
    for member in members:
        for _ident_uri, bib_id in member.helmet_identifiers:
            if bib_id in seen_bib_ids:
                continue
            seen_bib_ids.add(bib_id)
            helmet_bib_ids_ordered.append(bib_id)

    _propagate_expressions(g, canonical_uri=canonical_uri, members=members)

    # Provenance back-links to absorbed raw Works.
    raw_uris_sorted = sorted(m.work_uri for m in members)
    for raw in raw_uris_sorted:
        g.add((canonical_uri, V.PROV.wasDerivedFrom, URIRef(raw)))

    # Propagate bffi:subject + bffi:genreForm onto the canonical Work.
    # Resolved (URI) targets dedupe across members; unresolved (blank-node)
    # targets dedupe by (label, source) so two raw Works carrying the same
    # cataloguer subject string emit one blank node on the canonical, ready
    # for M9 phase 3 to reconcile against Finto.
    _propagate_subject_targets(
        g,
        canonical_uri=canonical_uri,
        members=members,
        predicate=V.BFFI.subject,
        attr="subject_targets",
    )
    _propagate_subject_targets(
        g,
        canonical_uri=canonical_uri,
        members=members,
        predicate=V.BFFI.genreForm,
        attr="genre_form_targets",
    )

    # Propagate bffi:PrimaryContribution → agent → rdfs:label onto the
    # canonical Work so M9's `_iter_creator_requests` walker can find
    # creators to reconcile. Without this, M9 returns 0 entities.
    _propagate_primary_contributions(g, canonical_uri=canonical_uri, members=members)

    # AdminMetadata block — every predicate from spec § 8.
    admin_uri = _admin_metadata_uri(str(canonical_uri))
    earliest = _earliest_converted_at(members, helmet_entries)
    merged_at_iso = description_change_date.isoformat()

    g.add((canonical_uri, V.adminMetadata, admin_uri))
    g.add((admin_uri, RDF.type, V.AdminMetadata))
    g.add((admin_uri, V.adminMetadataFor, canonical_uri))
    if earliest is not None:
        g.add(
            (
                admin_uri,
                V.descriptionCreationDate,
                Literal(earliest, datatype=V.XSD.dateTime),
            )
        )
    g.add(
        (
            admin_uri,
            V.descriptionChangeDate,
            Literal(merged_at_iso, datatype=V.XSD.dateTime),
        )
    )
    g.add((admin_uri, V.dateGenerated, Literal(merged_at_iso, datatype=V.XSD.dateTime)))
    g.add((admin_uri, V.descriptionModifier, description_modifier_uri))
    g.add((admin_uri, V.descriptionConventions, V.DESC_CONV_BFFI_1_0_0))
    g.add((admin_uri, V.descriptionLevel, V.DESC_LEVEL_MINIMUM))
    g.add((admin_uri, V.encodingLevel, V.ENC_LEVEL_AUTO))
    g.add((admin_uri, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    g.add((admin_uri, V.generationProcess, V.GEN_PROCESS_PIPELINE_V0_1_0))
    g.add((admin_uri, V.metadataLicensor, V.METADATA_LICENSOR_CC0))
    g.add((admin_uri, V.recordingSource, V.RECORDING_SOURCE_HELMET))
    for bib_id in helmet_bib_ids_ordered:
        helmet_uri = URIRef(f"{get_settings().graph_base}helmet/{bib_id}")
        g.add((admin_uri, V.sourceMetadata, helmet_uri))

    return (
        CanonicalEntry(
            canonical_work_uri=str(canonical_uri),
            raw_work_uris=raw_uris_sorted,
            helmet_bib_ids=helmet_bib_ids_ordered,
            merged_at=merged_at_iso,
        ),
        merged_at_iso,
    )


def _earliest_converted_at(
    members: list[CanonicalWorkInputs],
    helmet_entries: dict[str, HelmetMapEntry],
) -> str | None:
    timestamps = [
        helmet_entries[m.work_uri].converted_at for m in members if m.work_uri in helmet_entries
    ]
    return min(timestamps) if timestamps else None


def _bind_prefixes(g: Graph) -> None:
    g.bind("bffi", V.BFFI)
    g.bind("bffi-prov", V.BFFI_PROV)
    g.bind("bf", V.BF)
    g.bind("bib", V.BIB)
    g.bind("dct", DCTERMS)
    g.bind("prov", V.PROV)
    g.bind("skos", V.SKOS)
    g.bind("xsd", V.XSD)


def _select_description_modifier(
    members: list[CanonicalWorkInputs],
    decisions_by_pair: dict[frozenset[str], JudgeDecisionRow],
) -> URIRef:
    """Pick the agent who modified this canonical Work.

    For singletons the modifier is the M2 marc2bibframe2 agent (matching
    the AdminMetadata stamp emitted at conversion time). For merge groups
    it's the agent of the first ``same_work`` decision contributing to
    the group, with a stable ordering on the member URIs.
    """
    if len(members) <= 1:
        return V.AGENT_MARC2BIBFRAME2

    sorted_uris = sorted(m.work_uri for m in members)
    for i, a in enumerate(sorted_uris):
        for b in sorted_uris[i + 1 :]:
            row = decisions_by_pair.get(frozenset({a, b}))
            if row is not None and row.decision == "same_work" and row.winning_model:
                return model_agent_uri(row.winning_model)
    return V.AGENT_MARC2BIBFRAME2
