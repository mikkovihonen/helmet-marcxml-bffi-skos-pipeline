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

from rdflib import BNode, Graph, Literal, URIRef
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
#: ``bffi-meta:relatedValueVocabulary`` on ``vocab/lkd.rdf``'s
#: ``bffi:Role`` class, pointing at the four axis-collections below).
DEFAULT_MTS_DUMP_PATH: Final[Path] = _REPO_ROOT / "finto-dumps" / "mts-skos.ttl"

#: Vendored LoC MARC country code → YSO bridge with cached fi/sv/en
#: prefLabels. See docs/bffi_limitations.md L-12 for the rationale and
#: vocab/loc-countries-bridge.ttl for the source-of-truth file.
DEFAULT_LOC_COUNTRIES_BRIDGE_PATH: Final[Path] = _REPO_ROOT / "vocab" / "loc-countries-bridge.ttl"
_LOC_COUNTRY_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/countries/"

#: Sibling of the countries bridge — LoC MARC language URIs
#: <http://id.loc.gov/vocabulary/languages/{code}> with cached
#: multilingual prefLabels. Same rationale as L-12. See
#: vocab/loc-languages-bridge.ttl.
DEFAULT_LOC_LANGUAGES_BRIDGE_PATH: Final[Path] = _REPO_ROOT / "vocab" / "loc-languages-bridge.ttl"
_LOC_LANGUAGE_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"

#: Sibling — LoC MARC issuance URIs
#: <http://id.loc.gov/vocabulary/issuance/{code}> → MTS RDA Mode of
#: Issuance concepts. Four-term controlled vocab; see
#: vocab/loc-issuance-bridge.ttl.
DEFAULT_LOC_ISSUANCE_BRIDGE_PATH: Final[Path] = _REPO_ROOT / "vocab" / "loc-issuance-bridge.ttl"
_LOC_ISSUANCE_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/issuance/"

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
        # Compose a ``skos:prefLabel`` on the ProvisionActivity bnode
        # itself ("Helsinki: WSOY, 2020"-style) so Skosmos renders the
        # bnode value as readable text inline. Without this, Skosmos
        # shows the bnode as a bare genid link that dereferences to a
        # 404. The composition only fires when at least a place or
        # agent or date is present and no prefLabel already exists.
        if any(graph.objects(pa, V.SKOS.prefLabel)):
            continue
        place = next(
            (str(v) for v in graph.objects(pa, V.BFLC.simplePlace) if isinstance(v, Literal)),
            None,
        )
        agent = next(
            (str(v) for v in graph.objects(pa, V.BFLC.simpleAgent) if isinstance(v, Literal)),
            None,
        )
        date = next(
            (str(v) for v in graph.objects(pa, V.BFLC.simpleDate) if isinstance(v, Literal)),
            None,
        )
        # Structural fallback: some marc2bibframe2-emitted activities
        # carry only ``bf:date`` (typed literal) and ``bf:place``
        # (LoC country URI) without the ``bflc:simple*`` flattening.
        # Use the typed date literal and the country URI's local
        # name as last-resort label sources so the row still shows
        # something readable instead of ``_:genidN``.
        if not date:
            date = next(
                (str(v) for v in graph.objects(pa, V.BF.date) if isinstance(v, Literal)),
                None,
            )
        if not place:
            place = next(
                (
                    _best_display_label(graph, v) or _uri_local_name(str(v))
                    for v in graph.objects(pa, V.BF.place)
                    if isinstance(v, URIRef)
                ),
                None,
            )
        composed = _compose_provision_label(place, agent, date)
        if not composed:
            continue
        graph.add((pa, V.SKOS.prefLabel, Literal(composed, lang="fi")))
        added += 1
    return added


def _uri_local_name(uri: str) -> str:
    """Return the URI's last path / fragment segment as a fallback
    display label. ``"http://id.loc.gov/vocabulary/countries/fi"``
    → ``"fi"``.
    """
    for sep in ("#", "/"):
        if sep in uri:
            tail = uri.rsplit(sep, 1)[-1]
            if tail:
                return tail
    return uri


def _compose_provision_label(place: str | None, agent: str | None, date: str | None) -> str | None:
    """ISBD-style ``"Place : Agent, Date"`` composition with graceful
    degradation when any part is missing. Returns ``None`` when all
    three are missing/empty."""
    if place and agent and date:
        return f"{place} : {agent}, {date}"
    if place and agent:
        return f"{place} : {agent}"
    if agent and date:
        return f"{agent}, {date}"
    if place and date:
        return f"{place}, {date}"
    return place or agent or date or None


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


def _synthesise_title_part_display(graph: Graph) -> int:
    """Lift ``bffi:partNumber`` / ``bffi:partName`` from inside the
    Title bnode onto the parent entity so Skosmos renders them as
    direct property rows.

    Source shape:

        <Manifestation> bffi:title [
            bffi:mainTitle "Sota ja rauha" ;
            bffi:partNumber "2" ;
            bffi:partName   "Andrei" ] .

    ``_synthesise_title_alt_labels`` already lifts ``bf:mainTitle``
    to ``skos:altLabel``, but the part subfields stay buried on the
    inner Title bnode. This pass mirrors the pattern, attaching
    ``bffi:partNumber`` / ``bffi:partName`` as direct literals on
    the parent so the cataloguer-typed "no. 2, Andrei" part info
    appears next to the title on the concept page.
    """
    added = 0
    for s, title in graph.subject_objects(V.BFFI.title):
        for pn in graph.objects(title, V.BFFI.partNumber):
            if isinstance(pn, Literal) and (s, V.BFFI.partNumber, pn) not in graph:
                graph.add((s, V.BFFI.partNumber, pn))
                added += 1
        for pname in graph.objects(title, V.BFFI.partName):
            if isinstance(pname, Literal) and (s, V.BFFI.partName, pname) not in graph:
                graph.add((s, V.BFFI.partName, pname))
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


#: Axis label for AdminMetadata prefLabel composition, per
#: parent entity's BFFI type. Three languages mirror the project's
#: display priority (fi → sv → en).
_ADMIN_METADATA_AXIS_LABELS: Final[dict[URIRef, dict[str, str]]] = {
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:Work"): {
        "fi": "Teos",
        "sv": "Verk",
        "en": "Work",
    },
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:Expression"): {
        "fi": "Ekspressio",
        "sv": "Uttryck",
        "en": "Expression",
    },
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:Manifestation"): {
        "fi": "Manifestaatio",
        "sv": "Manifestation",
        "en": "Manifestation",
    },
}
_ADMIN_METADATA_PREFIX: Final[dict[str, str]] = {
    "fi": "Hallinnolliset metatiedot",
    "sv": "Administrativ metadata",
    "en": "Administrative metadata",
}


def _synthesise_admin_metadata_pref_labels(graph: Graph) -> int:
    """Compose a ``skos:prefLabel`` (fi/sv/en) on every
    ``bffi:AdminMetadata`` resource so its own Skosmos concept page
    renders a human-readable title and the parent W/E/M's
    "Hallinnolliset metatiedot" link surfaces composed text instead
    of the bare AdminMetadata URI.

    Composition: ``"<prefix>: <axis> -- <parent-prefLabel>"@<lang>``
    (using a literal en-dash in the actual emit; ASCII hyphens in
    this docstring to keep ruff RUF002 quiet), e.g.::

        "Hallinnolliset metatiedot: Teos -- Symphony no 1 in A flat, op. 55"@fi
        "Hallinnolliset metatiedot: Ekspressio -- Symphony no 1 in A flat, op. 55"@fi
        "Hallinnolliset metatiedot: Manifestaatio -- ..."@fi

    Falls back to the parent's Helmet bib ID (``dct:identifier``)
    when no usable prefLabel reaches ``_best_display_label`` —
    raw / partially-built records still get a readable link.

    Also mirrors the parent's plain prefLabel as ``skos:altLabel``
    so a cataloguer typing the Work title finds the AdminMetadata
    page in Skosmos's text search.

    Idempotent: skips AdminMetadata nodes that already carry any
    ``skos:prefLabel``.
    """
    added = 0
    for parent, am in graph.subject_objects(V.BFFI.adminMetadata):
        if any(graph.objects(am, V.SKOS.prefLabel)):
            continue
        # Axis is the first matching BFFI W/E/M type on the parent.
        # Most parents carry both the BFFI type and ``skos:Concept``;
        # we want the BFFI side.
        axis_labels: dict[str, str] | None = None
        for t in graph.objects(parent, V.RDF.type):
            if not isinstance(t, URIRef):
                continue
            if t in _ADMIN_METADATA_AXIS_LABELS:
                axis_labels = _ADMIN_METADATA_AXIS_LABELS[t]
                break
        parent_label = _best_display_label(graph, parent)
        if parent_label is None:
            # Helmet bib ID fallback so the row is still identifiable.
            for ident in graph.objects(parent, DCTERMS.identifier):
                if isinstance(ident, Literal):
                    parent_label = str(ident)
                    break
        if parent_label is None:
            continue
        for lang in ("fi", "sv", "en"):
            prefix = _ADMIN_METADATA_PREFIX[lang]
            axis = axis_labels[lang] if axis_labels else "?"
            composed = f"{prefix}: {axis} – {parent_label}"  # noqa: RUF001 — en-dash separator is intentional (typographic, not hyphen)
            graph.add((am, V.SKOS.prefLabel, Literal(composed, lang=lang)))
            added += 1
        # AltLabel mirrors the bare parent label for text-search
        # discoverability — typing the Work's title surfaces the
        # AdminMetadata page in the result set.
        graph.add((am, V.SKOS.altLabel, Literal(parent_label, lang="fi")))
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


#: Predicates whose blank-node values carry their human-readable
#: content as ``rdfs:label``. ``_synthesise_bnode_prefLabel_mirrors``
#: mirrors that label as ``skos:prefLabel`` on the bnode so Skosmos's
#: bnode-rendering picks it up — without the mirror Skosmos shows
#: the bnode as a genid link that 404s.
_BNODE_LABEL_PREDICATES: Final[tuple[Node, ...]] = (
    V.BFFI.note,
    V.BF.note,
    V.BFFI.extent,
    V.BFFI.tableOfContents,
    V.BF.hasSeries,
    DCTERMS.isPartOf,
    V.BF.language,
    V.BFFI.language,
    V.BFFI.role,
    V.BFFI.subject,
    V.BFFI.content,
    V.BFFI.relationship,
)


#: Predicates that count as "provenance-only" — a bnode carrying
#: ONLY these (and nothing else) has no user-visible content and is
#: prunable by :func:`_prune_empty_bnode_values`. ``bp:fromMarcField``
#: is the M2-post correlator's tokenisation tag attached to bnodes
#: pre-emptively; M3 SPARQL CONSTRUCTs that emit an unbound
#: ``bffi:title`` / ``bffi:note`` slot create a bnode that the
#: correlator then decorates with this token, defeating the
#: strict "zero outgoing properties" check.
_PROVENANCE_ONLY_PREDICATES: Final[frozenset[Node]] = frozenset(
    {
        URIRef("http://urn.fi/URN:NBN:fi:schema:bffi-prov#fromMarcField"),
    }
)


def _bnode_has_user_visible_content(graph: Graph, bnode: BNode) -> bool:
    """Return ``True`` if ``bnode`` carries any outgoing triple under
    a predicate that is NOT in :data:`_PROVENANCE_ONLY_PREDICATES`.

    Provenance-only bnodes are functionally empty from a Skosmos
    rendering standpoint — they carry the correlator's tagging but
    no label, no type, no content the operator can see. The prune
    pass treats them as removable.
    """
    return any(p not in _PROVENANCE_ONLY_PREDICATES for p, _o in graph.predicate_objects(bnode))


def _prune_empty_bnode_values(graph: Graph) -> int:
    """Remove ``<entity> <predicate> <empty-bnode>`` triples from the
    Skosify output when the bnode has no user-visible content.

    M3 SPARQL CONSTRUCTs that include unbound variables on the
    right-hand side (e.g. ``?exprURI bffi:title ?title`` where
    ``?title`` is unbound) emit an empty blank node into the
    canonical graph. Examples produced by
    ``sparql/bf_to_bffi_expression.rq``: ``bffi:title [ ]``,
    ``bffi:note [ ]``, ``bffi:role [ ]``. Skosmos renders these as
    rows ("Nimeke" / "Huomautus" / "Rooli") with a non-resolvable
    genid link that 404s — the bnode has no label, no type, nothing
    useful.

    "User-visible content" excludes the provenance-only predicates
    enumerated in :data:`_PROVENANCE_ONLY_PREDICATES` — those carry
    M2-post correlator metadata but no surface for the operator,
    so they don't save the bnode from pruning. The bnode's own
    provenance triple is removed alongside the parent triple so
    rdflib can drop the now-orphan bnode from the Turtle output.

    Runs after the bnode-prefLabel-mirror and label-composition
    passes so we don't accidentally prune bnodes that just got a
    label.
    """
    removed = 0
    parents_to_remove: list[tuple[Node, Node, Node]] = []
    bnodes_to_clear: set[BNode] = set()
    for s, p, o in graph.triples((None, None, None)):
        if not isinstance(o, BNode):
            continue
        if _bnode_has_user_visible_content(graph, o):
            continue
        parents_to_remove.append((s, p, o))
        bnodes_to_clear.add(o)
    for triple in parents_to_remove:
        graph.remove(triple)
        removed += 1
    # Drop the provenance-only triples on the now-orphan bnodes so
    # rdflib's Turtle serialiser doesn't emit a dangling
    # ``[ bffi-prov:fromMarcField "..." ] .`` standalone block.
    for bnode in bnodes_to_clear:
        for p, o in list(graph.predicate_objects(bnode)):
            graph.remove((bnode, p, o))
    return removed


def _synthesise_bnode_prefLabel_mirrors(graph: Graph) -> int:
    """Mirror ``rdfs:label`` onto ``skos:prefLabel`` for blank-node
    values of subject-side predicates that carry their content as
    a label.

    Skosmos's concept page renders an outgoing predicate's bnode
    value via ``skos:prefLabel`` / ``rdfs:label`` on the bnode itself.
    Multiple predicates (notes, extent, table of contents, language,
    role, series links) point at bnodes that carry ``rdfs:label`` but
    not ``skos:prefLabel`` — Skosmos doesn't pick up ``rdfs:label`` on
    bnodes consistently, so the bnode renders as a genid link that
    dereferences to a 404.

    This pass walks each predicate in :data:`_BNODE_LABEL_PREDICATES`
    and adds ``skos:prefLabel`` mirroring ``rdfs:label`` on every
    bnode value. The original ``rdfs:label`` stays untouched —
    round-trip readers that walk ``rdfs:label`` are unaffected.

    Idempotent: skips bnodes that already carry any
    ``skos:prefLabel``.
    """
    added = 0
    for predicate in _BNODE_LABEL_PREDICATES:
        for _s, target in graph.subject_objects(predicate):
            if isinstance(target, Literal):
                continue
            if any(graph.objects(target, V.SKOS.prefLabel)):
                continue
            for lbl in graph.objects(target, V.RDFS.label):
                if isinstance(lbl, Literal) and (target, V.SKOS.prefLabel, lbl) not in graph:
                    graph.add((target, V.SKOS.prefLabel, lbl))
                    added += 1
    return added


def _synthesise_note_display(graph: Graph) -> int:
    """Backwards-compat alias — kept for the existing test name.
    Calls :func:`_synthesise_bnode_prefLabel_mirrors` which covers
    notes alongside other bnode-with-rdfs:label predicates."""
    return _synthesise_bnode_prefLabel_mirrors(graph)


_NOTE_LANGUAGE_TYPE_TO_LABEL: Final[dict[str, str]] = {
    # http://id.loc.gov/vocabulary/resourceComponents/ — marc2bibframe2
    # routes MARC 041 sub-language relationships through the
    # ``resourceComponents`` vocab. Each component type means
    # "this note describes the language of <X>"; without a textual
    # prefix Skosmos shows just the language name, which is
    # ambiguous (it's also a ``bffi:language`` value elsewhere on
    # the same page).
    "otx": "Alkuteoksen kieli",
    "trans": "Käännöksen kieli",
    "intertitles": "Välitekstien kieli",
    "captions": "Kuvatekstien kieli",
    "subtitles": "Tekstityksen kieli",
    "sungtext": "Laulutekstin kieli",
    "audio": "Äänen kieli",
    "abstract": "Tiivistelmän kieli",
    "tableOfContents": "Sisällysluettelon kieli",
    "summary": "Tiivistelmän kieli",
    "accessibleAlt": "Saavutettavan vaihtoehdon kieli",
}
_NOTE_LANGUAGE_DEFAULT_LABEL: Final[str] = "Kielen tunnus"
_RESOURCE_COMPONENTS_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/resourceComponents/"


def _synthesise_note_language_labels(graph: Graph) -> int:
    """Compose a ``skos:prefLabel`` on every ``bffi:note`` bnode that
    carries only a ``bf:language`` URI (no ``rdfs:label``).

    Source shape (marc2bibframe2 routing of MARC 041 sub-language
    relationships, canonicalised to the BFFI namespace by the M3
    SPARQL):

        <Expression> bffi:note [
            a bffi:Note ;
            a <http://id.loc.gov/vocabulary/resourceComponents/otx> ;
            bffi:language <http://id.loc.gov/vocabulary/languages/fre> ] .

    With no textual label, Skosmos's "Huomautus" row renders as a
    bare ``_:genidN``. This pass composes
    ``"<role-prefix>: <language-label>"@fi`` from the
    ``resourceComponents`` axis (``otx`` → "Alkuteoksen kieli",
    ``trans`` → "Käännöksen kieli", etc.) plus the language URI's
    loaded prefLabel (from the LoC language bridge synthesised
    earlier in the chain).

    Falls back to a generic prefix ("Kielen tunnus") when the
    resourceComponents type isn't in the recognised set.
    Idempotent: skips notes that already carry any
    ``skos:prefLabel`` or ``rdfs:label``.
    """
    added = 0
    for _s, note in graph.subject_objects(V.BFFI.note):
        if not isinstance(note, BNode):
            continue
        if any(graph.objects(note, V.SKOS.prefLabel)):
            continue
        if any(graph.objects(note, V.RDFS.label)):
            continue
        language: URIRef | None = None
        for lang in graph.objects(note, V.BFFI.language):
            if isinstance(lang, URIRef):
                language = lang
                break
        if language is None:
            continue
        lang_label = _best_display_label(graph, language) or _uri_local_name(str(language))
        role_label: str | None = None
        for t in graph.objects(note, V.RDF.type):
            if isinstance(t, URIRef) and str(t).startswith(_RESOURCE_COMPONENTS_PREFIX):
                key = str(t)[len(_RESOURCE_COMPONENTS_PREFIX) :]
                role_label = _NOTE_LANGUAGE_TYPE_TO_LABEL.get(key)
                if role_label:
                    break
        prefix = role_label or _NOTE_LANGUAGE_DEFAULT_LABEL
        composed = f"{prefix}: {lang_label}"
        graph.add((note, V.SKOS.prefLabel, Literal(composed, lang="fi")))
        added += 1
    return added


def _synthesise_main_title_pref_label(graph: Graph) -> int:
    """Mirror ``bffi:mainTitle`` onto ``skos:prefLabel`` for every
    Title / VariantTitle blank node that lacks one.

    Source shape (typical for MARC 246 → bf:VariantTitle):

        <Expression> bffi:title [
            a              bf:VariantTitle ;
            bffi:mainTitle "Romaani rikoksesta ja maailmoista" ] .

    Without a ``skos:prefLabel`` on the VariantTitle bnode Skosmos's
    Twig template falls back to the bare ``_:genidN`` identifier in
    the "Nimeke" row (or, before the bnode-text patch, a 404 link).
    ``_synthesise_bnode_prefLabel_mirrors`` only walks
    ``rdfs:label`` and so doesn't catch this shape — VariantTitle
    carries its display content under ``bffi:mainTitle``.

    Idempotent: skips bnodes that already carry any
    ``skos:prefLabel``.
    """
    added = 0
    for s, mt in graph.subject_objects(V.BFFI.mainTitle):
        if not isinstance(s, BNode):
            continue
        if not isinstance(mt, Literal):
            continue
        if any(graph.objects(s, V.SKOS.prefLabel)):
            continue
        graph.add((s, V.SKOS.prefLabel, mt))
        added += 1
    return added


def _synthesise_related_resource_display(graph: Graph) -> int:
    """Lift the related-resource URI off ``bffi:relation`` blank-node
    chains as a direct ``dct:relation`` triple on the parent.

    Source shape:

        <Manifestation> bffi:relation [
            bffi:associatedResource <…#Hub730-N | #Work740-N> ;
            bffi:relationship <…relationship/relatedwork> ] .

    Skosmos doesn't walk into the bnode, so related-work links stay
    invisible on the concept page. ``dct:relation`` flat on the
    parent gives Skosmos a clickable property; the target's
    ``skos:prefLabel`` / ``skos:altLabel`` (already lifted by
    ``_synthesise_title_alt_labels`` for Hub-typed targets) renders
    as the link text. The structured ``bffi:relation`` chain stays
    in canonical for round-trip fidelity.
    """
    added = 0
    for s, rel in graph.subject_objects(V.BFFI.relation):
        for resource in graph.objects(rel, V.BFFI.associatedResource):
            if not isinstance(resource, URIRef):
                continue
            if (s, DCTERMS.relation, resource) not in graph:
                graph.add((s, DCTERMS.relation, resource))
                added += 1
    return added


#: Predicates on a Work / Expression / Manifestation that carry
#: subject-like references. When the same predicate also has an
#: authority-reconciled value (YSO / KANTO / etc.) on the same
#: subject, the raw bib URI is redundant for Skosmos display and
#: gets pruned by ``_prune_redundant_raw_subject_references``.
_SUBJECT_LIKE_PREDICATES: Final[tuple[Node, ...]] = (
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:subject"),
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:genreForm"),
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:geographicCoverage"),
    DCTERMS.temporal,
)
_RAW_BIB_URI_MARKER: Final[str] = "/URN:NBN:fi:bib:raw/"


def _prune_redundant_raw_subject_references(graph: Graph) -> int:
    """Remove ``<entity> <subject-pred> <raw-bib-URI>`` triples when
    the same entity ALSO has the same predicate pointing at an
    authority-reconciled URI that the raw URI ``skos:exactMatch``-es.

    Without this pass, Skosmos's concept page shows each subject
    twice on the same row: once as the raw bib URI (which has no
    prefLabel — renders as a bare URI link) and once as the
    authority URI (which renders with its Finto-loaded prefLabel).
    Cataloguers asked for the authority-only display.

    Pruned predicates: ``bffi:subject``, ``bffi:genreForm``,
    ``bffi:geographicCoverage``, ``dct:temporal``. The reified
    ``rdf:Statement`` triples that the round-trip's
    ``_emit_subjects`` walk uses still point at the raw URI as
    ``rdf:object`` and are NOT touched — only the flat fallback
    triple is pruned.

    Defensive: only prunes when the authority URI is actually
    present on the same entity (so single-occurrence raw subjects
    without an authority twin stay visible).
    """
    removed = 0
    for pred in _SUBJECT_LIKE_PREDICATES:
        triples_to_remove: list[tuple[Node, URIRef]] = []
        for s, target in graph.subject_objects(pred):
            if not isinstance(target, URIRef):
                continue
            if _RAW_BIB_URI_MARKER not in str(target):
                continue
            # Is there an authority twin on the same entity under
            # the same predicate?
            for auth in graph.objects(target, V.SKOS.exactMatch):
                if not isinstance(auth, URIRef):
                    continue
                if _RAW_BIB_URI_MARKER in str(auth):
                    continue  # twin is also a raw URI — not what we want
                if (s, pred, auth) in graph:
                    triples_to_remove.append((s, target))
                    break
        for s, target in triples_to_remove:
            graph.remove((s, pred, target))
            removed += 1
    return removed


def _synthesise_typed_subject_display(graph: Graph) -> int:
    """Mirror typed subject references onto type-specific predicates
    so Skosmos shows distinct rows for temporal / geographic /
    topical subjects instead of collapsing them all under
    ``bffi:subject``.

    Source shape:

        <Work> bffi:subject <yso:p…> .
        <yso:p…> a bffi:Temporal | bffi:Place | bffi:Topic .

    Skosmos renders one row per outgoing predicate, so the type
    distinction (carried on the TARGET) doesn't translate into the
    UI. This pass adds:

      - ``<Work> dct:temporal <target>`` when target is ``bffi:Temporal``
      - ``<Work> bffi:geographicCoverage <target>`` when target is ``bffi:Place``
      - (topical / untyped subjects stay under ``bffi:subject`` only)

    Triples are ADDITIVE — the original ``bffi:subject`` link stays
    in canonical so the round-trip's MARC 648/651/650 routing and
    the ``_build_raw_origin_hints`` walk both continue to find their
    inputs. The trade-off is that each temporal/geographic subject
    shows up twice on the concept page (once under "Aihe", once
    under the typed label). The typed label gives the cataloguer
    the disambiguation; the bffi:subject row is the catch-all.

    The ``rdfs:domain bffi:GeographicCoverage`` constraint on
    ``bffi:geographicCoverage`` is mildly violated (the target is a
    Place URI not a GeographicCoverage instance), same pragmatic
    stretch as ``_synthesise_admin_metadata_highlights``.
    """
    added = 0
    for s, target in graph.subject_objects(V.BFFI.subject):
        if not isinstance(target, URIRef):
            continue
        types = set(graph.objects(target, RDF.type))
        if V.BFFI.Temporal in types and (s, DCTERMS.temporal, target) not in graph:
            graph.add((s, DCTERMS.temporal, target))
            added += 1
        elif V.BFFI.Place in types and (s, V.BFFI.geographicCoverage, target) not in graph:
            graph.add((s, V.BFFI.geographicCoverage, target))
            added += 1
    return added


def _synthesise_classification_display(graph: Graph) -> int:
    """Flatten ``bffi:classification → bffi:Classification`` blank-node
    chains onto the parent entity as a literal Skosmos can render
    directly.

    Source shape:

        <Work> bffi:classification [
            bffi:classificationPortion "78" ;
            bf:source [ bffi:code "ykl" ] ] .

    Skosmos doesn't walk into the blank-node Classification, so the
    YKL / UDC / Dewey numbers stay invisible on the concept page.
    This pass lifts each ``bffi:classificationPortion`` literal onto
    the parent, formatted with the source vocab code in parentheses
    (``"78 (ykl)"``) when present. The Hub-side ``bffi:classification``
    chain stays in canonical for downstream consumers; this just adds
    a Skosmos-visible mirror.

    Uses ``bffi:classificationPortion`` as the lifted predicate; its
    ``rdfs:domain bffi:Classification`` in lkd.rdf is mildly violated
    by attaching directly to a Work, but the inference is benign
    (Skosmos doesn't OWL-reason; no SHACL shape enforces it) and the
    same pragmatic stretch as ``_synthesise_admin_metadata_highlights``.
    """
    added = 0
    for s, cls in graph.subject_objects(V.BFFI.classification):
        for portion in graph.objects(cls, V.BFFI.classificationPortion):
            if not isinstance(portion, Literal):
                continue
            source_code: str | None = None
            for src in graph.objects(cls, V.BF.source):
                for code in graph.objects(src, V.BFFI.code):
                    if isinstance(code, Literal):
                        source_code = str(code)
                        break
                if source_code:
                    break
            display = f"{portion} ({source_code})" if source_code else str(portion)
            lit = Literal(display)
            if (s, V.BFFI.classificationPortion, lit) not in graph:
                graph.add((s, V.BFFI.classificationPortion, lit))
                added += 1
    return added


def _best_display_label(graph: Graph, node: Node) -> str | None:
    """Return a human-readable label for ``node`` using the
    project-standard display-language priority (``fi → sv → en →
    unlanguaged → any``).

    Lookup order:

      1. ``skos:prefLabel`` — canonical user-facing label after M9
         reconciliation to KANTO / YSO / etc.
      2. ``rdfs:label`` — marc2bibframe2's cataloguer-text form on
         raw agent URIs and ``bf:Role`` blank nodes (often
         unlanguaged).
      3. ``skos:altLabel`` — variant form. Some raw bib Works (e.g.
         the ``Work740-N`` series-link targets) only carry an
         altLabel from MARC ``$g``-routed 740s; without this
         fallback, ``_synthesise_relation_labels`` returns None for
         them and the relation row renders as a bare ``_:genid``.
      4. ``bffi:title → bffi:mainTitle`` — last-resort hop into the
         entity's Title block. Catches the raw bib Works whose
         display content lives one node down.

    Returns ``None`` when no label is reachable — caller decides
    whether to skip emission or substitute a placeholder.
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
    for lab in graph.objects(node, V.RDFS.label):
        if isinstance(lab, Literal):
            return str(lab)
    for lab in graph.objects(node, V.SKOS.altLabel):
        if isinstance(lab, Literal):
            return str(lab)
    for title in graph.objects(node, V.BFFI.title):
        for mt in graph.objects(title, V.BFFI.mainTitle):
            if isinstance(mt, Literal):
                return str(mt)
    return None


def _mirror_preflabel_as_rdf_value(graph: Graph, bnode: Node) -> int:
    """Mirror every ``skos:prefLabel`` on ``bnode`` as ``rdf:value``.

    Why ``rdf:value``: Skosmos's
    ``GenericSparql::generateConceptInfoQuery`` only pulls a bnode's
    outgoing properties (``?o ?oprop ?oval``) inside the OPTIONAL
    branch gated by ``?o rdf:value ?ov``. Without an ``rdf:value``
    on the bnode, the rendered EasyRdf graph arrives with just
    ``rdf:type`` + ``skos:prefLabel`` — and the
    ``ConceptPropertyValue::getUri`` override (bind-mounted from
    ``config/skosmos-overrides/ConceptPropertyValue.php``) can't
    find the ``bffi:agent`` / ``bffi:associatedResource`` triple it
    needs to redirect the "Tekijyys" / "Liittyy" link to the
    target's concept page. Mirroring the prefLabel as ``rdf:value``
    flips that branch on without changing the Skosmos-visible
    label: ``ConceptPropertyValue::getLabel`` prefers
    ``skos:prefLabel`` whenever it exists, and ``isReified()`` only
    fires when the label is absent.

    Called before the idempotency guard in the contribution /
    relation label passes so re-runs on ``canonical-skosified.ttl``
    (which already carries the prefLabel from a prior pass) still
    emit the trigger triple.
    """
    added = 0
    for label in graph.objects(bnode, V.SKOS.prefLabel):
        if (bnode, V.RDF.value, label) not in graph:
            graph.add((bnode, V.RDF.value, label))
            added += 1
    return added


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
        added += _mirror_preflabel_as_rdf_value(graph, contrib)
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
        composed_literal = Literal(composed, lang="fi")
        graph.add((contrib, V.SKOS.prefLabel, composed_literal))
        # ``rdf:value`` mirror is the Skosmos deep-fetch trigger — see
        # :func:`_mirror_preflabel_as_rdf_value`.
        graph.add((contrib, V.RDF.value, composed_literal))
        added += 1
    return added


def _relationship_label(graph: Graph, relationship: Node) -> str | None:
    """Return a display label for a ``bffi:relationship`` value.

    Prefers any loaded label (``skos:prefLabel`` / ``rdfs:label``).
    Falls back to the URI's local name when the value is a LoC
    relator URI — those aren't loaded in any Finto graph so the bare
    fragment ("series", "relatedwork") is the most useful hint
    available. Returns ``None`` for blank nodes with no labels.
    """
    label = _best_display_label(graph, relationship)
    if label:
        return label
    if isinstance(relationship, URIRef):
        text = str(relationship)
        for sep in ("#", "/"):
            if sep in text:
                tail = text.rsplit(sep, 1)[-1]
                if tail:
                    return tail
        return text
    return None


def _synthesise_relation_labels(graph: Graph) -> int:
    """For every ``bffi:relation`` blank node, compose
    ``"<resource-label> (<relationship-label>)"@fi`` and emit it as
    ``skos:prefLabel`` on the relation.

    Why: Skosmos's concept-page Twig template renders bnode values
    through their ``skos:prefLabel`` / ``rdfs:label``. The structural
    BFFI chain (``?rel bffi:associatedResource ?ar ;
    bffi:relationship ?relator``) lives one hop away from the
    parent entity and isn't walked by the template, so without a
    composed label every ``bffi:relation`` row renders as a bare
    ``_:genidN`` text fragment (or, before the Twig patch, a 404
    link to that genid). Composing a presentation literal puts the
    related resource's title + relationship hint inline on the
    parent Work / Expression / Manifestation page.

    Companion data-side trigger: ``rdf:value`` is mirrored alongside
    ``skos:prefLabel`` so Skosmos's
    ``GenericSparql::generateConceptInfoQuery`` pulls the bnode's
    ``bffi:associatedResource`` triple into the rendered EasyRdf
    graph. The PHP override (``config/skosmos-overrides/
    ConceptPropertyValue.php``) then redirects the "Liittyy"
    row's link target through ``bffi:associatedResource`` to the
    related Hub / Work — same pattern as the contribution
    bnode-with-``bffi:agent`` detour.

    Composition rule:

      - ``resource_label`` present + ``relationship_label`` present →
        ``"<resource> (<relationship>)"@fi``
      - ``resource_label`` present + no ``relationship_label`` →
        ``"<resource>"@fi``
      - no ``resource_label`` → skip (no information to display;
        the structural triples stay in canonical for downstream
        consumers).

    Idempotent: skips relations that already carry any
    ``skos:prefLabel`` (re-run safe; doesn't accumulate). Mirrors
    any pre-existing prefLabel as ``rdf:value`` so re-runs on
    ``canonical-skosified.ttl`` still emit the deep-fetch trigger.
    """
    added = 0
    for _s, relation in graph.subject_objects(V.BFFI.relation):
        added += _mirror_preflabel_as_rdf_value(graph, relation)
        if any(graph.objects(relation, V.SKOS.prefLabel)):
            continue
        resource_label: str | None = None
        for resource in graph.objects(relation, V.BFFI.associatedResource):
            resource_label = _best_display_label(graph, resource)
            if resource_label:
                break
        if resource_label is None:
            continue
        relationship_label: str | None = None
        for relationship in graph.objects(relation, V.BFFI.relationship):
            relationship_label = _relationship_label(graph, relationship)
            if relationship_label:
                break
        composed = (
            f"{resource_label} ({relationship_label})" if relationship_label else resource_label
        )
        composed_literal = Literal(composed, lang="fi")
        graph.add((relation, V.SKOS.prefLabel, composed_literal))
        graph.add((relation, V.RDF.value, composed_literal))
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


#: BFFI subject classes the round-trip converter's
#: ``_subject_marc_tag`` routes on. Same set as M8's
#: ``_SUBJECT_TYPING_PREDICATES`` — kept in sync because both passes
#: feed the same downstream consumer. Listed here to avoid a cross-stage
#: import from M8.
_SUBJECT_TYPING_BFFI_CLASSES: Final[tuple[URIRef, ...]] = (
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:Topic"),
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:Place"),
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:Temporal"),
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:Person"),
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:Organization"),
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:Meeting"),
)


def _propagate_subject_typing_via_exact_match(graph: Graph) -> int:
    """Propagate BFFI subject typing across ``skos:exactMatch`` edges.

    M8's ``_propagate_subject_typing`` already lifts the typing on
    raw ``#Temporal648-N`` / ``#Place651-N`` / ``#Topic650-N`` nodes
    into canonical. M9 then writes ``skos:exactMatch`` triples from
    those raw nodes to the YSO URIs they reconciled to — but M9 runs
    AFTER M8, so the M8 propagation can't see the exactMatch yet.

    Without this pass, the round-trip converter's ``_subject_marc_tag``
    looks at a YSO URI subject (e.g. ``yso:p6191061919`` for
    "1910-luku") and finds no ``rdf:type`` triple — unless some
    OTHER record in the corpus catalogued the same URI with ``$0``
    (which DOES type the YSO URI directly in BIBFRAME via
    ``<bf:Temporal rdf:about="yso:..."/>``). The asymmetry leaks
    "1910-luku" into MARC 650 while "1930-luku" stays in 648
    purely because some 1930-luku record had a ``$0`` and no
    1910-luku record did.

    This pass closes that gap: walks every routable rdf:type triple
    on a raw subject URI and copies it to the URI's
    ``skos:exactMatch`` target. Returns triples added.
    """
    routable = set(_SUBJECT_TYPING_BFFI_CLASSES)
    added = 0
    for raw_uri, _p, t in graph.triples((None, RDF.type, None)):
        if not isinstance(raw_uri, URIRef) or t not in routable:
            continue
        for target in graph.objects(raw_uri, SKOS.exactMatch):
            if not isinstance(target, URIRef):
                continue
            if (target, RDF.type, t) not in graph:
                graph.add((target, RDF.type, t))
                added += 1
    return added


def _load_loc_bridge(bridge_path: Path) -> Graph:
    """Parse a LoC bridge TTL (countries / languages / future
    siblings).

    Returns an empty graph when the file is missing — Skosify still
    runs; the URIs remain bare in the output, which matches the
    pre-bridge behaviour and is non-fatal.
    """
    g = Graph()
    if bridge_path.is_file():
        g.parse(str(bridge_path), format="turtle")
    return g


#: Triples copied from a bridge graph onto each referenced LoC URI in
#: the canonical graph. Three is enough to make Skosmos render with a
#: language-tagged label and to leave a back-reference for downstream
#: queries.
_LOC_BRIDGE_TARGET_PREDICATES: Final[tuple[Node, ...]] = (
    SKOS.prefLabel,
    SKOS.exactMatch,
    SKOS.notation,
)


def _synthesise_loc_bridge_labels(
    graph: Graph,
    bridge: Graph,
    uri_prefix: str,
) -> int:
    """Materialise multilingual labels on every LoC URI under
    ``uri_prefix`` referenced from the canonical graph.

    Used for both the countries bridge
    (``vocab/loc-countries-bridge.ttl``,
    ``http://id.loc.gov/vocabulary/countries/``) and the languages
    bridge (``vocab/loc-languages-bridge.ttl``,
    ``http://id.loc.gov/vocabulary/languages/``). For each LoC URI
    used as an object anywhere in the canonical graph, look it up in
    the bridge graph and copy its ``skos:prefLabel`` /
    ``skos:exactMatch`` / ``skos:notation`` triples onto the same
    URI in the canonical graph. Skosmos then renders the labels on
    the LoC concept page without having to follow the exactMatch at
    query time.

    Codes absent from the bridge are skipped silently. Idempotent.
    See ``docs/bffi_limitations.md`` L-12 for the underlying
    authority-interop gap.
    """
    if len(bridge) == 0:
        return 0
    referenced_uris: set[URIRef] = {
        o for _s, _p, o in graph if isinstance(o, URIRef) and str(o).startswith(uri_prefix)
    }
    added = 0
    for uri in referenced_uris:
        for pred in _LOC_BRIDGE_TARGET_PREDICATES:
            for value in bridge.objects(uri, pred):
                if (uri, pred, value) not in graph:
                    graph.add((uri, pred, value))
                    added += 1
    return added


def _load_loc_countries_bridge(bridge_path: Path) -> Graph:
    """Backwards-compat alias for :func:`_load_loc_bridge` — kept
    until callers migrate to the generic helper. The two are
    structurally identical."""
    return _load_loc_bridge(bridge_path)


def _synthesise_country_labels(graph: Graph, bridge: Graph) -> int:
    """Backwards-compat alias for :func:`_synthesise_loc_bridge_labels`
    targeting the countries URI prefix. Kept for the existing tests
    in ``tests/unit/test_skosify_run.py`` that test the country
    bridge by name."""
    return _synthesise_loc_bridge_labels(graph, bridge, _LOC_COUNTRY_URI_PREFIX)


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
    loc_countries_bridge_path: Path | None = None,
    loc_languages_bridge_path: Path | None = None,
    loc_issuance_bridge_path: Path | None = None,
    force: bool = False,
) -> SkosifyResult:
    """Run Skosify on the canonical Turtle + overlay; emit the dual-typed result."""
    from skosify import skosify

    settings = get_settings()
    canonical_path = canonical_path or (settings.data_dir / "canonical.ttl")
    overlay_path = overlay_path or DEFAULT_OVERLAY_PATH
    config_path = config_path or DEFAULT_CONFIG_PATH
    mts_dump_path = mts_dump_path or DEFAULT_MTS_DUMP_PATH
    loc_countries_bridge_path = loc_countries_bridge_path or DEFAULT_LOC_COUNTRIES_BRIDGE_PATH
    loc_languages_bridge_path = loc_languages_bridge_path or DEFAULT_LOC_LANGUAGES_BRIDGE_PATH
    loc_issuance_bridge_path = loc_issuance_bridge_path or DEFAULT_LOC_ISSUANCE_BRIDGE_PATH
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
    # NOTE: ``_synthesise_display_predicates`` (dct:creator /
    # dct:contributor) and ``_synthesise_role_predicates``
    # (bffi:role on the entity) used to emit entity-level mirrors of
    # the contribution chain for Skosmos rendering. They've been
    # disabled: Skosmos's concept page rendered them as parallel
    # "Tekijä" + "Rooli" rows with no visual indication that a
    # specific role belongs to a specific agent, which was confusing
    # in the UI (the agent ↔ role pairing is implicit in MARC but
    # explicit in the bffi:Contribution bnode that
    # ``_synthesise_contribution_labels`` composes a ``skos:prefLabel``
    # on — e.g. ``"Elgar, Edward (säveltäjä)"@fi``). Skosmos walks
    # one hop into a bnode object to read its ``skos:prefLabel`` /
    # ``rdfs:label``, so the single ``bffi:contribution`` row carries
    # the paired display directly. The canonical
    # ``dct:creator`` / ``dct:contributor`` / ``bffi:role``
    # triples remain in ``canonical.ttl`` for downstream BFFI
    # consumers; only the Skosify-time flat MIRRORS were removed.
    display_added = 0
    for flattener in (
        _synthesise_component_pref_labels,
        _synthesise_provision_display,
        _synthesise_title_alt_labels,
        _synthesise_title_part_display,
        _synthesise_series_membership,
        _synthesise_admin_metadata_pref_labels,
        _synthesise_typed_subject_display,
        _prune_redundant_raw_subject_references,
        _synthesise_classification_display,
        _synthesise_note_display,
        _synthesise_main_title_pref_label,
        _synthesise_related_resource_display,
        # Run AFTER every flattener that could have added properties
        # to a previously-empty bnode (so we don't accidentally prune
        # something we just enriched).
        _prune_empty_bnode_values,
    ):
        display_added += flattener(skosified)
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
    display_added += _synthesise_relation_labels(skosified)
    display_added += _synthesise_identifier_predicates(skosified)
    # Materialise multilingual prefLabels on LoC MARC country URIs
    # from our local bridge (see docs/bffi_limitations.md L-12).
    loc_countries_bridge = _load_loc_bridge(loc_countries_bridge_path)
    display_added += _synthesise_loc_bridge_labels(
        skosified, loc_countries_bridge, _LOC_COUNTRY_URI_PREFIX
    )
    # Same for LoC MARC language URIs (sibling bridge).
    loc_languages_bridge = _load_loc_bridge(loc_languages_bridge_path)
    display_added += _synthesise_loc_bridge_labels(
        skosified, loc_languages_bridge, _LOC_LANGUAGE_URI_PREFIX
    )
    # Compose labels for ``bffi:note`` bnodes that carry only a
    # ``bf:language`` URI (marc2bibframe2's MARC 041 sub-language
    # routing). Runs AFTER the language bridge so the composed
    # ``"Alkuteoksen kieli: <lang-label>"`` has a real language
    # label to splice in.
    display_added += _synthesise_note_language_labels(skosified)
    # Same for LoC MARC issuance URIs (mono / mas / serl / intg) →
    # MTS RDA Mode of Issuance concepts.
    loc_issuance_bridge = _load_loc_bridge(loc_issuance_bridge_path)
    display_added += _synthesise_loc_bridge_labels(
        skosified, loc_issuance_bridge, _LOC_ISSUANCE_URI_PREFIX
    )
    # Propagate subject typing across skos:exactMatch (M9 → YSO URIs)
    # so the round-trip's 648/651/600 routing fires consistently. See
    # the helper docstring for the b19845637 1910-luku reproducer.
    display_added += _propagate_subject_typing_via_exact_match(skosified)

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
