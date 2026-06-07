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

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m3.contributions import _emit_extracted_contributions
from bffi_pipeline.stages.m3.language_detect import (
    _LANG_3_TO_2,
    _candidate_languages,
    _retag_pref_labels,
)
from bffi_pipeline.stages.m3.relator_term_enrichment import enrich_role_uris

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
    # Resolve Finnish / Swedish ``$e`` role terms on every bf:role
    # blank node to a LoC relator URI when the curated mapping
    # matches. Sibling URI lives next to the original
    # blank-node-with-rdfs:label so round-trip MARC keeps the
    # cataloguer's original ``$e`` and gains a ``$4`` code.
    enrich_role_uris(bffi_graph)
    # Language-tag untagged rdfs:label values on LoC vocab URIs with
    # the record's primary language so M8's cross-record propagation
    # (and Skosify's default_language=fi) don't merge a Finnish $a
    # and a Swedish $a into one ambiguous bucket. See helper docstring.
    _tag_loc_vocab_labels_with_primary_language(bffi_graph, source)
    V.bind_canonical_prefixes(bffi_graph)
    return bffi_graph
