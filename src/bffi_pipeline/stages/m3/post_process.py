"""M3 graph post-processing — chains the per-stage helpers carved
out into ``language_detect`` / ``contributions``.

``post_process`` mutates the BFFI graph the CONSTRUCT pass produced
in three ways:

- Tags ``skos:prefLabel`` literals with BCP-47 language codes via
  the Lingua + optional local-LLM cascade
  (:mod:`bffi_pipeline.stages.m3.language_detect`).
- Optionally runs the MARC 245$c contributor-extraction cascade
  (:mod:`bffi_pipeline.stages.m3.contributions`).
- Binds the BFFI / Bibframe / Bib / DCT / RDF / SKOS prefixes on the
  output graph for human-readable Turtle.

P-38 Phase D: extracted from m3/runner.py to keep the runner focused
on the per-record driver loop. No logic change — moves only.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m3.contributions import _emit_extracted_contributions
from bffi_pipeline.stages.m3.language_detect import (
    _LANG_3_TO_2,
    _candidate_languages,
    _retag_pref_labels,
)

#: LoC vocab URIs that carry per-record cataloguer-typed
#: ``rdfs:label`` text (e.g. MARC 33X ``$a``). Without per-record
#: language tagging, M8's :func:`_propagate_loc_vocab_labels` merges
#: Finnish- and Swedish-source records' labels onto the same shared
#: URI, and Skosify's ``default_language = fi`` config force-tags
#: every untagged literal as ``@fi`` — including the Swedish ones,
#: confusing the round-trip converter's label lookup.
_LOC_VOCAB_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/"
_LANGUAGES_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"


def _primary_record_language(source: Graph) -> str | None:
    """Pick the source record's primary language as a BCP-47 code.

    Reads ``bf:Work bf:language`` URIs (skipping contained / related
    works the M3 SPARQL filters out) and returns the first one whose
    MARC 3-letter tail maps to a BCP-47 code in
    :data:`_LANG_3_TO_2`. Returns ``None`` when no Work has a routable
    language — in which case the caller skips re-tagging and rdfs:label
    triples stay untagged for Skosify to default-language them.
    """
    contained = {
        o
        for _, _, o in source.triples((None, V.BF.associatedResource, None))
        if isinstance(o, URIRef)
    }
    for work in source.subjects(RDF.type, V.BF.Work):
        if not isinstance(work, URIRef) or work in contained:
            continue
        for lang in source.objects(work, V.BF.language):
            if not isinstance(lang, URIRef):
                continue
            s = str(lang)
            if not s.startswith(_LANGUAGES_URI_PREFIX):
                continue
            code = _LANG_3_TO_2.get(s[len(_LANGUAGES_URI_PREFIX) :])
            if code is not None:
                return code
    return None


def _extract_g_subfield(marc_key: str) -> str | None:
    """Parse a MARC marcKey literal (e.g. ``"73000 $aFame /$gGore,
    Michael"``) and return the ``$g`` subfield value with trailing
    ISBD punctuation stripped. Returns ``None`` when no ``$g`` is
    present or the extracted value is empty.

    MARC marcKey format: leading tag (3 digits) + indicators (2 chars)
    + space + subfield-delimiter pairs (``$<code><value>``). The next
    ``$`` after ``$g`` marks the end of the agent value. ISBD
    punctuation that the previous subfield carried at its tail (``/``,
    ``,``, ``;``, ``:`` etc.) is also stripped from the ``$g`` value.
    """
    idx = marc_key.find("$g")
    if idx < 0:
        return None
    after_g = marc_key[idx + 2 :]
    next_delim = after_g.find("$")
    g_value = after_g if next_delim < 0 else after_g[:next_delim]
    return g_value.strip().rstrip("/.,;:").strip() or None


def _enrich_aggregation_components_with_agents(bffi_graph: Graph) -> None:
    """For each aggregation component Expression that carries a source
    ``bflc:marcKey`` literal, parse the ``$g`` subfield and synthesise
    a contribution chain on the component:

        <component> bffi:contribution _:c .
        _:c        a              bffi:Contribution ;
                   bffi:agent     _:agent .
        _:agent    a              bf:Agent ;
                   rdfs:label     "<agent name>" .

    Drives two downstream consumers:

    1. **Round-trip 700 ind2=2 emit** (P-52 Phase G.bis). The
       converter walks ``bffi:Expression bffi:aggregates ?component``
       and, for each component with a contribution, emits a
       ``700 ind2=2 $a <name>`` analytical-entry row.

    2. **M9 component-agent reconciliation** (P-52 Phase H). M9's
       ``_iter_creator_requests`` already walks every
       ``bffi:Expression bffi:contribution → bffi:agent`` chain;
       component agents flow through automatically.

    Idempotent: skips components that already have any
    ``bffi:contribution`` (re-runs against an already-enriched graph
    are no-ops).
    """
    seen_components: set[URIRef] = set()
    for _parent, _p, component in bffi_graph.triples((None, V.BFFI.aggregates, None)):
        if not isinstance(component, URIRef) or component in seen_components:
            continue
        seen_components.add(component)
        if (component, V.BFFI.contribution, None) in bffi_graph:
            continue
        agent_label: str | None = None
        for mk in bffi_graph.objects(component, V.BFLC.marcKey):
            if isinstance(mk, Literal):
                agent_label = _extract_g_subfield(str(mk))
                if agent_label:
                    break
        if not agent_label:
            continue
        contrib = BNode()
        agent = BNode()
        bffi_graph.add((component, V.BFFI.contribution, contrib))
        bffi_graph.add((contrib, RDF.type, V.BFFI.Contribution))
        bffi_graph.add((contrib, V.BFFI.agent, agent))
        bffi_graph.add((agent, RDF.type, V.BFFI.Agent))
        bffi_graph.add((agent, RDFS.label, Literal(agent_label)))


def _merge_duplicate_provision_activities(bffi_graph: Graph) -> None:
    """Collapse duplicate ``bffi:provisionActivity`` blank nodes on each
    Manifestation into a single node, merging their outgoing triples.

    Source 260/264 fields end up as TWO ``bf:ProvisionActivity`` blank
    nodes in the BIBFRAME marc2bibframe2 emits — one carrying the
    ``bflc:simple*`` transcription only, one carrying the same
    ``bflc:simple*`` plus the 008-derived ``bf:date`` / ``bf:place``
    normalisation. The round-trip converter walks both targets and
    emits two identical 264 rows. See ConvSpec-Process8-ProvAct.xsl
    lines 214 + 460 for the upstream double-emit; modifying the
    submodule is prohibited per CLAUDE.md so we collapse the
    duplication at the BFFI canonical layer.

    Grouping signature: ``(rdf:type set, bflc:simplePlace literals,
    bflc:simpleAgent literals, bflc:simpleDate literals)``. Within a
    group, the node with the most outgoing triples wins (so the
    richer ``bf:date`` / ``bf:place``-bearing node survives); losers
    have their non-redundant triples copied onto the winner, then are
    removed along with the Manifestation's ``bffi:provisionActivity``
    edge pointing at them.

    Manifestations with one ProvisionActivity, or with multiple
    legitimately distinct activities (e.g. Publication + Distribution
    + Manufacture for serials), are unaffected — distinct activities
    differ in their type set or their ``bflc:simple*`` content and
    don't group.
    """
    for manif in bffi_graph.subjects(RDF.type, V.BFFI.Manifestation):
        targets = [
            t for t in bffi_graph.objects(manif, V.BFFI.provisionActivity) if isinstance(t, BNode)
        ]
        if len(targets) < 2:  # noqa: PLR2004 — need two activities to deduplicate
            continue
        groups: dict[
            tuple[frozenset[URIRef], frozenset[Literal], frozenset[Literal], frozenset[Literal]],
            list[BNode],
        ] = {}
        for t in targets:
            types = frozenset(o for o in bffi_graph.objects(t, RDF.type) if isinstance(o, URIRef))
            places = frozenset(
                o for o in bffi_graph.objects(t, V.BFLC.simplePlace) if isinstance(o, Literal)
            )
            agents = frozenset(
                o for o in bffi_graph.objects(t, V.BFLC.simpleAgent) if isinstance(o, Literal)
            )
            dates = frozenset(
                o for o in bffi_graph.objects(t, V.BFLC.simpleDate) if isinstance(o, Literal)
            )
            sig = (types, places, agents, dates)
            groups.setdefault(sig, []).append(t)
        for nodes in groups.values():
            if len(nodes) < 2:  # noqa: PLR2004 — singleton groups need no merge
                continue
            winner = max(nodes, key=lambda n: sum(1 for _ in bffi_graph.triples((n, None, None))))
            for loser in nodes:
                if loser == winner:
                    continue
                for _s, p, o in list(bffi_graph.triples((loser, None, None))):
                    bffi_graph.add((winner, p, o))
                    bffi_graph.remove((loser, p, o))
                bffi_graph.remove((manif, V.BFFI.provisionActivity, loser))


def _tag_manifestation_pref_labels_with_primary_language(bffi_graph: Graph, source: Graph) -> None:
    """Pre-tag ``bffi:Manifestation``-side ``skos:prefLabel`` literals
    with the record's primary language so the LLM title-language
    cascade doesn't fire on them.

    M3's manifestation CONSTRUCT synthesises the Manifestation
    prefLabel by concatenating the source title with the publication
    statement (e.g. ``"AADA WILDE (Helsinki : Otava, 1912)"``). This
    is pipeline-generated, NOT cataloguer-typed, and:

    1. The same conceptual title already lives on the bf:Work /
       bffi:Expression with a clean (no-parenthetical) form — language
       detection on THAT literal is the authoritative answer.
    2. The synthetic concatenation can confuse the detector when the
       publication statement is in a different language than the
       title (e.g. Finnish title + Swedish-language imprint).
    3. Sending it through the LLM cascade burns cycles for no value;
       the language detector audit log fills with synthetic-looking
       rows that aren't actionable.

    Pre-tagging here marks each synthetic Manifestation prefLabel as
    already-tagged with the record's primary language. ``_retag_pref_labels``
    then skips the literal (its ``o.language`` is now truthy).

    Returns silently when no primary language is detected — the
    LLM cascade falls back to its existing per-text detection.
    """
    primary_lang = _primary_record_language(source)
    if primary_lang is None:
        return
    to_swap: list[tuple[URIRef, Literal]] = []
    for manif in bffi_graph.subjects(RDF.type, V.BFFI.Manifestation):
        if not isinstance(manif, URIRef):
            continue
        for o in bffi_graph.objects(manif, V.SKOS.prefLabel):
            if isinstance(o, Literal) and o.language is None:
                to_swap.append((manif, o))
    for manif, o in to_swap:
        bffi_graph.remove((manif, V.SKOS.prefLabel, o))
        bffi_graph.add((manif, V.SKOS.prefLabel, Literal(str(o), lang=primary_lang)))


def _tag_loc_vocab_labels_with_primary_language(bffi_graph: Graph, source: Graph) -> None:
    """Tag every untagged ``rdfs:label`` literal on a LoC vocabulary
    URI with the source record's primary language BCP-47 code.

    The round-trip converter's ``_loc_label`` lookup uses
    ``lang_pref=("fi", "en")`` so the language tag is the only way it
    can pick the right per-record label when M8 merges multiple
    records' cataloguer-typed labels onto the same shared LoC URI in
    canonical. Without this, Skosify's ``default_language = fi``
    force-tags every untagged literal as @fi — including Swedish
    text from Swedish-source records — and the lookup randomly
    surfaces "ingen medietyp" on a Finnish-source record's 337 $a.
    """
    primary_lang = _primary_record_language(source)
    if primary_lang is None:
        return
    to_swap: list[tuple[URIRef, Literal]] = []
    for s, _p, o in bffi_graph.triples((None, RDFS.label, None)):
        if not (isinstance(s, URIRef) and str(s).startswith(_LOC_VOCAB_URI_PREFIX)):
            continue
        if not (isinstance(o, Literal) and o.language is None):
            continue
        to_swap.append((s, o))
    for s, o in to_swap:
        bffi_graph.remove((s, RDFS.label, o))
        bffi_graph.add((s, RDFS.label, Literal(str(o), lang=primary_lang)))


def post_process(
    bffi_graph: Graph,
    source: Graph,
    *,
    llm_detector: object | None = None,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    audit_log_path: Path | None = None,
    title_lang_audit_log_path: Path | None = None,
    now: datetime | None = None,
) -> Graph:
    """Mutate ``bffi_graph`` in place: tag prefLabels, denormalise Helmet
    identifiers for Skosmos display, optionally extract 245$c
    contributors, bind namespaces.

    ``llm_detector`` enables the M3 title-language cascade;
    ``contrib_extractor`` enables the M3 245$c contributor-extraction
    cascade. Either / both can be ``None`` to keep that stage
    graph-only. ``variants_sidecar_path`` is where the cascade
    appends one row per detected transliteration variant; M8's
    binding pass reads the same file. ``audit_log_path`` is where
    the *contrib* cascade appends one row per fire;
    ``title_lang_audit_log_path`` is the analogous file for the
    *title-language* cascade. Both are consumed by the
    cataloguer-review bundle build to avoid re-running the LLM
    against BIBFRAME / canonical graphs.
    """
    # Pre-tag Manifestation prefLabels with the record's primary
    # language. These are pipeline-synthesised (title + publication
    # statement concatenation) rather than cataloguer-typed, so they
    # shouldn't burn LLM cascade cycles — the underlying title's
    # language is already detected on the bf:Work / bffi:Expression
    # side, and the synthetic concatenation can confuse the detector
    # when the imprint is in a different language than the title.
    # Pre-tagging marks them as already-tagged so the cascade skips.
    _tag_manifestation_pref_labels_with_primary_language(bffi_graph, source)
    candidates = _candidate_languages(source)
    if candidates:
        _retag_pref_labels(
            bffi_graph,
            candidates,
            llm_detector=llm_detector,
            audit_log_path=title_lang_audit_log_path,
        )
    _emit_extracted_contributions(
        bffi_graph,
        source,
        contrib_extractor=contrib_extractor,
        variants_sidecar_path=variants_sidecar_path,
        audit_log_path=audit_log_path,
        now=now,
    )
    # P-52 Phase G.bis — synthesise component-Expression contribution
    # chains from each aggregation component's ``bflc:marcKey`` ``$g``
    # subfield. Drives the round-trip 700 ind2=2 analytical-entry emit
    # path and feeds M9 component-agent reconciliation.
    _enrich_aggregation_components_with_agents(bffi_graph)
    # Collapse marc2bibframe2's double-emitted ProvisionActivity blank
    # nodes (one literal-only, one 008-normalised) into a single node
    # per Manifestation so the round-trip emits one 264 per source
    # 260/264.
    _merge_duplicate_provision_activities(bffi_graph)
    # NOTE: a M3 post-pass that lifted Finnish / Swedish ``$e`` role
    # terms onto a LoC relator URI used to live here
    # (``relator_term_enrichment.enrich_role_uris``). It was removed
    # in the role-redesign because BFFI 1.0.0 designates MTS — not
    # LoC relators — as the value vocabulary for ``bffi:Role`` (see
    # ``bffi-meta:relatedValueVocabulary`` on ``vocab/lkd.rdf``'s
    # ``bffi:Role`` class, pointing at MTS collections m34 / m153 /
    # m491 / m1157). Role-URI enrichment now happens at M10 / Skosify
    # time against MTS; ``canonical.ttl`` carries the cataloguer's
    # bnode-with-``rdfs:label`` form only (source-faithful).
    # ``$4`` round-trip emission was also removed — source MARC
    # essentially never has ``$4`` (10/473 k records per the corpus
    # inventory). See ``docs/bffi_limitations.md``.
    # Language-tag untagged rdfs:label values on LoC vocab URIs with
    # the record's primary language so M8's cross-record propagation
    # (and Skosify's default_language=fi) don't merge a Finnish $a
    # and a Swedish $a into one ambiguous bucket. See helper docstring.
    _tag_loc_vocab_labels_with_primary_language(bffi_graph, source)
    V.bind_canonical_prefixes(bffi_graph)
    return bffi_graph
