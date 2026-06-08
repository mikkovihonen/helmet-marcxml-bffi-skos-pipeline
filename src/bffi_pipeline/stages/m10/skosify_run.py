"""Stage M10 (phase 1): Skosify overlay run.

Loads the M8 ``canonical.ttl`` together with
``config/overlay/bffi-skos-overlay.ttl`` through ``skosify.skosify``
with RDFS inference on. The output keeps the BFFI types
(``bffi:Work`` / ``bffi:Expression``) intact and adds the matching
``skos:Concept`` typing plus ``skos:narrower`` / ``skos:broader``
inverses on the Work ↔ Expression hierarchy. The result is what M10
phase 2 will load into Fuseki's ``bffi-works`` named graph.

Spec § 5 commits to the *overlay-plus-inference* approach: the
destructive Skosify ``[types]`` section is left empty, and the
overlay declares
``bffi:Work / bffi:Expression rdfs:subClassOf skos:Concept`` so
future BFFI subclasses absorb without a config change.

Output goes to ``<BFFI_DATA_DIR>/canonical-skosified.ttl`` via
tmp-then-rename. Re-runs are idempotent: when the output exists *and*
is newer than every input (canonical, overlay, config), the run
skips re-serialising. Pass ``force=True`` to override.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, SKOS
from rdflib.term import Node

from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.provenance import vocab as V

#: Default filenames under ``BFFI_DATA_DIR``.
SKOSIFIED_FILENAME: Final[str] = "canonical-skosified.ttl"

#: Project-relative paths to the canonical config files.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
DEFAULT_OVERLAY_PATH: Final[Path] = _REPO_ROOT / "config" / "overlay" / "bffi-skos-overlay.ttl"
DEFAULT_CONFIG_PATH: Final[Path] = _REPO_ROOT / "config" / "bffi.cfg"

#: Vendored MTS dump used to enrich ``bffi:role`` blank-node-with-
#: rdfs:label values with the MTS concept URI. BFFI 1.0.0 designates
#: MTS as the value vocabulary for ``bffi:Role`` (see
#: ``bffi-meta:relatedValueVocabulary`` on ``docs/lkd.rdf``'s
#: ``bffi:Role`` class, pointing at the four axis-collections below).
DEFAULT_MTS_DUMP_PATH: Final[Path] = _REPO_ROOT / "finto-dumps" / "mts-skos.ttl"

#: The four MTS collections BFFI 1.0.0 ties to ``bffi:Role`` via
#: ``bffi-meta:relatedValueVocabulary``. Each is FRBR-axis-scoped:
#: roles a Work can carry, roles an Expression can carry, etc.
#: The role-enrichment pass searches members of these collections
#: only — concepts elsewhere in MTS (e.g. RDA admin terms, content
#: types) are out of scope for ``bffi:Role``.
_MTS_ROLE_COLLECTION_BY_AXIS: Final[dict[str, str]] = {
    "Work": "http://urn.fi/URN:NBN:fi:au:mts:m34",
    "Expression": "http://urn.fi/URN:NBN:fi:au:mts:m153",
    "Manifestation": "http://urn.fi/URN:NBN:fi:au:mts:m491",
    "Item": "http://urn.fi/URN:NBN:fi:au:mts:m1157",
}


@dataclass
class SkosifyResult:
    """Summary of one ``run`` invocation."""

    input_triples: int
    output_triples: int
    dual_typed_works: int
    dual_typed_expressions: int
    inferred_narrower: int
    inferred_broader: int
    skipped_idempotent: bool
    output_path: str
    display_triples_added: int = 0

    def render(self) -> str:
        """Format this result as paste-ready text for the skosify CLI."""
        if self.skipped_idempotent:
            return (
                "Skosify skipped — output is newer than the inputs (canonical, "
                "overlay, config). Re-run with --force to override.\n"
                f"  output: {self.output_path}"
            )
        return "\n".join(
            (
                "Skosify run complete",
                f"  input triples:                  {self.input_triples:,}",
                f"  output triples:                 {self.output_triples:,}",
                f"  dual-typed Works:               {self.dual_typed_works:,}",
                f"  dual-typed Expressions:         {self.dual_typed_expressions:,}",
                f"  inferred skos:narrower triples: {self.inferred_narrower:,}",
                f"  inferred skos:broader  triples: {self.inferred_broader:,}",
                f"  display flatteners added:       {self.display_triples_added:,}",
                f"  output: {self.output_path}",
            )
        )


def _load_skosify_config(config_path: Path) -> dict[str, Any]:
    """Read ``bffi.cfg`` into the kwargs dict skosify expects.

    Deferred import — ``skosify`` is fast enough but reading the config
    fails at module import time on machines without skosify, which
    would prevent the rest of the CLI from loading.
    """
    from skosify import config as skosify_config

    cfg = skosify_config(str(config_path))
    return dict(cfg)


def _is_output_fresh(output_path: Path, input_paths: list[Path]) -> bool:
    if not output_path.is_file():
        return False
    out_mtime = output_path.stat().st_mtime
    return all(p.stat().st_mtime <= out_mtime for p in input_paths if p.is_file())


def _count_dual_typed(g: Graph, bffi_class: Any) -> int:
    """How many subjects are typed as both ``bffi_class`` and ``skos:Concept``?"""
    count = 0
    for s in set(g.subjects(V.RDF.type, bffi_class)):
        types = set(g.objects(s, V.RDF.type))
        if V.SKOS.Concept in types:
            count += 1
    return count


def _synthesise_display_predicates(graph: Graph) -> tuple[int, int]:
    """Add flat ``dct:creator`` / ``dct:contributor`` triples to the
    Skosmos-loaded graph so the standard concept-page template renders
    authors and contributors.

    BFFI 1.0.0's authorship model is the structured chain
    ``<entity> bffi:contribution <Contribution> ; <Contribution>
    bffi:agent <Agent>``. ``canonical.ttl`` carries that shape verbatim
    and stays BFFI-spec-clean. Skosmos's default Twig template, however,
    only renders direct outgoing properties on a concept page — it
    does not traverse blank-node ``bffi:contribution`` chains — so
    without a flat predicate the author exists in the graph but is
    invisible in the UI.

    The synthesis here lives in M10 / Skosify (not M8 / merge) so the
    canonical graph the project would contribute to NLF carries only
    BFFI-canonical triples; the Dublin Core display predicates appear
    only in the Skosmos-loaded artefact (``canonical-skosified.ttl``).

    Rules:
      - For every ``?s bffi:contribution ?c`` where ``?c`` is typed
        ``bffi:PrimaryContribution``: emit ``?s dct:creator ?agent``
        with the agent URI taken from ``?c bffi:agent ?agent``.
        ``?s`` is the canonical Work in practice.
      - For every ``?s bffi:contribution ?c`` NOT typed
        ``PrimaryContribution`` (i.e. ``bffi:Contribution`` base): emit
        ``?s dct:contributor ?agent``. ``?s`` is the canonical
        Expression in practice (M3-cascade-emitted translators,
        illustrators, performers).

    Returns the (creators_added, contributors_added) counts for the
    summary.
    """
    creators = 0
    contributors = 0
    for s, contrib in graph.subject_objects(V.BFFI.contribution):
        contrib_types = set(graph.objects(contrib, RDF.type))
        is_primary = V.BFFI.PrimaryContribution in contrib_types
        for agent in graph.objects(contrib, V.BFFI.agent):
            if is_primary:
                graph.add((s, DCTERMS.creator, agent))
                creators += 1
            else:
                graph.add((s, DCTERMS.contributor, agent))
                contributors += 1
    return creators, contributors


def _extract_marckey_subfield(marc_key: str, code: str) -> str | None:
    """Return the value of ``$<code>`` in a ``bflc:marcKey`` literal, or
    ``None`` if absent / empty after ISBD-punctuation trim.

    Mirrors :func:`bffi_pipeline.stages.m3.post_process._extract_g_subfield`
    but is parametric on the subfield code so the same parser can pull
    ``$a`` (title), ``$t`` (related work title), ``$g`` (agent), etc.
    Lives here rather than being imported because M10 has no other
    dependency on M3's post-process module, and the function is two
    cheap string ops.
    """
    marker = f"${code}"
    idx = marc_key.find(marker)
    if idx < 0:
        return None
    after = marc_key[idx + len(marker) :]
    next_delim = after.find("$")
    value = after if next_delim < 0 else after[:next_delim]
    return value.strip().rstrip("/.,;:").strip() or None


def _synthesise_component_pref_labels(graph: Graph) -> int:
    """For aggregation-component Expressions that carry a
    ``bflc:marcKey`` but no ``skos:prefLabel``, parse the ``$a`` (or
    ``$t`` as fallback) subfield and emit it as ``skos:prefLabel``.

    Why: M3-emitted component Expressions (~30k on the 500-sample) all
    have ``bflc:marcKey`` but only a fraction carry a ``skos:prefLabel``
    — M8's labeller only labels Works/Expressions it canonicalises.
    Without a prefLabel Skosmos shows the bare URI on the navigation
    grid. The ``$a`` subfield IS the title the cataloguer wrote, so a
    direct lift recovers the navigation experience.
    """
    added = 0
    for component in set(graph.subjects(RDF.type, V.BFFI.Expression)):
        existing = set(graph.objects(component, SKOS.prefLabel))
        if existing:
            continue
        for mk in graph.objects(component, V.BFLC.marcKey):
            mk_str = str(mk)
            title = _extract_marckey_subfield(mk_str, "a") or _extract_marckey_subfield(mk_str, "t")
            if title:
                graph.add((component, SKOS.prefLabel, Literal(title)))
                added += 1
                break
    return added


def _synthesise_provision_display(graph: Graph) -> int:
    """Flatten ``bffi:provisionActivity`` blank-node chains into
    ``dct:publisher`` / ``dct:date`` / ``dct:spatial`` on the parent
    Manifestation.

    Skosmos does not walk blank-node chains, so the
    ``bflc:simpleAgent`` / ``simpleDate`` / ``simplePlace`` triples
    inside the activity are invisible on a concept page. Emitting them
    flat on the Manifestation surfaces the publication line in the
    standard property grid. Records with multiple ProvisionActivities
    (publication + distribution + manufacture) accumulate parallel
    values; Skosmos renders them as a list which is the desired UI.
    """
    added = 0
    flat_predicates: tuple[tuple[Node, Node], ...] = (
        (V.BFLC.simpleAgent, DCTERMS.publisher),
        (V.BFLC.simpleDate, DCTERMS.date),
        (V.BFLC.simplePlace, DCTERMS.spatial),
    )
    structured_predicates: tuple[tuple[Node, Node], ...] = (
        (V.BF.date, DCTERMS.date),
        (V.BF.place, DCTERMS.spatial),
    )
    for s, pa in graph.subject_objects(V.BFFI.provisionActivity):
        for src_pred, dst_pred in flat_predicates:
            for value in graph.objects(pa, src_pred):
                if (s, dst_pred, value) not in graph:
                    graph.add((s, dst_pred, value))
                    added += 1
        for src_pred, dst_pred in structured_predicates:
            for value in graph.objects(pa, src_pred):
                if (s, dst_pred, value) not in graph:
                    graph.add((s, dst_pred, value))
                    added += 1
    return added


def _synthesise_title_alt_labels(graph: Graph) -> int:
    """For every ``?s bffi:title ?t . ?t bf:mainTitle ?mt`` chain whose
    ``?mt`` literal is not already the subject's ``skos:prefLabel``,
    emit ``?s skos:altLabel ?mt`` so the variant title surfaces on the
    concept page.

    ``bffi:title`` resolves to a Title resource (URI for canonical
    Manifestations, blank node for hub-derived variants); Skosmos
    does not walk that one-hop chain by default, so the title text
    is otherwise invisible. The prefLabel ``?s`` already carries
    (set by the M8 picker for Works/Expressions and by the
    Manifestation-template for Manifestations) wins on the page
    title; the synthesised altLabels collect every other variant.
    """
    added = 0
    for s, title in graph.subject_objects(V.BFFI.title):
        pref = set(graph.objects(s, SKOS.prefLabel))
        for main in graph.objects(title, V.BFFI.mainTitle):
            if main in pref:
                continue
            if (s, SKOS.altLabel, main) not in graph:
                graph.add((s, SKOS.altLabel, main))
                added += 1
    return added


def _synthesise_series_membership(graph: Graph) -> int:
    """For every ``?m bf:hasSeries ?series`` where ``?series`` carries
    a ``rdfs:label`` or ``bf:title → bf:mainTitle`` chain, emit
    ``?m dct:isPartOf <series>``.

    The series object is often a blank node (
    ``[ a bf:Series ; rdfs:label "Tammen kultaiset kirjat" ]``);
    Skosmos drops blank-node-only outgoing predicates from the
    rendered page. Adding a flat ``dct:isPartOf`` pointing at the
    series resource (URI or bnode) lets Skosmos render the series
    label one hop away when the series carries a label.
    """
    added = 0
    for s, series in graph.subject_objects(V.BF.hasSeries):
        if (s, DCTERMS.isPartOf, series) in graph:
            continue
        graph.add((s, DCTERMS.isPartOf, series))
        added += 1
    return added


def _synthesise_admin_metadata_highlights(graph: Graph) -> int:
    """Flatten the most cataloguer-relevant ``bffi:AdminMetadata``
    fields onto the parent entity:

      - ``?am dct:modified ?ts``        → ``?entity dct:modified ?ts``
      - ``?am bffi:descriptionConventions ?c`` → ``?entity dct:conformsTo ?c``

    AdminMetadata sits on its own URI (see ``bffi:AdminMetadata``
    block in canonical.ttl); Skosmos walks that one hop only for
    predicates it has labels for. Lifting ``dct:modified`` and the
    conventions URI onto the entity surfaces "last touched" and
    "catalogued under BFFI 1.0.0" on the concept page directly.
    """
    added = 0
    for s, am in graph.subject_objects(V.BFFI.adminMetadata):
        for ts in graph.objects(am, DCTERMS.modified):
            if (s, DCTERMS.modified, ts) not in graph:
                graph.add((s, DCTERMS.modified, ts))
                added += 1
        for conv in graph.objects(am, V.BFFI.descriptionConventions):
            if (s, DCTERMS.conformsTo, conv) not in graph:
                graph.add((s, DCTERMS.conformsTo, conv))
                added += 1
    return added


def _synthesise_role_predicates(graph: Graph) -> int:
    """For every ``?s bffi:contribution ?c . ?c bf:role ?role``, emit
    ``?s bf:role ?role`` so the role surfaces on the parent entity's
    concept page as its own property row.

    Same blank-node-traversal limitation as the ``dct:creator`` /
    ``dct:contributor`` synthesis (which lives in
    :func:`_synthesise_display_predicates`): the role lives one hop
    inside a Contribution blank node, invisible to Skosmos until
    lifted flat. Skosmos's concept page then shows "Role: [list]"
    next to "Creator" / "Contributor". The role-agent grouping is
    weaker than the canonical chain — multi-role records show
    parallel lists rather than a paired table — but the data is
    discoverable, which it currently is not.
    """
    added = 0
    for s, contrib in graph.subject_objects(V.BFFI.contribution):
        for role in graph.objects(contrib, V.BFFI.role):
            if (s, V.BFFI.role, role) not in graph:
                graph.add((s, V.BFFI.role, role))
                added += 1
    return added


def _best_display_label(graph: Graph, node: Node) -> str | None:
    """Return a human-readable label for ``node`` using the
    project-standard display-language priority (``fi → sv → en →
    unlanguaged → any``).

    Walks ``skos:prefLabel`` first (the canonical user-facing label
    after M9 reconciliation to KANTO / YSO / etc.), then falls back
    to ``rdfs:label`` (marc2bibframe2's cataloguer-text form on raw
    agent URIs and ``bf:Role`` blank nodes). Returns ``None`` when
    no label is reachable — caller decides whether to skip emission
    or substitute a placeholder.
    """
    pref_by_lang: dict[str | None, str] = {}
    for lab in graph.objects(node, V.SKOS.prefLabel):
        if isinstance(lab, Literal):
            pref_by_lang.setdefault(lab.language, str(lab))
    for lang_pref in ("fi", "sv", "en", None):
        if lang_pref in pref_by_lang:
            return pref_by_lang[lang_pref]
    if pref_by_lang:
        return next(iter(pref_by_lang.values()))
    # Fall back to rdfs:label — marc2bibframe2 emits these unlanguaged.
    for lab in graph.objects(node, V.RDFS.label):
        if isinstance(lab, Literal):
            return str(lab)
    return None


def _synthesise_contribution_labels(graph: Graph) -> int:
    """For every ``bffi:contribution`` blank node, compose
    ``"<agent-label> (<role-label>)"@fi`` and emit it as
    ``skos:prefLabel`` on the contribution.

    Why: Skosmos's default Twig template renders bnode property
    values using ``skos:prefLabel`` / ``rdfs:label`` when one is
    present on the bnode; otherwise the "Tekijyys" row shows a bare
    bnode identifier with no useful content. The structural BFFI
    chain (``?contrib bffi:agent ?agent . ?contrib bf:role ?role``)
    is intact in the canonical graph, but a one-hop walk isn't
    rendered by the default template. Composing a single
    presentation-ready literal on the bnode lets the standard
    template surface both pieces — agent name and role term —
    inline on the parent Work / Expression page.

    Role-label preference: the bnode-with-``rdfs:label`` form (the
    cataloguer's original Finnish/Swedish ``$e`` term like
    "säveltäjä") wins over the LoC relator URI's English label
    ("Composer") because Finnish is the page's display language.
    Falls back to the URI form when only the relator URI is
    present (rare; the marc2bibframe2 ``bf:Role`` bnode is the
    dominant Helmet shape).

    Composition rule:

      - ``agent_label`` present + ``role_label`` present →
        ``"<agent> (<role>)"@fi``
      - ``agent_label`` present + no ``role_label`` →
        ``"<agent>"@fi``
      - no ``agent_label`` → skip (no information to display).

    Idempotent: skips contributions that already carry any
    ``skos:prefLabel`` (re-run safe; doesn't accumulate).
    """
    added = 0
    for _s, contrib in graph.subject_objects(V.BFFI.contribution):
        if any(graph.objects(contrib, V.SKOS.prefLabel)):
            continue
        agent_label: str | None = None
        for agent in graph.objects(contrib, V.BFFI.agent):
            agent_label = _best_display_label(graph, agent)
            if agent_label:
                break
        if agent_label is None:
            continue
        role_label: str | None = None
        # Pass 1 — prefer the cataloguer's free-text term on the
        # bnode-form role.
        for role in graph.objects(contrib, V.BFFI.role):
            if not isinstance(role, URIRef):
                role_label = _best_display_label(graph, role)
                if role_label:
                    break
        # Pass 2 — fall back to the URI-form role's loaded label
        # (Finto vocab dumps provide ``rdfs:label`` on
        # ``<relators/cmp>``-style URIs).
        if role_label is None:
            for role in graph.objects(contrib, V.BFFI.role):
                if isinstance(role, URIRef):
                    role_label = _best_display_label(graph, role)
                    if role_label:
                        break
        composed = f"{agent_label} ({role_label})" if role_label else agent_label
        graph.add((contrib, V.SKOS.prefLabel, Literal(composed, lang="fi")))
        added += 1
    return added


#: Built-once-per-process MTS index lookup. The four collections are
#: stable across pipeline runs (NLF updates them at most a few times
#: a year); caching the parsed graph + index avoids parsing 2.7 MB
#: of Turtle on every Skosify invocation.
_MtsRoleIndex = dict[tuple[str, str], URIRef]


def _build_mts_role_index(mts_path: Path) -> _MtsRoleIndex:
    """Build ``{(finnish_term_lowercased, axis): mts_uri}`` from the four
    BFFI-prescribed ``bffi:Role`` MTS collections.

    Walks each collection's ``skos:member`` list and indexes every
    member by its Finnish ``skos:prefLabel`` AND any Finnish
    ``skos:altLabel`` (cataloguer-vernacular forms like ``"säv."`` —
    altLabels capture spellings cataloguers used historically).
    Axis is the FRBR axis of the parent collection
    (Work / Expression / Manifestation / Item).

    Returns an empty dict when ``mts_path`` doesn't exist (CI
    environments, smoke tests). Callers fall back to bnode-only
    role values in that case — the enrichment is purely additive.
    """
    if not mts_path.is_file():
        return {}
    mts = Graph()
    mts.parse(str(mts_path), format="turtle")
    index: _MtsRoleIndex = {}
    for axis, collection_uri in _MTS_ROLE_COLLECTION_BY_AXIS.items():
        collection = URIRef(collection_uri)
        for member in mts.objects(collection, V.SKOS.member):
            if not isinstance(member, URIRef):
                continue
            for pred in (V.SKOS.prefLabel, V.SKOS.altLabel):
                for lab in mts.objects(member, pred):
                    if not isinstance(lab, Literal) or lab.language != "fi":
                        continue
                    key = (str(lab).strip().lower(), axis)
                    # First write wins — prefLabel is iterated before
                    # altLabel for the same member because predicate
                    # order in ``(SKOS.prefLabel, SKOS.altLabel)`` is
                    # respected by rdflib's per-predicate object walk.
                    index.setdefault(key, member)
    return index


def _entity_axis(graph: Graph, entity: Node) -> str | None:
    """Return the FRBR axis (Work / Expression / Manifestation /
    Item) of ``entity`` by walking its ``rdf:type`` triples.

    Used to pick the matching MTS role collection when a contribution
    is being enriched: a role on a Work pulls from m34, on an
    Expression from m153, etc. Returns ``None`` for entities whose
    type is none of the four BFFI top-level classes (e.g. typed
    only as ``skos:Concept`` after the Skosify pass — shouldn't
    happen but we bail rather than misroute).
    """
    types = set(graph.objects(entity, RDF.type))
    if V.BFFI.Work in types:
        return "Work"
    if V.BFFI.Expression in types:
        return "Expression"
    if V.BFFI.Manifestation in types:
        return "Manifestation"
    if V.BFFI.Item in types:
        return "Item"
    return None


def _resolve_role_via_mts(
    mts_index: _MtsRoleIndex, term: str, preferred_axis: str | None
) -> URIRef | None:
    """Look up ``term`` in the MTS index, preferring the entity's
    axis-matching collection. Falls back to any of the four axes
    when the preferred-axis lookup misses (e.g. "säveltäjä" exists
    only on the Work-axis collection but is routed to Expressions
    in our pipeline via the 700 → Expression routing). Returns
    ``None`` when the term is in no axis-collection at all.
    """
    if preferred_axis is not None:
        hit = mts_index.get((term, preferred_axis))
        if hit is not None:
            return hit
    for fallback_axis in ("Work", "Expression", "Manifestation", "Item"):
        if fallback_axis == preferred_axis:
            continue
        hit = mts_index.get((term, fallback_axis))
        if hit is not None:
            return hit
    return None


def _synthesise_role_mts_uri(graph: Graph, mts_index: _MtsRoleIndex) -> tuple[int, int]:
    """For every ``?entity bffi:contribution ?c . ?c bffi:role ?b .
    ?b rdfs:label "<finnish-term>"`` chain, look up the term in the
    MTS index and emit ``?c bffi:role <mts:m...>`` alongside the
    existing bnode.

    Axis-prioritised lookup: the contribution's parent entity's
    BFFI class (Work / Expression / Manifestation / Item)
    determines which of the four ``bffi:Role`` value-vocab
    collections is consulted first. Falls back to any axis when
    the strict-axis lookup misses (e.g. "säveltäjä" only exists in
    the Work-axis collection ``m34``, but composer contributions
    in Helmet typically live on Expressions via the 700-routing —
    we resolve by axis-mismatch fallback).

    Returns ``(matched_count, unmatched_count)``. ``unmatched``
    is a candidate signal for proposing additions to the four
    MTS collections back to NLF — terms cataloguers use that the
    BFFI-prescribed value vocab doesn't cover yet.

    Skipped silently when ``mts_index`` is empty (MTS dump
    unavailable). The bnode-with-rdfs:label form remains; only the
    URI-form enrichment is missing.
    """
    if not mts_index:
        return (0, 0)
    matched = 0
    seen_terms_unmatched: set[str] = set()
    for entity, contrib in graph.subject_objects(V.BFFI.contribution):
        axis = _entity_axis(graph, entity)
        for role in graph.objects(contrib, V.BFFI.role):
            if isinstance(role, URIRef):
                continue
            for lab in graph.objects(role, V.RDFS.label):
                if not isinstance(lab, Literal):
                    continue
                term = str(lab).strip().lower()
                if not term:
                    continue
                mts_uri = _resolve_role_via_mts(mts_index, term, axis)
                if mts_uri is None:
                    seen_terms_unmatched.add(term)
                    continue
                if (contrib, V.BFFI.role, mts_uri) not in graph:
                    graph.add((contrib, V.BFFI.role, mts_uri))
                    matched += 1
                break  # one label match per role bnode is enough
    return (matched, len(seen_terms_unmatched))


def _synthesise_identifier_predicates(graph: Graph) -> int:
    """Flatten ``bf:identifiedBy`` blank nodes into flat type-specific
    predicates so identifiers are visible on the Manifestation page
    without a one-hop walk.

    Mapping table mirrors marc2bibframe2's identifier-type ontology:

      - ``bf:Isbn``             → ``bf:isbn "<value>"``
      - ``bf:Issn``             → ``bf:issn "<value>"``
      - ``bf:Ean``              → ``bf:ean "<value>"``
      - ``bf:AudioIssueNumber`` → ``bf:audioIssueNumber "<value>"``
      - ``bf:SystemNumber``     → ``bf:systemNumber "<value>"``

    ``bf:Local`` (Helmet bib ID) is intentionally excluded — the bib
    ID already surfaces as ``dct:identifier`` from the canonical
    Manifestation construct, so duplicating it would inflate the
    page noise. The ``rdf:value`` carries the literal in all five
    handled types.
    """
    type_to_predicate: dict[Node, Node] = {
        V.BF.Isbn: V.BF.isbn,
        V.BF.Issn: V.BF.issn,
        V.BF.Ean: V.BF.ean,
        V.BF.AudioIssueNumber: V.BF.audioIssueNumber,
        V.BF.SystemNumber: V.BF.systemNumber,
    }
    added = 0
    for s, idnode in graph.subject_objects(V.BFFI.identifiedBy):
        id_types = set(graph.objects(idnode, RDF.type))
        target_pred: Node | None = None
        for t in id_types:
            if t in type_to_predicate:
                target_pred = type_to_predicate[t]
                break
        if target_pred is None:
            continue
        for value in graph.objects(idnode, RDF.value):
            if (s, target_pred, value) not in graph:
                graph.add((s, target_pred, value))
                added += 1
    return added


def _summarise(
    skosified: Graph,
    *,
    input_triples: int,
    output_path: Path,
    skipped: bool,
    display_triples_added: int = 0,
) -> SkosifyResult:
    return SkosifyResult(
        input_triples=input_triples,
        output_triples=len(skosified),
        dual_typed_works=_count_dual_typed(skosified, V.BFFI.Work),
        dual_typed_expressions=_count_dual_typed(skosified, V.BFFI.Expression),
        inferred_narrower=len(list(skosified.triples((None, V.SKOS.narrower, None)))),
        inferred_broader=len(list(skosified.triples((None, V.SKOS.broader, None)))),
        skipped_idempotent=skipped,
        output_path=str(output_path),
        display_triples_added=display_triples_added,
    )


def run(
    canonical_path: Path | None = None,
    *,
    output_path: Path | None = None,
    overlay_path: Path | None = None,
    config_path: Path | None = None,
    mts_dump_path: Path | None = None,
    force: bool = False,
) -> SkosifyResult:
    """Run Skosify on the canonical Turtle + overlay; emit the dual-typed result."""
    from skosify import skosify

    settings = get_settings()
    canonical_path = canonical_path or (settings.data_dir / "canonical.ttl")
    overlay_path = overlay_path or DEFAULT_OVERLAY_PATH
    config_path = config_path or DEFAULT_CONFIG_PATH
    mts_dump_path = mts_dump_path or DEFAULT_MTS_DUMP_PATH
    output_path = output_path or (settings.data_dir / SKOSIFIED_FILENAME)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Total for the dashboard's row-2 ``${skosify_total}`` tile —
    # sourced from M8's ``canonical-map.jsonl`` (one row per canonical
    # Work; M9 / Skosify / Load don't add or remove Works so the count
    # is stable across all post-M8 stages).
    canonical_map = settings.data_dir / "canonical-map.jsonl"
    total = sum(1 for _ in canonical_map.open(encoding="utf-8")) if canonical_map.is_file() else 0
    emit_if_active(
        stage="skosify",
        event="start",
        counters={"total": total},
        extra={"input": str(canonical_path)},
    )

    if not canonical_path.is_file():
        raise FileNotFoundError(
            f"Canonical Turtle not found at {canonical_path!s}. Run `bffi-pipeline merge` first."
        )

    inputs = [canonical_path, overlay_path, config_path]

    if not force and _is_output_fresh(output_path, inputs):
        existing = Graph()
        existing.parse(str(output_path), format="turtle")
        result = _summarise(existing, input_triples=0, output_path=output_path, skipped=True)
        emit_if_active(
            stage="skosify",
            event="end",
            counters={"output_triples": len(existing)},
            extra={"skipped": True},
        )
        return result

    pre_graph = Graph()
    pre_graph.parse(str(canonical_path), format="turtle")
    input_triples = len(pre_graph)

    cfg = _load_skosify_config(config_path)
    skosified = skosify(str(canonical_path), str(overlay_path), **cfg)

    # Post-Skosify: synthesise display-only flat predicates for
    # Skosmos. Every triple added here lands ONLY in the Skosify
    # output (``canonical-skosified.ttl``); ``canonical.ttl`` stays
    # BFFI 1.0.0-pure for the NLF-shippable surface.
    #
    # The flatteners exist because Skosmos's concept-page template
    # only renders direct outgoing properties on a subject — it does
    # not walk one-hop chains through blank nodes or non-labeled
    # predicates. Each flattener lifts a piece of canonical-graph
    # structure into a flat predicate that Skosmos's default
    # template DOES render.
    creators, contributors = _synthesise_display_predicates(skosified)
    display_added = creators + contributors
    display_added += _synthesise_component_pref_labels(skosified)
    display_added += _synthesise_provision_display(skosified)
    display_added += _synthesise_title_alt_labels(skosified)
    display_added += _synthesise_series_membership(skosified)
    display_added += _synthesise_admin_metadata_highlights(skosified)
    display_added += _synthesise_role_predicates(skosified)
    # MTS role-URI enrichment (BFFI 1.0.0 value-vocab contract).
    # Builds the MTS index once and feeds it to the flattener. Runs
    # BEFORE ``_synthesise_contribution_labels`` so the composed
    # ``"<agent> (<role>)"`` prefLabel can use the URI's loaded
    # multilingual prefLabel as a fallback when the cataloguer's
    # bnode label is absent.
    mts_role_index = _build_mts_role_index(mts_dump_path)
    role_uris_added, role_terms_unmatched = _synthesise_role_mts_uri(skosified, mts_role_index)
    display_added += role_uris_added
    display_added += _synthesise_contribution_labels(skosified)
    display_added += _synthesise_identifier_predicates(skosified)

    V.bind_canonical_prefixes(skosified)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    skosified.serialize(destination=str(tmp), format="turtle")
    tmp.replace(output_path)

    emit_if_active(
        stage="skosify",
        event="end",
        counters={
            "input_triples": input_triples,
            "output_triples": len(skosified),
            "role_uris_added": role_uris_added,
            "role_terms_unmatched": role_terms_unmatched,
        },
        extra={"skipped": False},
    )
    return _summarise(
        skosified,
        input_triples=input_triples,
        output_path=output_path,
        skipped=False,
        display_triples_added=display_added,
    )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_OVERLAY_PATH",
    "SKOSIFIED_FILENAME",
    "SkosifyResult",
    "run",
]
