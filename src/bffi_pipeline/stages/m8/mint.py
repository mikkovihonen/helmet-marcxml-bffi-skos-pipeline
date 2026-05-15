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


def _admin_metadata_uri(canonical_uri: str) -> URIRef:
    digest = hashlib.sha1(canonical_uri.encode("utf-8")).hexdigest()
    return URIRef(f"{get_settings().graph_base}adminmeta/{digest}")


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

    # bf:identifiedBy: union all Helmet identifiers across members, dedup by bib_id.
    seen_bib_ids: set[str] = set()
    helmet_bib_ids_ordered: list[str] = []
    for member in members:
        for ident_uri, bib_id in member.helmet_identifiers:
            if bib_id in seen_bib_ids:
                continue
            seen_bib_ids.add(bib_id)
            helmet_bib_ids_ordered.append(bib_id)
            ident = URIRef(ident_uri)
            g.add((canonical_uri, V.BF.identifiedBy, ident))
            g.add((ident, RDF.type, V.BF.Local))
            g.add((ident, RDF.value, Literal(bib_id)))
            g.add((ident, V.BF.source, V.HELMET_SOURCE_URI))
            g.add((canonical_uri, DCTERMS.identifier, Literal(bib_id)))

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
