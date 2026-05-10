"""Unit tests for the provenance vocabulary, logger, writer, and compaction (M7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from rdflib import Graph, URIRef

from bffi_pipeline.cli import _parse_age_spec
from bffi_pipeline.provenance import logger as P
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.provenance import writer as W

# --- log_software_agent ---------------------------------------------------


def test_software_agent_uri_sanitises_ollama_tags() -> None:
    """Ollama tags use ':' which is reserved in URI path segments."""
    uri = P.model_agent_uri("qwen3:32b-q4_K_M")
    assert ":" not in uri.split("/")[-1]
    assert uri.endswith("agent/qwen3-32b-q4_K_M")


def test_log_software_agent_emits_full_block() -> None:
    g = Graph()
    agent = P.log_software_agent(
        g,
        model_id="qwen3:32b-q4_K_M",
        label="Qwen3 32B Instruct (MLX 4-bit)",
        provider="Alibaba (Qwen team)",
        temperature=0.0,
        seed=42,
    )
    triples = set(g.triples((agent, None, None)))
    predicates = {str(p) for _, p, _ in triples}
    assert str(V.RDF.type) in predicates
    assert str(V.modelId) in predicates
    assert str(V.RDFS.label) in predicates
    assert str(V.provider) in predicates
    assert str(V.temperature) in predicates
    assert str(V.seed) in predicates


# --- log_merge_decision ---------------------------------------------------


def _required_predicates() -> set[URIRef]:
    """Spec § 8 minimum predicate set on every WorkMergeDecision."""
    return {
        V.RDF.type,
        V.PROV.startedAtTime,
        V.PROV.endedAtTime,
        V.PROV.wasAssociatedWith,
        V.PROV.used,
        V.stage,
        V.decision,
        V.confidence,
        V.embeddingSimilarity,
        V.rationale,
        V.promptHash,
        V.rawResponse,
    }


def test_log_merge_decision_emits_all_required_predicates() -> None:
    g = Graph()
    activity = P.log_merge_decision(
        g,
        inputs=["http://example.org/raw/1", "http://example.org/raw/2"],
        decision="same_work",
        confidence=0.91,
        embedding_similarity=0.84,
        rationale="Plenty of detail here, more than twenty characters total.",
        matching_fields=["creator", "original_language"],
        diverging_fields=["preferred_title"],
        prompt_hash="sha256:9a1f7c3e",
        raw_response='{"decision":"same_work"}',
        model_id="qwen3:32b-q4_K_M",
        stage="llm-judge-primary",
    )
    seen = {p for _, p, _ in g.triples((activity, None, None))}
    assert _required_predicates() <= seen


def test_log_merge_decision_is_typed_as_activity_and_workmergedecision() -> None:
    g = Graph()
    activity = P.log_merge_decision(
        g,
        inputs=["http://example.org/raw/1"],
        decision="different_work",
        confidence=0.95,
        embedding_similarity=0.40,
        rationale="Plenty of detail here, more than twenty characters total.",
        matching_fields=[],
        diverging_fields=["creator", "content_type"],
        prompt_hash="sha256:abc",
        raw_response='{"decision":"different_work"}',
        model_id="qwen2.5:72b-instruct-q4_K_M",
        stage="llm-judge-second-opinion",
    )
    types = {str(o) for _, _, o in g.triples((activity, V.RDF.type, None))}
    assert str(V.PROV.Activity) in types
    assert str(V.WorkMergeDecision) in types


def test_same_work_links_canonical_back_to_activity() -> None:
    g = Graph()
    canonical = "http://urn.fi/URN:NBN:fi:bib:work:c0ffee"
    activity = P.log_merge_decision(
        g,
        inputs=["http://example.org/raw/1", "http://example.org/raw/2"],
        decision="same_work",
        confidence=0.91,
        embedding_similarity=0.84,
        rationale="Plenty of detail here, more than twenty characters total.",
        matching_fields=["creator"],
        diverging_fields=[],
        prompt_hash="sha256:abc",
        raw_response="{}",
        model_id="qwen3:32b-q4_K_M",
        stage="llm-judge-primary",
        canonical=canonical,
    )
    assert (URIRef(canonical), V.PROV.wasGeneratedBy, activity) in g
    derived = list(g.objects(URIRef(canonical), V.PROV.wasDerivedFrom))
    assert len(derived) == 2


def test_different_work_does_not_link_canonical() -> None:
    g = Graph()
    P.log_merge_decision(
        g,
        inputs=["http://example.org/raw/1"],
        decision="different_work",
        confidence=0.95,
        embedding_similarity=0.40,
        rationale="Plenty of detail here, more than twenty characters total.",
        matching_fields=[],
        diverging_fields=["creator"],
        prompt_hash="sha256:abc",
        raw_response="{}",
        model_id="qwen3:32b-q4_K_M",
        stage="llm-judge-primary",
        canonical="http://example.org/canonical/x",
    )
    triples = set(
        g.triples((URIRef("http://example.org/canonical/x"), V.PROV.wasGeneratedBy, None))
    )
    assert triples == set()


# --- log_review -----------------------------------------------------------


def test_log_review_chains_via_was_informed_by() -> None:
    g = Graph()
    decision = P.log_merge_decision(
        g,
        inputs=["http://example.org/raw/1"],
        decision="same_work",
        confidence=0.91,
        embedding_similarity=0.84,
        rationale="Plenty of detail here, more than twenty characters total.",
        matching_fields=["creator"],
        diverging_fields=[],
        prompt_hash="sha256:abc",
        raw_response="{}",
        model_id="qwen3:32b-q4_K_M",
        stage="llm-judge-primary",
    )
    review = P.log_review(
        g,
        informed_by=decision,
        reviewer_uri="http://urn.fi/URN:NBN:fi:bib:agent/cataloguer/jdoe",
        decision="confirmed",
        review_note="Verified against KANTO00012345.",
    )
    types = {str(o) for _, _, o in g.triples((review, V.RDF.type, None))}
    assert str(V.HumanReview) in types
    assert (review, V.PROV.wasInformedBy, decision) in g
    assert any(g.triples((review, V.reviewNote, None)))


# --- ProvenanceWriter -----------------------------------------------------


def test_provenance_writer_round_trips_via_turtle(tmp_path: Path) -> None:
    out = tmp_path / "provenance.ttl"
    with W.ProvenanceWriter(out) as writer:
        writer.add_software_agent(
            model_id="qwen3:32b-q4_K_M",
            label="Qwen3 32B Instruct (MLX 4-bit)",
        )
        writer.add_merge_decision(
            inputs=["http://example.org/raw/1"],
            decision="same_work",
            confidence=0.91,
            embedding_similarity=0.84,
            rationale="Plenty of detail here, more than twenty characters total.",
            matching_fields=["creator"],
            diverging_fields=[],
            prompt_hash="sha256:abc",
            raw_response="{}",
            model_id="qwen3:32b-q4_K_M",
            stage="llm-judge-primary",
        )
    assert out.is_file()
    g = Graph()
    g.parse(str(out), format="turtle")
    assert any(g.triples((None, V.RDF.type, V.WorkMergeDecision)))
    assert any(g.triples((None, V.RDF.type, V.PROV.SoftwareAgent)))


def test_provenance_writer_appends_to_existing_file(tmp_path: Path) -> None:
    out = tmp_path / "provenance.ttl"
    with W.ProvenanceWriter(out) as writer:
        writer.add_merge_decision(
            inputs=["http://example.org/raw/1"],
            decision="different_work",
            confidence=0.95,
            embedding_similarity=0.40,
            rationale="Plenty of detail here, more than twenty characters total.",
            matching_fields=[],
            diverging_fields=["creator"],
            prompt_hash="sha256:abc",
            raw_response="{}",
            model_id="qwen3:32b-q4_K_M",
            stage="llm-judge-primary",
        )
    with W.ProvenanceWriter(out) as writer:
        writer.add_merge_decision(
            inputs=["http://example.org/raw/2"],
            decision="same_work",
            confidence=0.91,
            embedding_similarity=0.84,
            rationale="Plenty of detail here, more than twenty characters total.",
            matching_fields=["creator"],
            diverging_fields=[],
            prompt_hash="sha256:abc",
            raw_response="{}",
            model_id="qwen3:32b-q4_K_M",
            stage="llm-judge-primary",
        )
    g = Graph()
    g.parse(str(out), format="turtle")
    activities = list(g.subjects(V.RDF.type, V.WorkMergeDecision))
    assert len(activities) == 2


# --- Meta graph + lastCompactedAt ----------------------------------------


def test_read_last_compacted_at_returns_none_when_missing(tmp_path: Path) -> None:
    assert W.read_last_compacted_at(tmp_path / "provenance-meta.ttl") is None


def test_write_then_read_last_compacted_at(tmp_path: Path) -> None:
    meta = tmp_path / "provenance-meta.ttl"
    moment = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    W.write_last_compacted_at(moment, meta_path=meta)
    got = W.read_last_compacted_at(meta)
    assert got == moment


# --- compact_provenance --------------------------------------------------


def _seed_decision(
    g: Graph,
    started_at: datetime,
    *,
    activity_uri: URIRef | None = None,
) -> URIRef:
    return P.log_merge_decision(
        g,
        inputs=["http://example.org/raw/1"],
        decision="same_work",
        confidence=0.91,
        embedding_similarity=0.84,
        rationale="Plenty of detail here, more than twenty characters total.",
        matching_fields=["creator"],
        diverging_fields=[],
        prompt_hash="sha256:abc",
        raw_response='{"raw":"old"}',
        model_id="qwen3:32b-q4_K_M",
        stage="llm-judge-primary",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=4),
        activity_uri=activity_uri,
    )


def test_compact_strips_old_raw_response_keeps_structured_fields(tmp_path: Path) -> None:
    prov = tmp_path / "provenance.ttl"
    meta = tmp_path / "provenance-meta.ttl"
    now = datetime(2026, 5, 9, tzinfo=UTC)
    old = now - timedelta(days=120)  # past 90-day cutoff
    fresh = now - timedelta(days=10)  # well within cutoff

    g = Graph()
    old_activity = URIRef("http://urn.fi/URN:NBN:fi:bib:merge/old")
    fresh_activity = URIRef("http://urn.fi/URN:NBN:fi:bib:merge/fresh")
    _seed_decision(g, old, activity_uri=old_activity)
    _seed_decision(g, fresh, activity_uri=fresh_activity)
    g.serialize(destination=str(prov), format="turtle")

    removed = W.compact_provenance(
        older_than_days=90,
        provenance_path=prov,
        meta_path=meta,
        now=now,
    )
    assert removed == 1

    after = Graph()
    after.parse(str(prov), format="turtle")
    # The OLD activity has lost its rawResponse but kept its structured fields.
    assert not any(after.triples((old_activity, V.rawResponse, None)))
    assert any(after.triples((old_activity, V.decision, None)))
    assert any(after.triples((old_activity, V.confidence, None)))
    # The FRESH activity is untouched.
    assert any(after.triples((fresh_activity, V.rawResponse, None)))
    # And the meta sentinel was rewritten.
    assert W.read_last_compacted_at(meta) is not None


def test_compact_writes_meta_even_when_zero_removed(tmp_path: Path) -> None:
    prov = tmp_path / "provenance.ttl"
    meta = tmp_path / "provenance-meta.ttl"
    now = datetime(2026, 5, 9, tzinfo=UTC)
    g = Graph()
    _seed_decision(g, now - timedelta(days=10))  # all fresh
    g.serialize(destination=str(prov), format="turtle")

    removed = W.compact_provenance(
        older_than_days=90,
        provenance_path=prov,
        meta_path=meta,
        now=now,
    )
    assert removed == 0
    assert W.read_last_compacted_at(meta) is not None


# --- stale_provenance_warning --------------------------------------------


def test_stale_warning_silent_when_no_provenance_file(tmp_path: Path) -> None:
    msg = W.stale_provenance_warning(
        provenance_path=tmp_path / "provenance.ttl",
        meta_path=tmp_path / "provenance-meta.ttl",
    )
    assert msg is None


def test_stale_warning_fires_when_meta_missing_but_prov_exists(tmp_path: Path) -> None:
    prov = tmp_path / "provenance.ttl"
    prov.write_text("@prefix bffi: <http://example.org/> .\n", encoding="utf-8")
    meta = tmp_path / "provenance-meta.ttl"
    msg = W.stale_provenance_warning(
        provenance_path=prov,
        meta_path=meta,
        now=datetime.now(UTC),
    )
    assert msg is not None
    assert "never been compacted" in msg


def test_stale_warning_fires_when_compaction_old(tmp_path: Path) -> None:
    prov = tmp_path / "provenance.ttl"
    prov.write_text("@prefix bffi: <http://example.org/> .\n", encoding="utf-8")
    meta = tmp_path / "provenance-meta.ttl"
    long_ago = datetime(2026, 1, 1, tzinfo=UTC)
    W.write_last_compacted_at(long_ago, meta_path=meta)
    msg = W.stale_provenance_warning(
        provenance_path=prov,
        meta_path=meta,
        older_than_days=90,
        now=datetime(2026, 5, 9, tzinfo=UTC),
    )
    assert msg is not None
    assert "stale" in msg


def test_stale_warning_silent_when_compaction_recent(tmp_path: Path) -> None:
    prov = tmp_path / "provenance.ttl"
    prov.write_text("@prefix bffi: <http://example.org/> .\n", encoding="utf-8")
    meta = tmp_path / "provenance-meta.ttl"
    yesterday = datetime.now(UTC) - timedelta(days=1)
    W.write_last_compacted_at(yesterday, meta_path=meta)
    assert W.stale_provenance_warning(provenance_path=prov, meta_path=meta) is None


# --- _parse_age_spec via the CLI -----------------------------------------


def test_age_spec_accepts_d_suffix() -> None:
    assert _parse_age_spec("90d") == 90
    assert _parse_age_spec("0d") == 0
    assert _parse_age_spec("30") == 30


def test_age_spec_rejects_garbage() -> None:
    with pytest.raises(typer.BadParameter):
        _parse_age_spec("ninety days")
    with pytest.raises(typer.BadParameter):
        _parse_age_spec("-5d")
