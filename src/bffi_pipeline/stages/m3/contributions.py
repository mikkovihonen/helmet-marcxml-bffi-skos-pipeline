"""M3 MARC 245$c contributor extraction cascade.

Heuristic + optional local-LLM cascade that walks each main
``bf:Work``'s responsibility-statement text and emits a non-primary
``bffi:Contribution`` block per detected contributor that's missing from
MARC 100/700. Transliteration variants of existing 100/700 agents go to
the F2 sidecar (``contrib-variants.jsonl``) for M8 to bind via
``skos:altLabel``.

P-38 Phase B: extracted from m3/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.contrib_variants import (
    ContribVariantClaim,
    append_variant_claims,
)
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.uris import mint_raw_expression_uri, mint_raw_work_uri


def _read_helmet_bib_id(source: Graph, work: URIRef) -> str | None:
    """Walk ``work``'s ``bf:identifiedBy`` chain for the bare Helmet bib ID.

    Returns the ``rdf:value`` literal on the first ``bf:Local`` identifier
    sourced from ``<helmet>`` — the same string M2 records in
    ``helmet-map.jsonl``.
    """
    for ident in source.objects(work, V.BF.identifiedBy):
        if (ident, V.BF.source, V.HELMET_SOURCE_URI) not in source:
            continue
        for value in source.objects(ident, RDF.value):
            if isinstance(value, Literal):
                return str(value)
    return None


def _emit_extracted_contributions(
    bffi_graph: Graph,
    source: Graph,
    *,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Run the heuristic + optional LLM cascade for MARC 245$c extraction.

    Per main bf:Work in ``source``: read the responsibility-statement
    text and existing 100/700 agent labels, gate on the heuristic, and
    when ``contrib_extractor`` is provided escalate to the LLM. Each
    new agent the LLM returns becomes a non-primary
    ``bffi:Contribution`` block on the corresponding bffi:Expression
    (mirroring the existing M3 routing rule that puts non-primary
    contributions on the Expression).

    Transliteration-variant entries (``transliteration_of`` set) are
    *not* emitted as new Contributions — that would propagate the
    cataloguer's typo'd form. Instead, when ``variants_sidecar_path``
    is supplied, each variant claim is appended to the
    ``contrib-variants.jsonl`` sidecar as a
    :class:`bffi_pipeline.contrib_variants.ContribVariantClaim`. M8's
    binding pass later attaches ``skos:altLabel`` on the canonical
    agent so both forms share the same identity downstream.

    Re-runs against the same source produce byte-identical bffi_graph
    output: blank nodes use SHA-1 of (work_uri, agent_name,
    relator_code) so deterministic.
    """
    from bffi_pipeline.contrib_extract import (
        ExtractionInputs,
        extract_contributions,
        gather_inputs,
    )
    from bffi_pipeline.contrib_extract_llm import (
        DEFAULT_CONTRIB_MODEL,
        RELATOR_URI_PREFIX,
        ContribExtractor,
        contrib_extract_prompt_hash,
    )

    typed_extractor = cast("ContribExtractor | None", contrib_extractor)
    timestamp = (now or datetime.now(UTC)).isoformat()
    prompt_hash = contrib_extract_prompt_hash() if variants_sidecar_path is not None else ""
    extractor_model = (
        getattr(typed_extractor, "model_name", None) or DEFAULT_CONTRIB_MODEL
        if typed_extractor is not None
        else DEFAULT_CONTRIB_MODEL
    )
    pending_claims: list[ContribVariantClaim] = []

    contained: set[URIRef] = {
        o
        for _, _, o in source.triples((None, V.BF.associatedResource, None))
        if isinstance(o, URIRef)
    }
    for work in source.subjects(RDF.type, V.BF.Work):
        if not isinstance(work, URIRef) or work in contained:
            continue
        inputs: ExtractionInputs | None = gather_inputs(source, work)
        if inputs is None:
            continue
        decision = extract_contributions(inputs, extractor=typed_extractor)
        if decision is None or not decision.contributions:
            continue

        expr_uri = URIRef(mint_raw_expression_uri(str(work)))
        bib_id = _read_helmet_bib_id(source, work)
        for cand in decision.contributions:
            if cand.transliteration_of is not None:
                # Variant pointer — record the binding decision in the
                # sidecar so M8 can attach it as a skos:altLabel on
                # the matching canonical agent. Skip Contribution
                # emission either way to avoid propagating the typo'd
                # form as a new agent.
                if variants_sidecar_path is not None and bib_id is not None:
                    pending_claims.append(
                        ContribVariantClaim(
                            helmet_bib_id=bib_id,
                            # Mint the bffi:Work URI rather than passing
                            # the source bf:Work URI: M8's binding pass
                            # joins the sidecar against canonical-map
                            # entries whose raw_work_uris are the bffi
                            # form. Sending the source URI here would
                            # produce a phantom-pointer mismatch.
                            raw_work_uri=mint_raw_work_uri(str(work)),
                            variant_label=cand.name,
                            canonical_label=cand.transliteration_of,
                            relator_code_hint=cand.relator_code,
                            role_text_hint=cand.role_text,
                            rationale=decision.rationale,
                            prompt_hash=prompt_hash,
                            model_id=extractor_model,
                            decided_at=timestamp,
                        )
                    )
                continue
            if cand.relator_code is None:
                continue
            seed = f"{expr_uri}|{cand.name}|{cand.relator_code}"
            digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
            contrib_node = BNode(f"contrib{digest}")
            agent_node = BNode(f"agent{digest}")
            role_uri = URIRef(RELATOR_URI_PREFIX + cand.relator_code)
            bffi_graph.add((expr_uri, V.BFFI.contribution, contrib_node))
            bffi_graph.add((contrib_node, RDF.type, V.BFFI.Contribution))
            bffi_graph.add((contrib_node, V.BFFI.agent, agent_node))
            bffi_graph.add((contrib_node, V.BF.role, role_uri))
            bffi_graph.add((agent_node, RDF.type, V.BFFI.Agent))
            bffi_graph.add((agent_node, RDFS.label, Literal(cand.name)))

    if pending_claims and variants_sidecar_path is not None:
        append_variant_claims(variants_sidecar_path, pending_claims)
