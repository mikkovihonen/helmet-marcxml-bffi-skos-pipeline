"""Provenance writers for spec § 8.

Two public entry points:

- :func:`log_merge_decision` mints a ULID-keyed
  ``bffi-prov:WorkMergeDecision`` Activity for a single LLM-judge call
  (primary or second-opinion). Every cascade step in M6's
  :class:`~bffi_pipeline.stages.m6.JudgeOutcome` becomes one such
  Activity, so the ``bffi-prov:stage`` literal differentiates 32 B
  primary decisions from 72 B cascade re-runs.

- :func:`log_review` mints a ``bffi-prov:HumanReview`` Activity chained
  onto the original decision via ``prov:wasInformedBy``. Used when a
  cataloguer confirms or overrides an earlier merge decision.

A small :func:`log_software_agent` helper emits the per-model
``prov:SoftwareAgent`` block once per ``model_id`` so a graph can carry
every distinct agent referenced by ``prov:wasAssociatedWith``.

The module is pure data-handling: it adds triples to an
``rdflib.Graph`` (the named graph the caller manages) and returns the
minted Activity URI. Compaction, named-graph routing, and stale-
warning logic live in :mod:`~bffi_pipeline.provenance.writer` to keep
this layer small and testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from rdflib import Graph, Literal, URIRef
from ulid import ULID

from bffi_pipeline.provenance import vocab as V


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_agent_segment(model_id: str) -> str:
    """Sanitise ``model_id`` for use in an URI segment.

    Ollama tags use ``:`` (``"qwen3:32b-q4_K_M"``) which is reserved
    in URI path segments per RFC 3986; the spec § 8 helper replaces it with
    ``-`` and we follow.
    """
    return model_id.replace(":", "-").replace("/", "_")


def model_agent_uri(model_id: str) -> URIRef:
    """``bib:agent/<sanitised model_id>`` URI used by ``prov:wasAssociatedWith``."""
    return V.BIB[f"agent/{_safe_agent_segment(model_id)}"]


def log_software_agent(
    g: Graph,
    *,
    model_id: str,
    label: str | None = None,
    provider: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
) -> URIRef:
    """Emit the ``prov:SoftwareAgent`` block for ``model_id`` once.

    Idempotent — calling twice on the same graph adds the same triples
    a second time but rdflib treats them as set semantics; downstream
    consumers see one block.
    """
    agent = model_agent_uri(model_id)
    g.add((agent, V.RDF.type, V.PROV.SoftwareAgent))
    g.add((agent, V.modelId, Literal(model_id)))
    if label is not None:
        g.add((agent, V.RDFS.label, Literal(label)))
    if provider is not None:
        g.add((agent, V.provider, Literal(provider)))
    if temperature is not None:
        g.add((agent, V.temperature, Literal(temperature, datatype=V.XSD.decimal)))
    if seed is not None:
        g.add((agent, V.seed, Literal(seed, datatype=V.XSD.integer)))
    return agent


def log_merge_decision(
    g: Graph,
    *,
    inputs: Iterable[str],
    decision: str,
    confidence: float,
    embedding_similarity: float,
    rationale: str,
    matching_fields: Iterable[str],
    diverging_fields: Iterable[str],
    prompt_hash: str,
    raw_response: str,
    model_id: str,
    stage: str,
    cache_hit: bool = False,
    canonical: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    prompt_source: str | None = None,
    activity_uri: URIRef | None = None,
) -> URIRef:
    """Mint a ``bffi-prov:WorkMergeDecision`` Activity per spec § 8.

    Returns the activity URI so callers (the M6 batch driver) can chain
    further triples — e.g. point a canonical Work at it via
    ``prov:wasGeneratedBy`` once M8 mints the canonical URI.
    """
    activity = activity_uri or V.BIB[f"merge/{ULID()}"]
    started = started_at or _now()
    ended = ended_at or started

    g.add((activity, V.RDF.type, V.PROV.Activity))
    g.add((activity, V.RDF.type, V.WorkMergeDecision))
    g.add((activity, V.PROV.startedAtTime, Literal(started.isoformat(), datatype=V.XSD.dateTime)))
    g.add((activity, V.PROV.endedAtTime, Literal(ended.isoformat(), datatype=V.XSD.dateTime)))
    g.add((activity, V.PROV.wasAssociatedWith, model_agent_uri(model_id)))
    for src in inputs:
        g.add((activity, V.PROV.used, URIRef(src)))

    g.add((activity, V.stage, Literal(stage)))
    g.add((activity, V.decision, Literal(decision)))
    g.add((activity, V.confidence, Literal(confidence, datatype=V.XSD.decimal)))
    g.add(
        (
            activity,
            V.embeddingSimilarity,
            Literal(embedding_similarity, datatype=V.XSD.decimal),
        )
    )
    g.add((activity, V.rationale, Literal(rationale)))
    for value in matching_fields:
        g.add((activity, V.matchingField, Literal(value)))
    for value in diverging_fields:
        g.add((activity, V.divergingField, Literal(value)))
    g.add((activity, V.promptHash, Literal(prompt_hash)))
    if prompt_source is not None:
        g.add((activity, V.promptSource, Literal(prompt_source)))
    g.add((activity, V.rawResponse, Literal(raw_response)))
    g.add((activity, V.cacheHit, Literal(cache_hit, datatype=V.XSD.boolean)))

    if decision == "same_work" and canonical is not None:
        canonical_uri = URIRef(canonical)
        g.add((canonical_uri, V.PROV.wasGeneratedBy, activity))
        for src in inputs:
            g.add((canonical_uri, V.PROV.wasDerivedFrom, URIRef(src)))

    return activity


def log_review(
    g: Graph,
    *,
    informed_by: URIRef,
    reviewer_uri: str,
    decision: str,
    review_note: str,
    at_time: datetime | None = None,
    activity_uri: URIRef | None = None,
) -> URIRef:
    """Mint a ``bffi-prov:HumanReview`` Activity chained onto ``informed_by``.

    ``decision`` ∈ ``{"confirmed", "overridden"}`` per spec § 8. The
    chain is via ``prov:wasInformedBy`` rather than a custom predicate
    so generic PROV-O tooling can walk it.
    """
    activity = activity_uri or V.BIB[f"review/{ULID()}"]
    moment = at_time or _now()
    g.add((activity, V.RDF.type, V.PROV.Activity))
    g.add((activity, V.RDF.type, V.HumanReview))
    g.add((activity, V.PROV.wasInformedBy, informed_by))
    g.add((activity, V.PROV.wasAssociatedWith, URIRef(reviewer_uri)))
    g.add((activity, V.PROV.atTime, Literal(moment.isoformat(), datatype=V.XSD.dateTime)))
    g.add((activity, V.decision, Literal(decision)))
    g.add((activity, V.reviewNote, Literal(review_note)))
    return activity


def log_reconciliation(
    g: Graph,
    *,
    work_uri: str,
    input_literal: str,
    source_vocabulary: str,
    stage: str,
    chosen_authority_uri: str | None,
    candidates: Iterable[tuple[str, float]],
    confidence: float,
    rationale: str,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    activity_uri: URIRef | None = None,
    was_influenced_by: URIRef | None = None,
) -> URIRef:
    """Mint a ``bffi-prov:Reconciliation`` Activity per spec § 6.

    The four ``stage`` values (``"reconciliation-lexical"``,
    ``"reconciliation-llm"``, ``"reconciliation-fallback"``,
    ``"reconciliation-no-candidate"``) feed downstream filters; one
    ``Activity`` is logged per reconciliation attempt regardless of
    outcome so the negative cases (no candidate, fallback, …) stay
    auditable.

    ``candidates`` is an iterable of ``(authority_uri, lexical_similarity)``
    pairs covering the full top-k pool the decision was drawn from.

    ``was_influenced_by`` records the URI of an earlier Activity whose
    verdict this Activity replays — used by P-10 Phase B to mark
    picker-cache hits: the new Activity carries
    ``prov:wasInfluencedBy <cached-activity-uri>`` so the audit trail
    distinguishes "fresh LLM verdict" from "reused cached verdict".
    """
    activity = activity_uri or V.BIB[f"reconcile/{ULID()}"]
    started = started_at or _now()
    ended = ended_at or started

    g.add((activity, V.RDF.type, V.PROV.Activity))
    g.add((activity, V.RDF.type, V.Reconciliation))
    g.add((activity, V.PROV.startedAtTime, Literal(started.isoformat(), datatype=V.XSD.dateTime)))
    g.add((activity, V.PROV.endedAtTime, Literal(ended.isoformat(), datatype=V.XSD.dateTime)))
    g.add((activity, V.PROV.used, URIRef(work_uri)))
    g.add((activity, V.stage, Literal(stage)))
    g.add((activity, V.inputLiteral, Literal(input_literal)))
    g.add((activity, V.sourceVocabulary, Literal(source_vocabulary)))
    g.add((activity, V.confidence, Literal(confidence, datatype=V.XSD.decimal)))
    g.add((activity, V.rationale, Literal(rationale)))
    if chosen_authority_uri is not None:
        g.add((activity, V.chosenAuthorityUri, URIRef(chosen_authority_uri)))
    if was_influenced_by is not None:
        g.add((activity, V.PROV.wasInfluencedBy, was_influenced_by))
    for cand_uri, lex in candidates:
        g.add((activity, V.candidateAuthorityUri, URIRef(cand_uri)))
        g.add(
            (
                URIRef(cand_uri),
                V.lexicalSimilarity,
                Literal(lex, datatype=V.XSD.decimal),
            )
        )
    return activity


__all__ = [
    "log_merge_decision",
    "log_reconciliation",
    "log_review",
    "log_software_agent",
    "model_agent_uri",
]
