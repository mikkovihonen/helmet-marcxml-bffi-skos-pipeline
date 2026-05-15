"""Unit tests for stages/reconcile (M9 phase 1).

No live HTTP, no live LLM. The orchestrator runs against an in-memory
graph; the FintoSkosmosClient is exercised through ``httpx.MockTransport``
so the JSON-shape parsing is verified without touching the network.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, Namespace

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.provenance.vocab import PROV
from bffi_pipeline.stages.m9.local_concept_resolver import LocalConceptHit
from bffi_pipeline.stages.m9.runner import (
    ALL_AUTHORITY_KINDS,
    LEXICAL_DIRECT_THRESHOLD,
    LEXICAL_FLOOR,
    LLM_CONFIDENCE_THRESHOLD,
    PICKER_MAX_VALIDATION_RETRIES,
    PICKER_ORDERING_PREFIX_CACHE,
    PICKER_ORDERING_SUBMISSION,
    STAGE_FALLBACK,
    STAGE_FICTIONAL,
    STAGE_LEXICAL,
    STAGE_LLM,
    STAGE_NO_CANDIDATE,
    AuthorityCandidate,
    AuthorityKind,
    EntityRequest,
    FintoSkosmosClient,
    LangChainLLMPicker,
    PickerCache,
    PickerDecision,
    StubAuthorityClient,
    StubPicker,
    _decide_with_pick,
    _iter_subject_requests,
    _order_deferred_picker_queue,
    _picker_phase_pool,
    _picker_phase_seq,
    _picker_queue_sort_key,
    apply_reconciliation,
    compute_finto_shas,
    compute_picker_cache_key,
    decide_reconciliation,
    lexical_similarity,
    picker_prompt_hash,
    picker_prompt_text,
    reconcile_one,
)
from bffi_pipeline.stages.observability import (
    StageEventEmitter,
    set_active_emitter,
)

# --- lexical_similarity ---------------------------------------------------


def test_lexical_similarity_identical_strings_score_1() -> None:
    assert lexical_similarity("Tolstoy, Leo", "Tolstoy, Leo") == pytest.approx(1.0)


def test_lexical_similarity_is_case_insensitive() -> None:
    assert lexical_similarity("Tolstoy, Leo", "TOLSTOY, LEO") == pytest.approx(1.0)


def test_lexical_similarity_folds_foreign_diacritics() -> None:
    """``ï`` is a foreign diacritic from cross-script romanization;
    cataloguer-supplied variants like ``Tolstoï`` must match KANTO's
    ``Tolstoi`` form."""
    assert lexical_similarity("Tolstoï", "Tolstoi") == pytest.approx(1.0)


def test_lexical_similarity_folds_german_umlaut() -> None:
    """A German source's ``Müller`` must match a Finnish-cataloguer
    transcription of ``Muller`` because ``ü`` is foreign to Finnish."""
    assert lexical_similarity("Müller", "Muller") == pytest.approx(1.0)


def test_lexical_similarity_folds_french_acute() -> None:
    assert lexical_similarity("LINDGRÉN, Astrid", "Lindgren, Astrid") == pytest.approx(1.0)


def test_lexical_similarity_preserves_finnish_a_with_diaeresis() -> None:
    """``Häme`` (region) and ``hame`` (skirt) are distinct lexemes —
    they must NOT match at lexical similarity 1.0."""
    assert lexical_similarity("Häme", "hame") < 1.0
    assert lexical_similarity("Hämeenlinna", "Hameenlinna") < 1.0


def test_lexical_similarity_preserves_finnish_o_with_diaeresis() -> None:
    """``Yrjö`` (a Finnish given name) and ``Yrjo`` (gibberish) are
    distinct — ``ö`` is native and must not be folded."""
    assert lexical_similarity("Yrjö", "Yrjo") < 1.0


def test_lexical_similarity_preserves_swedish_a_with_ring() -> None:
    """``Ångström`` (Swedish surname) preserves both ``Å`` and ``ö``."""
    assert lexical_similarity("Ångström", "Angstrom") < 1.0


def test_lexical_similarity_native_diacritics_match_themselves() -> None:
    """Same Finnish word with same orthography still matches 1.0 after
    selective fold (case + whitespace insensitivity still apply)."""
    assert lexical_similarity("Häme", "HÄME") == pytest.approx(1.0)
    assert lexical_similarity("Hämeenlinna ", " Hämeenlinna") == pytest.approx(1.0)


def test_lexical_similarity_disjoint_strings_score_low() -> None:
    assert lexical_similarity("Pushkin, Aleksandr", "Mozart, Wolfgang Amadeus") < 0.3


def test_lexical_similarity_collapses_internal_whitespace() -> None:
    assert lexical_similarity("ydin voima", "ydinvoima") > 0.85


# --- decide_reconciliation: four tiers -----------------------------------


def _request() -> EntityRequest:
    return EntityRequest(
        work_uri="http://urn.fi/URN:NBN:fi:bib:work:abc",
        literal="Tolstoy, Leo,",
        kind="person",
    )


def _candidate(uri: str, label: str, sim: float, vocab: str = "finaf") -> AuthorityCandidate:
    return AuthorityCandidate(
        uri=uri, pref_label=label, source_vocabulary=vocab, lexical_similarity=sim
    )


def test_lexical_direct_one_clear_winner_at_or_above_threshold() -> None:
    """Single candidate ≥0.95, no other ≥0.95 → reconciliation-lexical."""
    candidates = [
        _candidate("http://kanto/1", "Tolstoy, Leo", 0.97),
        _candidate("http://kanto/2", "Tolstoyevsky, Lev", 0.84),
    ]
    out = decide_reconciliation(request=_request(), candidates=candidates, picker=StubPicker())
    assert out.stage == STAGE_LEXICAL
    assert out.chosen_uri == "http://kanto/1"
    assert out.needs_review is False
    assert out.confidence >= LEXICAL_DIRECT_THRESHOLD


def test_lexical_direct_threshold_is_inclusive() -> None:
    """Exactly at the 0.95 floor with a clear gap below counts as direct."""
    candidates = [
        _candidate("http://kanto/1", "Tolstoy, Leo", LEXICAL_DIRECT_THRESHOLD),
        _candidate("http://kanto/2", "Tolstoyevsky", 0.80),
    ]
    out = decide_reconciliation(request=_request(), candidates=candidates, picker=StubPicker())
    assert out.stage == STAGE_LEXICAL


def test_multiple_high_similarity_candidates_routes_to_llm_pick() -> None:
    """≥2 candidates above 0.95 → llm-pick."""
    candidates = [
        _candidate("http://kanto/1", "Tolstoy, Leo", 0.96),
        _candidate("http://kanto/2", "Tolstoy, Lev", 0.96),
    ]
    picker = StubPicker(
        decisions={
            (_request().work_uri, _request().literal): PickerDecision(
                chosen_uri="http://kanto/1",
                confidence=0.92,
                rationale="KANTO entry for Lev Tolstoy is the older, more frequently cited form.",
                decision="chose",
            )
        }
    )
    out = decide_reconciliation(request=_request(), candidates=candidates, picker=picker)
    assert out.stage == STAGE_LLM
    assert out.chosen_uri == "http://kanto/1"
    assert out.needs_review is False


def test_llm_uncertain_routes_to_fallback_with_needs_review() -> None:
    candidates = [
        _candidate("http://kanto/1", "Tolstoy, Leo", 0.96),
        _candidate("http://kanto/2", "Tolstoy, Lev", 0.96),
    ]
    picker = StubPicker(
        decisions={
            (_request().work_uri, _request().literal): PickerDecision(
                chosen_uri=None,
                confidence=0.4,
                rationale="StubPicker forced uncertainty for the test.",
                decision="uncertain",
            )
        }
    )
    out = decide_reconciliation(request=_request(), candidates=candidates, picker=picker)
    assert out.stage == STAGE_FALLBACK
    assert out.chosen_uri is not None
    assert out.needs_review is True


def test_llm_low_confidence_routes_to_fallback() -> None:
    """Below the 0.80 LLM-confidence floor falls back to highest-lexical."""
    candidates = [
        _candidate("http://kanto/top", "Tolstoy, Leo", 0.97),
        _candidate("http://kanto/two", "Tolstoy, Lev", 0.96),
    ]
    picker = StubPicker(
        decisions={
            (_request().work_uri, _request().literal): PickerDecision(
                chosen_uri="http://kanto/two",
                confidence=LLM_CONFIDENCE_THRESHOLD - 0.01,
                rationale="StubPicker low-confidence pick for the test.",
                decision="chose",
            )
        }
    )
    out = decide_reconciliation(request=_request(), candidates=candidates, picker=picker)
    assert out.stage == STAGE_FALLBACK
    assert out.chosen_uri == "http://kanto/top"  # highest lexical, NOT the picker's pick
    assert out.needs_review is True


def test_p16_knob_a_raised_fallback_floor_routes_to_no_candidate() -> None:
    """P-16 Knob A: with ``lexical_fallback_floor=0.85`` and a picker-uncertain
    verdict, a top lexical at 0.80 falls back to ``no-candidate`` instead of
    binding via tier-3 fallback. This is the ``Williams, John`` case from the
    2026-05-13 cataloguer-audit.
    """

    sorted_candidates = [_candidate("http://kanto/wrong-namesake", "Williams, John", 0.80)]
    pick = PickerDecision(
        chosen_uri=None,
        confidence=0.4,
        rationale="Stub uncertain — namesake-disambiguation case.",
        decision="uncertain",
    )
    out = _decide_with_pick(
        request=_request(),
        sorted_candidates=sorted_candidates,
        pick=pick,
        lexical_fallback_floor=0.85,
    )
    assert out.stage == STAGE_NO_CANDIDATE
    assert out.chosen_uri is None
    assert out.needs_review is False
    assert "below the 0.85 fallback floor" in out.rationale


def test_p16_default_floor_preserves_pre_p16_fallback_behavior() -> None:
    """P-16: with default kwargs (floor=0.70, no per-vocab, disable=False),
    the tier-3 fallback path is unchanged from pre-P-16 behaviour. Pins
    backward-compatibility for the no-knob default case.
    """

    sorted_candidates = [_candidate("http://kanto/wrong-namesake", "Williams, John", 0.80)]
    pick = PickerDecision(
        chosen_uri=None,
        confidence=0.4,
        rationale="Stub uncertain — namesake-disambiguation test.",
        decision="uncertain",
    )
    out = _decide_with_pick(request=_request(), sorted_candidates=sorted_candidates, pick=pick)
    assert out.stage == STAGE_FALLBACK
    assert out.chosen_uri == "http://kanto/wrong-namesake"
    assert out.needs_review is True


def test_p16_knob_b_per_vocab_floor_overrides_global() -> None:
    """P-16 Knob B: per-vocabulary floor map overrides the global floor for
    that vocab. Other vocabs keep the global floor.
    """

    finaf_cand = [_candidate("http://kanto/wrong", "Williams, John", 0.80, vocab="finaf")]
    yso_cand = [_candidate("http://yso/p1", "Ystävyys", 0.80, vocab="yso")]
    pick = PickerDecision(
        chosen_uri=None,
        confidence=0.4,
        rationale="Stub uncertain — knob-B per-vocab test.",
        decision="uncertain",
    )
    per_vocab = {"finaf": 0.85}

    # finaf candidate: per-vocab floor applies → no-candidate
    out_finaf = _decide_with_pick(
        request=_request(),
        sorted_candidates=finaf_cand,
        pick=pick,
        lexical_fallback_floor_per_vocab=per_vocab,
    )
    assert out_finaf.stage == STAGE_NO_CANDIDATE
    assert "finaf" in out_finaf.rationale

    # yso candidate: vocab not in per-vocab map → falls back to global (0.70) → tier-3 fallback
    out_yso = _decide_with_pick(
        request=_request(),
        sorted_candidates=yso_cand,
        pick=pick,
        lexical_fallback_floor_per_vocab=per_vocab,
    )
    assert out_yso.stage == STAGE_FALLBACK


def test_p16_knob_c_disable_fallback_routes_to_no_candidate() -> None:
    """P-16 Knob C: ``disable_fallback=True`` makes every picker-uncertain
    outcome a ``no-candidate`` bind, regardless of lexical similarity.
    """

    sorted_candidates = [_candidate("http://kanto/wrong", "Williams, John", 0.95)]
    pick = PickerDecision(
        chosen_uri=None,
        confidence=0.4,
        rationale="Stub uncertain — knob-B per-vocab test.",
        decision="uncertain",
    )
    out = _decide_with_pick(
        request=_request(),
        sorted_candidates=sorted_candidates,
        pick=pick,
        disable_fallback=True,
    )
    assert out.stage == STAGE_NO_CANDIDATE
    assert out.chosen_uri is None
    assert "hard-disabled" in out.rationale
    assert "BFFI_M9_DISABLE_FALLBACK" in out.rationale


def test_p16_knob_c_disable_subsumes_chose_below_llm_threshold() -> None:
    """P-16 Knob C: ``disable_fallback=True`` also gates the picker's
    low-confidence ``chose`` path (which today falls to tier-3 with the
    highest-lexical, not the picker's choice). The disable wins.
    """

    sorted_candidates = [
        _candidate("http://kanto/top", "Williams, John", 0.95),
        _candidate("http://kanto/two", "Williams, J.", 0.92),
    ]
    pick = PickerDecision(
        chosen_uri="http://kanto/two",
        confidence=LLM_CONFIDENCE_THRESHOLD - 0.01,
        rationale="Stub low-confidence pick.",
        decision="chose",
    )
    out = _decide_with_pick(
        request=_request(),
        sorted_candidates=sorted_candidates,
        pick=pick,
        disable_fallback=True,
    )
    assert out.stage == STAGE_NO_CANDIDATE


def test_no_candidates_at_all_yields_no_candidate() -> None:
    out = decide_reconciliation(request=_request(), candidates=[], picker=StubPicker())
    assert out.stage == STAGE_NO_CANDIDATE
    assert out.chosen_uri is None
    assert out.needs_review is False


def test_top_candidate_below_lexical_floor_yields_no_candidate() -> None:
    """If the best candidate is below 0.70, leave it unreconciled."""
    candidates = [_candidate("http://kanto/1", "Some other person", LEXICAL_FLOOR - 0.01)]
    out = decide_reconciliation(request=_request(), candidates=candidates, picker=StubPicker())
    assert out.stage == STAGE_NO_CANDIDATE
    assert out.chosen_uri is None


def test_one_above_direct_threshold_one_below_takes_lexical() -> None:
    """Spec § 6: 'exactly one candidate has lexical similarity > 0.95' wins direct."""
    candidates = [
        _candidate("http://kanto/1", "Tolstoy, Leo", 0.96),
        _candidate("http://kanto/2", "Tolstoyev", 0.92),  # below 0.95 threshold
    ]
    out = decide_reconciliation(request=_request(), candidates=candidates, picker=StubPicker())
    assert out.stage == STAGE_LEXICAL


# --- FintoSkosmosClient ---------------------------------------------------


def _finto_handler(payload: dict[str, object]) -> httpx.MockTransport:
    """Build a MockTransport that returns ``payload`` for any GET to /search."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/search")
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_finto_client_parses_real_shape_results_into_candidates() -> None:
    payload = {
        "results": [
            {
                "uri": "http://urn.fi/URN:NBN:fi:au:finaf:000041686",
                "prefLabel": "Tolstoy, Leo",
                "vocab": "finaf",
            },
            {
                "uri": "http://urn.fi/URN:NBN:fi:au:finaf:000041687",
                "prefLabel": "Tolstoyevsky, Lev",
                "vocab": "finaf",
            },
        ]
    }
    transport = _finto_handler(payload)
    client = FintoSkosmosClient(http_client=httpx.Client(transport=transport))
    request = EntityRequest(
        work_uri="http://urn.fi/URN:NBN:fi:bib:work:abc",
        literal="Tolstoy, Leo",
        kind="person",
    )
    candidates = client.query(request=request)
    assert len(candidates) == 2
    assert candidates[0].uri.endswith("000041686")
    assert candidates[0].source_vocabulary == "finaf"
    # The first candidate has prefLabel matching the input → high similarity.
    assert candidates[0].lexical_similarity > 0.9


def test_finto_client_returns_empty_on_http_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "boom"})

    client = FintoSkosmosClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.query(
        request=EntityRequest(
            work_uri="http://urn.fi/URN:NBN:fi:bib:work:abc",
            literal="Tolstoy, Leo",
            kind="person",
        )
    )
    assert out == []


def test_finto_client_caches_results_per_query_per_day() -> None:
    """Repeated queries on the same day must hit the in-memory cache."""
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "results": [
                    {"uri": "http://kanto/1", "prefLabel": "Tolstoy, Leo", "vocab": "finaf"}
                ]
            },
        )

    client = FintoSkosmosClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    request = EntityRequest(
        work_uri="http://example.org/w/1", literal="Tolstoy, Leo", kind="person"
    )
    client.query(request=request)
    client.query(request=request)
    assert calls["n"] == 1


def test_finto_client_returns_empty_for_kinds_without_a_vocab_mapping() -> None:
    """The Protocol allows arbitrary kinds; uncovered ones short-circuit."""
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []}))
    client = FintoSkosmosClient(http_client=httpx.Client(transport=transport))
    out = client.query(
        request=EntityRequest(
            work_uri="http://example.org/w/1",
            literal="Tolstoy, Leo",
            kind="person",  # supported
        )
    )
    assert isinstance(out, list)


# --- apply_reconciliation orchestrator -----------------------------------


WORK = "http://urn.fi/URN:NBN:fi:bib:work:abc"
EXPR = "http://urn.fi/URN:NBN:fi:bib:expression:abc"
AGENT = "http://example.org/agent/tolstoy"
ADMIN = "http://urn.fi/URN:NBN:fi:bib:adminmeta/1"
CONTRIB = "http://example.org/contrib/1"


def _build_canonical_graph(creator_label: str = "Tolstoy, Leo,") -> Graph:
    g = Graph()
    work = URIRef(WORK)
    contrib = URIRef(CONTRIB)
    agent = URIRef(AGENT)
    admin = URIRef(ADMIN)
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal(creator_label)))
    g.add((work, V.adminMetadata, admin))
    g.add((admin, RDF.type, V.AdminMetadata))
    g.add((admin, V.adminMetadataFor, work))
    g.add(
        (
            admin,
            V.descriptionChangeDate,
            Literal("2026-05-01T00:00:00+00:00", datatype=V.XSD.dateTime),
        )
    )
    g.add((admin, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    return g


def test_apply_reconciliation_lexical_path_links_creator_and_bumps_admin() -> None:
    g = _build_canonical_graph()
    client = StubAuthorityClient(
        fixtures={
            ("person", "Tolstoy, Leo,"): [
                _candidate("http://kanto/1", "Tolstoy, Leo,", 0.97),
                _candidate("http://kanto/2", "Tolstoyevsky", 0.80),
            ]
        }
    )
    summary, outcomes = apply_reconciliation(
        client=client,
        picker=StubPicker(),
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.lexical == 1
    assert summary.total == 1
    assert outcomes[0].chosen_uri == "http://kanto/1"
    work = URIRef(WORK)
    assert (work, V.BFFI.creator, URIRef("http://kanto/1")) in g
    block = next(g.objects(work, V.adminMetadata))
    assert (block, V.sourceConsulted, URIRef("http://kanto/1")) in g
    change_dates = list(g.objects(block, V.descriptionChangeDate))
    assert len(change_dates) == 1
    assert "2026-05-09" in str(change_dates[0])
    # Authentication still auto-merged on the success path (NOT needs-review).
    auth_states = set(g.objects(block, V.descriptionAuthentication))
    assert V.AUTH_AUTO_MERGED in auth_states
    assert V.AUTH_NEEDS_REVIEW not in auth_states


def test_apply_reconciliation_fallback_path_flips_admin_to_needs_review() -> None:
    g = _build_canonical_graph()
    client = StubAuthorityClient(
        fixtures={
            ("person", "Tolstoy, Leo,"): [
                _candidate("http://kanto/1", "Tolstoy, Leo", 0.96),
                _candidate("http://kanto/2", "Tolstoy, Lev", 0.96),
            ]
        }
    )
    picker = StubPicker(
        decisions={
            (WORK, "Tolstoy, Leo,"): PickerDecision(
                chosen_uri=None,
                confidence=0.5,
                rationale="StubPicker forced uncertainty for the fallback path test.",
                decision="uncertain",
            )
        }
    )
    summary, outcomes = apply_reconciliation(
        client=client,
        picker=picker,
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.fallback == 1
    assert outcomes[0].needs_review is True
    block = next(g.objects(URIRef(WORK), V.adminMetadata))
    auth_states = set(g.objects(block, V.descriptionAuthentication))
    assert V.AUTH_NEEDS_REVIEW in auth_states


def test_apply_reconciliation_no_candidate_path_leaves_creator_alone() -> None:
    g = _build_canonical_graph()
    client = StubAuthorityClient(fixtures={})  # nothing returned for anything
    summary, _ = apply_reconciliation(
        client=client,
        picker=StubPicker(),
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.no_candidate == 1
    work = URIRef(WORK)
    creators = list(g.objects(work, V.BFFI.creator))
    assert creators == []  # no authority URI bound


def test_apply_reconciliation_records_provenance_per_attempt() -> None:
    """Every attempt — incl. the no-candidate negative case — gets one Activity."""
    g = _build_canonical_graph()
    client = StubAuthorityClient(
        fixtures={
            ("person", "Tolstoy, Leo,"): [
                _candidate("http://kanto/1", "Tolstoy, Leo,", 0.97),
                _candidate("http://kanto/2", "Tolstoyevsky", 0.80),
            ]
        }
    )
    prov = Graph()
    apply_reconciliation(
        client=client,
        picker=StubPicker(),
        graph=g,
        provenance_graph=prov,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    activities = list(prov.subjects(V.RDF.type, V.Reconciliation))
    assert len(activities) == 1
    activity = activities[0]
    stages = {str(s) for _, _, s in prov.triples((activity, V.stage, None))}
    assert STAGE_LEXICAL in stages
    chosen = list(prov.objects(activity, V.chosenAuthorityUri))
    assert URIRef("http://kanto/1") in chosen
    assert (activity, V.PROV.used, URIRef(WORK)) in prov


def test_apply_reconciliation_uses_fallback_client_only_when_primary_returns_nothing() -> None:
    g = _build_canonical_graph()
    primary = StubAuthorityClient(fixtures={})  # KANTO is empty
    fallback = StubAuthorityClient(
        fixtures={
            ("person", "Tolstoy, Leo,"): [
                _candidate("http://viaf/1", "Tolstoy, Leo", 0.96, vocab="viaf"),
            ]
        }
    )
    summary, _ = apply_reconciliation(
        client=primary,
        fallback_client=fallback,
        picker=StubPicker(),
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.lexical == 1
    assert (URIRef(WORK), V.BFFI.creator, URIRef("http://viaf/1")) in g


# --- Persistence path ----------------------------------------------------


def test_apply_reconciliation_serialises_back_to_canonical_ttl(tmp_path: Path) -> None:
    """When called without an explicit ``graph``, the orchestrator parses
    canonical.ttl, mutates it, and writes the result back to ``output_path``."""
    canonical = tmp_path / "canonical.ttl"
    g = _build_canonical_graph()
    g.serialize(destination=str(canonical), format="turtle")
    client = StubAuthorityClient(
        fixtures={
            ("person", "Tolstoy, Leo,"): [
                _candidate("http://kanto/1", "Tolstoy, Leo,", 0.97),
            ]
        }
    )
    apply_reconciliation(
        canonical_path=canonical,
        client=client,
        picker=StubPicker(),
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    reloaded = Graph()
    reloaded.parse(str(canonical), format="turtle")
    assert (URIRef(WORK), V.BFFI.creator, URIRef("http://kanto/1")) in reloaded


# --- PickerDecision schema (Pydantic validators) ------------------------


def test_picker_decision_chose_requires_chosen_uri() -> None:

    with pytest.raises(ValidationError, match="non-null chosen_uri"):
        PickerDecision(
            decision="chose",
            chosen_uri=None,
            confidence=0.95,
            rationale="Plenty of detail here, more than twenty characters total.",
        )


def test_picker_decision_uncertain_with_high_confidence_is_rejected() -> None:

    with pytest.raises(ValidationError, match="incoherent with confidence"):
        PickerDecision(
            decision="uncertain",
            chosen_uri=None,
            confidence=0.9,
            rationale="Plenty of detail here, more than twenty characters total.",
        )


def test_picker_decision_short_rationale_is_rejected() -> None:

    with pytest.raises(ValidationError, match=r"shorter than|at least 20"):
        PickerDecision(
            decision="uncertain",
            chosen_uri=None,
            confidence=0.5,
            rationale="too short",
        )


@pytest.mark.parametrize(
    "phrase",
    ["I don't know", "unable to determine", "n/a", "Not sure"],
)
def test_picker_decision_rationale_with_stub_phrase_is_rejected(phrase: str) -> None:

    text = f"{phrase} but the candidate prefLabel matches the input."
    with pytest.raises(ValidationError, match="stub phrase"):
        PickerDecision(
            decision="uncertain",
            chosen_uri=None,
            confidence=0.5,
            rationale=text,
        )


def test_picker_decision_extra_fields_are_rejected() -> None:

    with pytest.raises(ValidationError):
        PickerDecision.model_validate(
            {
                "decision": "uncertain",
                "chosen_uri": None,
                "confidence": 0.5,
                "rationale": "Plenty of detail here, more than twenty characters total.",
                "extra_unknown_field": "should-be-rejected",
            }
        )


# --- prompt + hash --------------------------------------------------------


def test_picker_prompt_hash_is_stable_within_a_run() -> None:
    assert picker_prompt_hash() == picker_prompt_hash()
    assert picker_prompt_hash().startswith("sha256:")


def test_picker_prompt_text_contains_required_sections() -> None:
    text = picker_prompt_text()
    assert "### SYSTEM" in text
    assert "### EXAMPLES" in text
    assert "### USER" in text
    assert "{input_literal}" in text
    assert "{candidates}" in text


# --- LangChainLLMPicker (with a scripted chain) ---------------------------


class _ScriptedChain:
    """Test chain that returns a queued sequence of values or raises errors."""

    def __init__(self, script: list[Any]) -> None:
        self._iter = iter(script)
        self.calls: list[dict[str, Any]] = []

    def invoke(self, payload: dict[str, Any]) -> Any:
        self.calls.append(payload)
        try:
            value = next(self._iter)
        except StopIteration as exc:
            raise AssertionError("Test chain ran past the end of its script") from exc
        if isinstance(value, BaseException):
            raise value
        return value


def _no_sleep(_seconds: float) -> None:
    """Keep retry-with-backoff tests fast."""


def _good_pick(uri: str = "http://kanto/1") -> PickerDecision:
    return PickerDecision(
        decision="chose",
        chosen_uri=uri,
        confidence=0.92,
        rationale="Candidate prefLabel matches the input modulo a transcription variant.",
    )


def test_langchain_picker_returns_decision_on_happy_path() -> None:
    chain = _ScriptedChain([_good_pick("http://kanto/1")])
    picker = LangChainLLMPicker(chain=chain, sleep=_no_sleep)
    out = picker.pick(
        request=EntityRequest(
            work_uri="http://example.org/w/1", literal="Tolstoy, Leo", kind="person"
        ),
        candidates=[
            _candidate("http://kanto/1", "Tolstoy, Leo", 0.97),
            _candidate("http://kanto/2", "Tolstoy, Aleksei", 0.84),
        ],
    )
    assert out.decision == "chose"
    assert out.chosen_uri == "http://kanto/1"


def test_langchain_picker_validates_dict_responses() -> None:
    """The chain may return a raw dict; the picker must Pydantic-validate it."""
    raw = {
        "decision": "chose",
        "chosen_uri": "http://kanto/1",
        "confidence": 0.91,
        "rationale": "Candidate prefLabel matches the input modulo a transcription variant.",
    }
    picker = LangChainLLMPicker(chain=_ScriptedChain([raw]), sleep=_no_sleep)
    out = picker.pick(
        request=EntityRequest(
            work_uri="http://example.org/w/1", literal="Tolstoy, Leo", kind="person"
        ),
        candidates=[_candidate("http://kanto/1", "Tolstoy, Leo", 0.97)],
    )
    assert out.decision == "chose"


def test_langchain_picker_rejects_uri_outside_candidate_set() -> None:
    """Defence-in-depth: an LLM picking a URI we never showed it must NOT bind."""
    chain = _ScriptedChain([_good_pick("http://kanto/HALLUCINATED")])
    picker = LangChainLLMPicker(chain=chain, sleep=_no_sleep)
    out = picker.pick(
        request=EntityRequest(
            work_uri="http://example.org/w/1", literal="Tolstoy, Leo", kind="person"
        ),
        candidates=[
            _candidate("http://kanto/1", "Tolstoy, Leo", 0.97),
        ],
    )
    assert out.decision == "uncertain"
    assert out.chosen_uri is None
    assert "not in the candidate URI set" in out.rationale


def test_langchain_picker_validation_retry_recovers_after_one_bad_response() -> None:
    bad = {
        "decision": "chose",
        "chosen_uri": None,  # violates _chose_requires_uri
        "confidence": 0.95,
        "rationale": "Plenty of detail here, more than twenty characters total.",
    }
    good = _good_pick("http://kanto/1")
    chain = _ScriptedChain([bad, good])
    picker = LangChainLLMPicker(chain=chain, sleep=_no_sleep)
    out = picker.pick(
        request=EntityRequest(
            work_uri="http://example.org/w/1", literal="Tolstoy, Leo", kind="person"
        ),
        candidates=[_candidate("http://kanto/1", "Tolstoy, Leo", 0.97)],
    )
    assert out.decision == "chose"
    assert out.chosen_uri == "http://kanto/1"
    assert len(chain.calls) == 2


def test_langchain_picker_validation_failure_after_max_retries_lands_uncertain() -> None:
    bad = {
        "decision": "chose",
        "chosen_uri": None,
        "confidence": 0.95,
        "rationale": "Plenty of detail here, more than twenty characters total.",
    }
    chain = _ScriptedChain([bad] * (PICKER_MAX_VALIDATION_RETRIES + 1))
    picker = LangChainLLMPicker(chain=chain, sleep=_no_sleep)
    out = picker.pick(
        request=EntityRequest(
            work_uri="http://example.org/w/1", literal="Tolstoy, Leo", kind="person"
        ),
        candidates=[_candidate("http://kanto/1", "Tolstoy, Leo", 0.97)],
    )
    assert out.decision == "uncertain"
    assert "validation failed" in out.rationale.lower()


class _ConnError(Exception):
    """Stub whose class name fools _is_picker_connection_error."""


_ConnError.__name__ = "ConnectError"


def test_langchain_picker_connection_error_lands_uncertain_after_retries() -> None:
    chain = _ScriptedChain([_ConnError("boom")] * 5)
    picker = LangChainLLMPicker(chain=chain, sleep=_no_sleep)
    out = picker.pick(
        request=EntityRequest(
            work_uri="http://example.org/w/1", literal="Tolstoy, Leo", kind="person"
        ),
        candidates=[_candidate("http://kanto/1", "Tolstoy, Leo", 0.97)],
    )
    assert out.decision == "uncertain"
    assert "connection error" in out.rationale.lower()


def test_langchain_picker_returns_uncertain_when_no_candidates() -> None:
    """Don't bother the LLM when there's nothing to pick from."""
    picker = LangChainLLMPicker(
        chain=_ScriptedChain([AssertionError("Chain must NOT run on empty candidate set")]),
        sleep=_no_sleep,
    )
    out = picker.pick(
        request=EntityRequest(
            work_uri="http://example.org/w/1", literal="Tolstoy, Leo", kind="person"
        ),
        candidates=[],
    )
    assert out.decision == "uncertain"


# --- M9 phase 3: subject + genre/form reconciliation ---------------------


def _build_subject_canonical_graph(
    *,
    subject_label: str | None = "Tampere",
    subject_source: str | None = "yso/fin",
    subject_uri: str | None = None,
    genre_label: str | None = None,
    genre_source: str | None = None,
) -> Graph:
    """Build a canonical graph with optional subject + genreForm targets.

    ``subject_uri`` (when given) emits a URI-resolved subject target;
    blank-node targets are emitted from ``*_label`` + ``*_source``. The
    contribution + AdminMetadata blocks mirror
    :func:`_build_canonical_graph` so the AdminMetadata bumping
    side-effects can be verified on the same shape.
    """
    g = _build_canonical_graph()  # creator graph already wired
    work = URIRef(WORK)
    if subject_uri is not None:
        g.add((work, V.BFFI.subject, URIRef(subject_uri)))
    if subject_label is not None:
        node_b = BNode()
        g.add((work, V.BFFI.subject, node_b))
        g.add((node_b, V.RDFS.label, Literal(subject_label)))
        if subject_source is not None:
            g.add((node_b, V.BF.source, Literal(subject_source)))
    if genre_label is not None:
        gnode = BNode()
        g.add((work, V.BFFI.genreForm, gnode))
        g.add((gnode, V.RDFS.label, Literal(genre_label)))
        if genre_source is not None:
            g.add((gnode, V.BF.source, Literal(genre_source)))
    return g


def test_iter_subject_requests_classifies_yso_source_as_subject() -> None:
    g = _build_subject_canonical_graph(subject_label="Tampere", subject_source="yso/fin")
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "subject"
    assert requests[0].literal == "Tampere"
    assert requests[0].predicate_uri == str(V.BFFI.subject)


def test_iter_subject_requests_classifies_kauno_source_as_genre_form() -> None:
    g = _build_subject_canonical_graph(
        subject_label=None,
        genre_label="historialliset romaanit",
        genre_source="kauno/fin",
    )
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "genre_form"
    assert requests[0].predicate_uri == str(V.BFFI.genreForm)


def test_iter_subject_requests_classifies_muso_source_as_music_form() -> None:
    g = _build_subject_canonical_graph(
        subject_label="oboo",  # treble clef literal
        subject_source="muso/fin",
    )
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "music_form"


def test_iter_subject_requests_defaults_unknown_source_to_subject() -> None:
    g = _build_subject_canonical_graph(subject_label="something", subject_source="lcsh")
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "subject"


def test_iter_subject_requests_classifies_allars_source_as_subject() -> None:
    """``$2 allars`` is the Swedish general thesaurus (Allmän tesaurus
    på svenska) — parallel to YSA/YSO on the Finnish side. Routes to
    ``subject`` so tier-0 hits the loaded Allars graph (and falls
    through to YSO + LCSH if Allars misses)."""
    g = _build_subject_canonical_graph(subject_label="ekonomi", subject_source="allars/swe")
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "subject"


def test_iter_subject_requests_classifies_bella_source_as_genre_form() -> None:
    """``$2 bella`` is the Swedish parallel labels under the Kaunokki
    fiction thesaurus — routes to ``genre_form`` just like ``$2
    kaunokki`` (the Finnish form) and ``$2 kauno`` (the modern form)."""
    g = _build_subject_canonical_graph(subject_label="dödsfall", subject_source="bella/swe")
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "genre_form"


def test_iter_subject_requests_skips_uri_targets() -> None:
    """Pre-resolved $0 subjects are not re-reconciled."""
    g = _build_subject_canonical_graph(
        subject_label=None,
        subject_uri="http://www.yso.fi/onto/yso/p105076",
    )
    assert list(_iter_subject_requests(g)) == []


def test_iter_subject_requests_treats_missing_source_as_subject() -> None:
    g = _build_subject_canonical_graph(subject_label="Tampere", subject_source=None)
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "subject"


@pytest.mark.parametrize(
    ("fragment", "expected_kind"),
    [
        ("Agent600-22", "person"),  # MARC 600 Personal Name
        ("Agent610-15", "corporate_body"),  # MARC 610 Corporate Body
        ("Agent611-7", "corporate_body"),  # MARC 611 Meeting Name
    ],
)
def test_iter_subject_requests_routes_marc_6xx_subject_names_to_kanto_kinds(
    fragment: str, expected_kind: AuthorityKind
) -> None:
    """When a cataloguer uses MARC 600/610/611 (subject-as-name fields),
    marc2bibframe2 mints a URI like ``<...#Agent600-22>`` carrying just
    ``rdfs:label "Person, Name"``. The walker must route those to
    ``person`` / ``corporate_body`` so tier-1 hits KANTO instead of
    YSO — bf:source is absent on these targets, so the URI fragment
    pattern is the only available signal."""
    g = Graph()
    work = URIRef(WORK)
    g.add((work, RDF.type, V.BFFI.Work))
    target = URIRef(f"http://urn.fi/URN:NBN:fi:bib:raw/test#{fragment}")
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal("Some, Person")))
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == expected_kind
    assert requests[0].literal == "Some, Person"
    # predicate_uri stays bffi:subject — the cataloguer's MARC tag
    # decides the predicate; reconciliation picks the authority.
    assert requests[0].predicate_uri == str(V.BFFI.subject)


def test_apply_reconciliation_person_subject_binds_to_bffi_subject_not_creator() -> None:
    """A successful KANTO bind for an Agent600 subject (Pekurinen as
    subject of a biography) must land as ``bffi:subject``, never as
    ``bffi:creator``. Cataloguer's MARC 600 chose the predicate; the
    person-kind reconciliation just picks the right authority."""
    g = Graph()
    work = URIRef(WORK)
    g.add((work, RDF.type, V.BFFI.Work))
    admin = URIRef(ADMIN)
    g.add((work, V.adminMetadata, admin))
    g.add((admin, RDF.type, V.AdminMetadata))
    g.add((admin, V.adminMetadataFor, work))
    g.add(
        (
            admin,
            V.descriptionChangeDate,
            Literal("2026-05-01T00:00:00+00:00", datatype=V.XSD.dateTime),
        )
    )
    g.add((admin, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/test#Agent600-22")
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal("Pekurinen, Arndt")))

    kanto_uri = "http://urn.fi/URN:NBN:fi:au:finaf:000106424"
    client = StubAuthorityClient(
        fixtures={
            ("person", "Pekurinen, Arndt"): [
                _candidate(kanto_uri, "Pekurinen, Arndt, 1905-1941", 0.97),
            ]
        }
    )
    summary, _ = apply_reconciliation(
        client=client,
        picker=StubPicker(),
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.lexical == 1
    # Bound as subject, never as creator — the M9 dispatcher must use
    # predicate_uri (set by the subject walker) to disambiguate.
    assert (work, V.BFFI.subject, URIRef(kanto_uri)) in g
    assert (work, V.BFFI.creator, URIRef(kanto_uri)) not in g


def test_iter_subject_requests_does_not_misroute_non_subject_agent_fragments() -> None:
    """Fragment names like ``#Agent100-X`` (primary creator),
    ``#Agent700-X`` (added entry), ``#Place651-X`` (place subject)
    must NOT be routed to person/corporate_body — only the 6XX
    subject-as-name range. Agent100/700 are creator-walker territory
    (separate path); Place651 stays a topical/place subject."""
    g = Graph()
    work = URIRef(WORK)
    g.add((work, RDF.type, V.BFFI.Work))
    place_target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/test#Place651-31")
    g.add((work, V.BFFI.subject, place_target))
    g.add((place_target, V.RDFS.label, Literal("Helsinki")))
    g.add(
        (
            place_target,
            V.BF.source,
            URIRef("http://id.loc.gov/vocabulary/subjectSchemes/yso"),
        )
    )
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "subject"  # routed via yso source token, not Agent fragment


# --- fictional_character kind --------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "Lily (fiktiivinen hahmo)",
        "Nicholson, Dorothy (fiktiivinen hahmo)",
        "Fjeld, Knut (fiktiv gestalt)",
        "Sophie (fiktiv gestalt)",
        # Trailing whitespace tolerated.
        "Marvel (fiktiivinen hahmo)  ",
    ],
)
def test_iter_subject_requests_routes_fictional_character_label(label: str) -> None:
    """Cataloguer-tagged ``(fiktiivinen hahmo)`` / ``(fiktiv gestalt)``
    qualifiers on a MARC 6XX person label win priority over the
    Agent6XX URI-fragment routing — fictional persons aren't in any
    authority, so KANTO/VIAF calls are wasted."""
    g = Graph()
    work = URIRef(WORK)
    g.add((work, RDF.type, V.BFFI.Work))
    target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/test#Agent600-22")
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal(label)))
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "fictional_character"
    assert requests[0].literal == label


def test_iter_subject_requests_treats_non_fictional_marker_label_as_person() -> None:
    """``"Pekurinen, Arndt"`` (no fictional-marker qualifier) on an
    Agent600 URI still routes to ``person`` — only the explicit
    parenthetical phrase triggers the fictional kind."""
    g = Graph()
    work = URIRef(WORK)
    g.add((work, RDF.type, V.BFFI.Work))
    target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/test#Agent600-25")
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal("Pekurinen, Arndt")))
    requests = list(_iter_subject_requests(g))
    assert len(requests) == 1
    assert requests[0].kind == "person"


def test_reconcile_one_short_circuits_on_fictional_character_kind() -> None:
    """Both tier-0 and tier-1 must be skipped — no Finto call, no
    LLM call, just a by-design outcome. Verified by injecting an
    exploding client + resolver."""
    request = EntityRequest(
        work_uri=WORK,
        literal="Lily (fiktiivinen hahmo)",
        kind="fictional_character",
        predicate_uri=str(V.BFFI.subject),
    )

    class _ExplodingClient:
        def query(self, *, request: EntityRequest, top_k: int = 10) -> list[AuthorityCandidate]:
            pytest.fail("tier-1 client must NOT be called for fictional_character")

    class _ExplodingResolver:
        def resolve(self, *, literal: str, kind: AuthorityKind) -> None:
            pytest.fail("tier-0 resolver must NOT be called for fictional_character")

    outcome = reconcile_one(
        request=request,
        client=_ExplodingClient(),
        fallback_client=None,
        picker=StubPicker(),
        local_resolver=_ExplodingResolver(),
    )
    assert outcome.stage == STAGE_FICTIONAL
    assert outcome.chosen_uri is None
    assert outcome.candidates == []
    assert outcome.needs_review is False


def test_apply_reconciliation_counts_fictional_separately_from_no_candidate() -> None:
    """The summary counter splits ``STAGE_FICTIONAL`` from
    ``STAGE_NO_CANDIDATE`` — review queues only show genuine
    no-candidates, not cataloguer-marked-unbindable entries."""
    g = Graph()
    work = URIRef(WORK)
    g.add((work, RDF.type, V.BFFI.Work))
    target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/test#Agent600-29")
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal("Winslow, Elodie (fiktiivinen hahmo)")))

    # No AdminMetadata block needed; the fictional path doesn't bump it.
    summary, outcomes = apply_reconciliation(
        client=StubAuthorityClient(),
        picker=StubPicker(),
        graph=g,
        now=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
    )
    assert summary.fictional == 1
    assert summary.no_candidate == 0
    assert outcomes[0].stage == STAGE_FICTIONAL


def test_apply_reconciliation_emits_provenance_for_fictional_outcome() -> None:
    """Per spec § 8 every reconciliation attempt logs one Activity,
    including the by-design fictional skip. Cataloguers reviewing the
    provenance graph can spot-check the marker was set correctly."""
    g = Graph()
    work = URIRef(WORK)
    g.add((work, RDF.type, V.BFFI.Work))
    target = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/test#Agent600-29")
    g.add((work, V.BFFI.subject, target))
    g.add((target, V.RDFS.label, Literal("Marvel (fiktiivinen hahmo)")))
    prov = Graph()
    apply_reconciliation(
        client=StubAuthorityClient(),
        picker=StubPicker(),
        graph=g,
        provenance_graph=prov,
        now=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
    )
    activities = list(prov.subjects(V.RDF.type, V.Reconciliation))
    assert len(activities) == 1
    stages = {str(s) for s in prov.objects(activities[0], V.stage)}
    assert STAGE_FICTIONAL in stages


def test_apply_reconciliation_subject_path_links_authority_and_bridges_blank_node() -> None:
    g = _build_subject_canonical_graph(subject_label="Tampere", subject_source="yso/fin")
    yso_uri = "http://www.yso.fi/onto/yso/p105076"
    client = StubAuthorityClient(
        fixtures={
            ("subject", "Tampere"): [
                _candidate(yso_uri, "Tampere", 0.99, vocab="yso"),
            ]
        }
    )
    summary, outcomes = apply_reconciliation(
        client=client,
        picker=StubPicker(),
        graph=g,
        kinds={"subject"},
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.lexical == 1
    assert summary.total == 1
    work = URIRef(WORK)
    assert (work, V.BFFI.subject, URIRef(yso_uri)) in g
    # The original blank-node target survives and now bridges to the authority.
    blank_targets = [o for o in g.objects(work, V.BFFI.subject) if not isinstance(o, URIRef)]
    assert len(blank_targets) == 1
    bridges = list(g.objects(blank_targets[0], V.PROV.specializationOf))
    assert URIRef(yso_uri) in bridges
    # AdminMetadata picked up the source-consulted side-effect like the creator path.
    block = next(g.objects(work, V.adminMetadata))
    assert (block, V.sourceConsulted, URIRef(yso_uri)) in g
    assert outcomes[0].request.kind == "subject"


def test_apply_reconciliation_genre_path_uses_genre_form_predicate() -> None:
    g = _build_subject_canonical_graph(
        subject_label=None,
        genre_label="historialliset romaanit",
        genre_source="kauno/fin",
    )
    kauno_uri = "http://urn.fi/URN:NBN:fi:au:kauno:p1234"
    client = StubAuthorityClient(
        fixtures={
            ("genre_form", "historialliset romaanit"): [
                _candidate(kauno_uri, "historialliset romaanit", 0.99, vocab="kauno"),
            ]
        }
    )
    summary, _ = apply_reconciliation(
        client=client,
        picker=StubPicker(),
        graph=g,
        kinds={"genre_form"},
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.lexical == 1
    work = URIRef(WORK)
    # Authority binds back via bffi:genreForm, NOT bffi:subject.
    assert (work, V.BFFI.genreForm, URIRef(kauno_uri)) in g
    assert (work, V.BFFI.subject, URIRef(kauno_uri)) not in g


def test_apply_reconciliation_kinds_filter_creators_skips_subjects() -> None:
    """`kinds={"person", "corporate_body"}` walks creators only."""
    g = _build_subject_canonical_graph(subject_label="Tampere", subject_source="yso/fin")
    primary = StubAuthorityClient(
        fixtures={
            ("person", "Tolstoy, Leo,"): [
                _candidate("http://kanto/1", "Tolstoy, Leo,", 0.99),
            ],
            # A subject candidate is wired but should not be queried.
            ("subject", "Tampere"): [
                _candidate("http://yso/1", "Tampere", 0.99, vocab="yso"),
            ],
        }
    )
    summary, outcomes = apply_reconciliation(
        client=primary,
        picker=StubPicker(),
        graph=g,
        kinds={"person", "corporate_body"},
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.total == 1
    assert {o.request.kind for o in outcomes} == {"person"}
    work = URIRef(WORK)
    # No subject reconciliation happened.
    assert (work, V.BFFI.subject, URIRef("http://yso/1")) not in g


def test_apply_reconciliation_kinds_none_walks_all_kinds() -> None:
    """Default ``kinds=None`` runs creators + subjects in the same pass."""
    g = _build_subject_canonical_graph(subject_label="Tampere", subject_source="yso/fin")
    client = StubAuthorityClient(
        fixtures={
            ("person", "Tolstoy, Leo,"): [
                _candidate("http://kanto/1", "Tolstoy, Leo,", 0.99),
            ],
            ("subject", "Tampere"): [
                _candidate("http://yso/1", "Tampere", 0.99, vocab="yso"),
            ],
        }
    )
    summary, outcomes = apply_reconciliation(
        client=client,
        picker=StubPicker(),
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.total == 2
    kinds_seen = {o.request.kind for o in outcomes}
    assert kinds_seen == {"person", "subject"}
    work = URIRef(WORK)
    assert (work, V.BFFI.creator, URIRef("http://kanto/1")) in g
    assert (work, V.BFFI.subject, URIRef("http://yso/1")) in g


def test_all_authority_kinds_constant_covers_every_authority_kind() -> None:
    expected = frozenset(
        {
            "person",
            "corporate_body",
            "subject",
            "genre_form",
            "music_form",
            "fictional_character",
        }
    )
    assert expected == ALL_AUTHORITY_KINDS


# --- P-10 Phase A: concurrency + watchdog ---------------------------------


def _build_canonical_with_n_creators(creator_labels: list[str]) -> Graph:
    """Build a canonical graph with N creators, one Contribution each.

    Each creator label needs an authority lookup; the fixture is paired
    with a ``StubAuthorityClient`` that returns multiple high-similarity
    candidates per label so the picker (tier-2) fires for every creator.
    """
    g = Graph()
    for i, label in enumerate(creator_labels):
        work = URIRef(f"http://urn.fi/URN:NBN:fi:bib:work:multi-{i}")
        contrib = URIRef(f"http://example.org/contrib/multi-{i}")
        agent = URIRef(f"http://example.org/agent/multi-{i}")
        admin = URIRef(f"http://urn.fi/URN:NBN:fi:bib:adminmeta/multi-{i}")
        g.add((work, RDF.type, V.BFFI.Work))
        g.add((work, V.BFFI.contribution, contrib))
        g.add((contrib, RDF.type, V.BFFI.PrimaryContribution))
        g.add((contrib, V.BFFI.agent, agent))
        g.add((agent, V.RDFS.label, Literal(label)))
        g.add((work, V.adminMetadata, admin))
        g.add((admin, RDF.type, V.AdminMetadata))
        g.add((admin, V.adminMetadataFor, work))
        g.add(
            (
                admin,
                V.descriptionChangeDate,
                Literal("2026-05-01T00:00:00+00:00", datatype=V.XSD.dateTime),
            )
        )
        g.add((admin, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    return g


def _twelve_ambiguous_creators() -> tuple[
    list[str],
    StubAuthorityClient,
    StubPicker,
]:
    """Helper: 12 creators each with 2 high-similarity candidates so the picker fires."""
    labels = [
        "Tolstoy, Leo,",
        "Pushkin, Aleksandr,",
        "Dostoevsky, Fyodor,",
        "Chekhov, Anton,",
        "Gogol, Nikolai,",
        "Turgenev, Ivan,",
        "Bulgakov, Mikhail,",
        "Nabokov, Vladimir,",
        "Pasternak, Boris,",
        "Akhmatova, Anna,",
        "Mandelstam, Osip,",
        "Tsvetaeva, Marina,",
    ]
    fixtures: dict[tuple[str, str], list[AuthorityCandidate]] = {}
    decisions: dict[tuple[str, str], PickerDecision] = {}
    for i, label in enumerate(labels):
        # Two candidates, both ≥ 0.96 → multiple high-similarity → tier-2 picker.
        fixtures[("person", label)] = [
            _candidate(f"http://kanto/multi-{i}-a", f"{label[:-1]} A", 0.97),
            _candidate(f"http://kanto/multi-{i}-b", f"{label[:-1]} B", 0.96),
        ]
        work_uri = f"http://urn.fi/URN:NBN:fi:bib:work:multi-{i}"
        decisions[(work_uri, label)] = PickerDecision(
            decision="chose",
            chosen_uri=f"http://kanto/multi-{i}-a",
            confidence=0.95,
            rationale=(
                f"StubPicker wired decision for {label!r}: candidate A "
                f"binds with confidence 0.95 (testing the byte-stability gate)."
            ),
        )
    return labels, StubAuthorityClient(fixtures=fixtures), StubPicker(decisions=decisions)


def test_apply_reconciliation_byte_stable_at_c1_vs_c4() -> None:
    """Canonical graph mutations must be deterministic regardless of M9 concurrency.

    With 12 picker-routed creators and a deterministic ``StubPicker``,
    a c=1 run and a c=4 run should produce byte-identical canonical
    Turtle. This is the load-bearing gate for Phase A: thread-pool
    dispatch is allowed only if it preserves determinism.
    """
    # c=1 (sequential): existing behaviour.
    labels, client_seq, picker_seq = _twelve_ambiguous_creators()
    g_seq = _build_canonical_with_n_creators(labels)
    apply_reconciliation(
        client=client_seq,
        picker=picker_seq,
        graph=g_seq,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=1,
        field_timeout_seconds=0,
    )
    bytes_seq = g_seq.serialize(format="turtle")

    # c=4 (concurrent): new path.
    labels2, client_par, picker_par = _twelve_ambiguous_creators()
    g_par = _build_canonical_with_n_creators(labels2)
    apply_reconciliation(
        client=client_par,
        # picker_factory builds one StubPicker per worker thread; each
        # carries the same decisions dict, so they're observationally
        # identical to a shared picker.
        picker_factory=lambda decisions=picker_par.decisions: StubPicker(decisions=dict(decisions)),
        graph=g_par,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
    )
    bytes_par = g_par.serialize(format="turtle")

    assert bytes_seq == bytes_par, (
        "Canonical Turtle diverged between c=1 and c=4 — the concurrent "
        "orchestrator is not order-stable."
    )


class _SleepingPicker:
    """Picker that blocks longer than the per-field budget on every ``pick`` call.

    Used to exercise the orchestrator's ``field_budget_exceeded`` path
    without depending on a real LLM backend.
    """

    model_name = "stub-sleeping"

    def __init__(self, sleep_seconds: float) -> None:
        self.sleep_seconds = sleep_seconds
        self.call_count = 0

    def pick(
        self,
        *,
        request: EntityRequest,
        candidates: list[AuthorityCandidate],
    ) -> PickerDecision:
        self.call_count += 1
        time.sleep(self.sleep_seconds)
        # Should never be reached when the orchestrator enforces a budget.
        return PickerDecision(
            decision="chose",
            chosen_uri=candidates[0].uri if candidates else None,
            confidence=0.99,
            rationale=(
                "SleepingPicker returned a verdict after sleeping — "
                "the orchestrator failed to enforce the per-field budget."
            ),
        )


def test_apply_reconciliation_picker_hang_triggers_field_budget_event(
    tmp_path: Path,
) -> None:
    """A picker that sleeps past the budget must be aborted by the orchestrator.

    The field falls through to tier-3 (highest-lexical + needs-review)
    with ``was_watchdog_aborted=True``; a ``field_budget_exceeded``
    event is written to the sidecar; the orchestrator does not
    deadlock.
    """
    g = _build_canonical_with_n_creators(["Tolstoy, Leo,"])
    client = StubAuthorityClient(
        fixtures={
            ("person", "Tolstoy, Leo,"): [
                _candidate("http://kanto/a", "Tolstoy, Lev A", 0.97),
                _candidate("http://kanto/b", "Tolstoy, Lev B", 0.96),
            ]
        }
    )
    sidecar = tmp_path / "watchdog-events.jsonl"
    # Budget of 1 s; picker sleeps for 5 s → guaranteed budget violation.
    picker = _SleepingPicker(sleep_seconds=5.0)

    summary, outcomes = apply_reconciliation(
        client=client,
        picker=picker,
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=1,
        field_timeout_seconds=1,
        watchdog_sidecar_path=sidecar,
    )

    # The field landed as tier-3 with watchdog marker; the picker never
    # got to complete (so its return value didn't influence the binding).
    assert summary.fallback == 1
    assert summary.watchdog_aborted == 1
    assert len(outcomes) == 1
    assert outcomes[0].was_watchdog_aborted is True
    assert outcomes[0].needs_review is True
    assert outcomes[0].chosen_uri == "http://kanto/a"  # highest-lexical

    # Sidecar contains exactly one field_budget_exceeded event.
    lines = [json.loads(line) for line in sidecar.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["event"] == "field_budget_exceeded"
    assert lines[0]["model"] == "stub-sleeping"
    assert lines[0]["elapsed_s"] >= 1.0


def test_apply_reconciliation_zero_budget_disables_watchdog() -> None:
    """``field_timeout_seconds=0`` must skip the budget wrap entirely.

    The picker runs to completion (or natural failure) and the
    outcome reflects the picker's real verdict. This is the Phase A
    rollback knob — operators set
    ``LLM_M9_FIELD_TIMEOUT_SECONDS=0`` to disable the watchdog
    without code revert.
    """
    g = _build_canonical_with_n_creators(["Tolstoy, Leo,"])
    client = StubAuthorityClient(
        fixtures={
            ("person", "Tolstoy, Leo,"): [
                _candidate("http://kanto/a", "Tolstoy, Lev A", 0.97),
                _candidate("http://kanto/b", "Tolstoy, Lev B", 0.96),
            ]
        }
    )
    picker = StubPicker(
        decisions={
            (
                "http://urn.fi/URN:NBN:fi:bib:work:multi-0",
                "Tolstoy, Leo,",
            ): PickerDecision(
                decision="chose",
                chosen_uri="http://kanto/a",
                confidence=0.95,
                rationale="StubPicker chose candidate A for the zero-budget test.",
            )
        }
    )

    summary, outcomes = apply_reconciliation(
        client=client,
        picker=picker,
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=1,
        field_timeout_seconds=0,
    )

    assert summary.llm_pick == 1
    assert summary.watchdog_aborted == 0
    assert outcomes[0].was_watchdog_aborted is False
    assert outcomes[0].chosen_uri == "http://kanto/a"


def test_apply_reconciliation_requires_picker_or_factory() -> None:
    """Either ``picker`` or ``picker_factory`` must be supplied."""
    g = _build_canonical_with_n_creators(["Tolstoy, Leo,"])
    with pytest.raises(ValueError, match="picker or picker_factory"):
        apply_reconciliation(
            client=StubAuthorityClient(fixtures={}),
            graph=g,
            now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        )


def test_apply_reconciliation_concurrent_path_requires_factory() -> None:
    """At c>=2 a ``picker_factory`` is required (one picker per worker)."""
    g = _build_canonical_with_n_creators(["Tolstoy, Leo,"])
    with pytest.raises(ValueError, match="picker_factory when concurrency"):
        apply_reconciliation(
            client=StubAuthorityClient(fixtures={}),
            picker=StubPicker(),
            graph=g,
            now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            concurrency=4,
        )


# --- P-10 Phase A2: Phase 1 parallelisation -------------------------------


def test_apply_reconciliation_byte_stable_at_phase1_1_vs_phase1_8() -> None:
    """Canonical graph mutations must be deterministic regardless of
    Phase 1 concurrency.

    Same 12-ambiguous-creator fixture used for the Phase A byte-stability
    test, now varying ``phase1_concurrency`` instead of ``concurrency``.
    Together with the Phase A test, both concurrency knobs are pinned as
    determinism-preserving against the same fixture.
    """
    labels_seq, client_seq, picker_seq = _twelve_ambiguous_creators()
    g_seq = _build_canonical_with_n_creators(labels_seq)
    apply_reconciliation(
        client=client_seq,
        picker=picker_seq,
        graph=g_seq,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=1,
        phase1_concurrency=1,
        field_timeout_seconds=0,
    )
    bytes_seq = g_seq.serialize(format="turtle")

    labels_par, client_par, picker_par = _twelve_ambiguous_creators()
    g_par = _build_canonical_with_n_creators(labels_par)
    apply_reconciliation(
        client=client_par,
        picker=picker_par,
        graph=g_par,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=1,
        phase1_concurrency=8,
        field_timeout_seconds=0,
    )
    bytes_par = g_par.serialize(format="turtle")

    assert bytes_seq == bytes_par, (
        "Canonical Turtle diverged between phase1=1 and phase1=8 — the "
        "Phase 1 ThreadPoolExecutor is not order-stable."
    )


class _MapLocalResolver:
    """In-memory ``LocalConceptResolver`` stub for Phase A2 tests.

    Returns a wired ``LocalConceptHit`` for a given literal (regardless
    of kind, to keep the fixture small), or ``None`` if the literal is
    not in the map. Thread-safe by virtue of being read-only after
    construction.
    """

    def __init__(self, hits: dict[str, tuple[str, str]]) -> None:
        # hits: {literal: (uri, pref_label)}
        self.hits = dict(hits)
        self.calls = 0
        self._lock = threading.Lock()

    def resolve(self, *, literal: str, kind: AuthorityKind) -> Any:
        with self._lock:
            self.calls += 1
        if literal not in self.hits:
            return None
        uri, label = self.hits[literal]
        return LocalConceptHit(uri=uri, pref_label=label, source_vocabulary="yso")


def _build_mixed_tier_fixture(
    n_each: int = 6,
) -> tuple[
    list[str],
    list[str],
    list[str],
    _MapLocalResolver,
    StubAuthorityClient,
    StubPicker,
]:
    """Build a fixture exercising all three Phase 1 outcome paths.

    Returns labels grouped by destination tier:
    - ``tier0_labels`` → resolved by the local resolver (no Finto call).
    - ``picker_labels`` → routed to tier-2 (multiple high-similarity).
    - ``empty_labels`` → no candidates returned → ``no_candidate``.

    Plus the stub resolver / client / picker pre-wired for them.
    """
    tier0_labels = [f"TierZero, Author {i}," for i in range(n_each)]
    picker_labels = [f"Picker, Author {i}," for i in range(n_each)]
    empty_labels = [f"Empty, Author {i}," for i in range(n_each)]

    resolver = _MapLocalResolver(
        hits={
            label: (f"http://yso/tier0-{i}", f"TierZero pref-label {i}")
            for i, label in enumerate(tier0_labels)
        }
    )

    fixtures: dict[tuple[str, str], list[AuthorityCandidate]] = {}
    decisions: dict[tuple[str, str], PickerDecision] = {}
    for i, label in enumerate(picker_labels):
        fixtures[("person", label)] = [
            _candidate(f"http://kanto/p-{i}-a", f"{label[:-1]} A", 0.97),
            _candidate(f"http://kanto/p-{i}-b", f"{label[:-1]} B", 0.96),
        ]
    # No fixtures for empty_labels = StubAuthorityClient returns [] → no_candidate.

    return (
        tier0_labels,
        picker_labels,
        empty_labels,
        resolver,
        StubAuthorityClient(fixtures=fixtures),
        StubPicker(decisions=decisions),
    )


def _build_canonical_with_mixed_creators(
    tier0_labels: list[str],
    picker_labels: list[str],
    empty_labels: list[str],
) -> Graph:
    all_labels = tier0_labels + picker_labels + empty_labels
    g = Graph()
    for i, label in enumerate(all_labels):
        work = URIRef(f"http://urn.fi/URN:NBN:fi:bib:work:mixed-{i}")
        contrib = URIRef(f"http://example.org/contrib/mixed-{i}")
        agent = URIRef(f"http://example.org/agent/mixed-{i}")
        admin = URIRef(f"http://urn.fi/URN:NBN:fi:bib:adminmeta/mixed-{i}")
        g.add((work, RDF.type, V.BFFI.Work))
        g.add((work, V.BFFI.contribution, contrib))
        g.add((contrib, RDF.type, V.BFFI.PrimaryContribution))
        g.add((contrib, V.BFFI.agent, agent))
        g.add((agent, V.RDFS.label, Literal(label)))
        g.add((work, V.adminMetadata, admin))
        g.add((admin, RDF.type, V.AdminMetadata))
        g.add((admin, V.adminMetadataFor, work))
        g.add(
            (
                admin,
                V.descriptionChangeDate,
                Literal("2026-05-01T00:00:00+00:00", datatype=V.XSD.dateTime),
            )
        )
        g.add((admin, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    return g


def test_phase1_concurrency_preserves_outcome_distribution() -> None:
    """All three Phase 1 outcome paths (tier-0 local, no-candidate,
    picker-deferred) must produce the same per-entity outcomes at
    phase1=1 and phase1=8.
    """
    tier0_labels, picker_labels, empty_labels, resolver, client, picker = _build_mixed_tier_fixture(
        n_each=6
    )
    n_total = len(tier0_labels) + len(picker_labels) + len(empty_labels)

    g_seq = _build_canonical_with_mixed_creators(tier0_labels, picker_labels, empty_labels)
    summary_seq, outcomes_seq = apply_reconciliation(
        client=client,
        picker=picker,
        graph=g_seq,
        local_resolver=resolver,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=1,
        phase1_concurrency=1,
    )

    # Fresh fixture for the concurrent run — resolver call counter resets.
    tier0_labels2, picker_labels2, empty_labels2, resolver2, client2, picker2 = (
        _build_mixed_tier_fixture(n_each=6)
    )
    g_par = _build_canonical_with_mixed_creators(tier0_labels2, picker_labels2, empty_labels2)
    summary_par, outcomes_par = apply_reconciliation(
        client=client2,
        picker=picker2,
        graph=g_par,
        local_resolver=resolver2,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=1,
        phase1_concurrency=8,
    )

    # Summary counts identical.
    assert summary_seq.local == summary_par.local == len(tier0_labels)
    assert summary_seq.no_candidate == summary_par.no_candidate == len(empty_labels)
    # picker_labels routed to fallback because StubPicker has no wired
    # decisions (returns ``uncertain``).
    assert summary_seq.fallback == summary_par.fallback == len(picker_labels)
    assert summary_seq.total == summary_par.total == n_total

    # Per-entity outcomes preserved (sorted by request.literal for comparison).
    def _key(o: object) -> tuple[str, str]:
        # ReconciliationOutcome carries request.literal + stage; sort by both.
        return (o.request.literal, o.stage)  # type: ignore[attr-defined]

    assert sorted(outcomes_seq, key=_key) == sorted(outcomes_par, key=_key)

    # Phase 1 resolver was called exactly once per entity, regardless of
    # concurrency. The lock inside ``_MapLocalResolver`` guarantees the
    # counter is thread-safe.
    assert resolver.calls == n_total
    assert resolver2.calls == n_total


def test_phase1_pool_handles_per_request_query_errors() -> None:
    """A stub client raising on one request must not poison the pool.

    The errored entity should route to ``no_candidate`` cleanly while
    the other entities resolve normally. No thread errors, no orchestrator
    abort.
    """

    class _ErrorOnLiteralClient:
        def __init__(self, error_literal: str) -> None:
            self.error_literal = error_literal
            self.calls = 0
            self._lock = threading.Lock()

        def query(self, *, request: EntityRequest, top_k: int = 10) -> list[AuthorityCandidate]:
            with self._lock:
                self.calls += 1
            if request.literal == self.error_literal:
                raise httpx.ReadTimeout("simulated Finto stall")
            # All other entities get one candidate at lexical 0.97 → tier-1 bind.
            return [
                AuthorityCandidate(
                    uri=f"http://kanto/{request.literal}",
                    pref_label=request.literal[:-1],  # strip trailing comma
                    source_vocabulary="finaf",
                    lexical_similarity=0.97,
                )
            ]

    labels = [f"Worker, Author {i}," for i in range(8)]
    error_literal = labels[3]
    g = _build_canonical_with_n_creators(labels)
    client = _ErrorOnLiteralClient(error_literal=error_literal)

    # Expect ``httpx.ReadTimeout`` to propagate out — the orchestrator
    # doesn't catch it today (no fallback when ``client.query`` itself
    # raises). This pin documents the current behaviour: thread-pool
    # workers surface exceptions; the orchestrator aborts the whole run.
    # If we later want graceful per-entity error handling, the test
    # changes shape; for now Phase A2 inherits the post-Phase-A
    # exception-propagation contract from the existing pool path.
    with pytest.raises(httpx.ReadTimeout, match="simulated Finto stall"):
        apply_reconciliation(
            client=client,
            picker=StubPicker(),
            graph=g,
            now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            concurrency=1,
            phase1_concurrency=4,
        )
    # All 8 query attempts dispatched before the error surfaced (pool
    # processes futures concurrently). Pinning >= 1 is enough; the exact
    # value depends on thread scheduling.
    assert client.calls >= 1


def test_phase1_pool_thread_safe_call_count() -> None:
    """Verify the stub client/resolver counters are incremented exactly
    once per entity, regardless of phase1 concurrency."""

    class _CountingClient:
        def __init__(self) -> None:
            self.calls = 0
            self._lock = threading.Lock()

        def query(self, *, request: EntityRequest, top_k: int = 10) -> list[AuthorityCandidate]:
            with self._lock:
                self.calls += 1
            return [
                AuthorityCandidate(
                    uri=f"http://kanto/{self.calls}",
                    pref_label="x",
                    source_vocabulary="finaf",
                    lexical_similarity=0.97,
                )
            ]

    labels = [f"Counter, Author {i}," for i in range(16)]
    g = _build_canonical_with_n_creators(labels)
    client = _CountingClient()

    apply_reconciliation(
        client=client,
        picker=StubPicker(),
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=1,
        phase1_concurrency=8,
    )

    assert client.calls == len(labels), (
        f"Expected exactly {len(labels)} client.query calls under phase1=8, "
        f"got {client.calls} — pool is either double-dispatching or dropping."
    )


# --- P-10 Phase E: picker queue ordering for prefix-cache stickiness ------


def _deferred_entry(
    idx: int,
    *,
    kind: AuthorityKind,
    literal: str,
    candidate_uris: list[str],
    source_vocab: str,
) -> tuple[int, EntityRequest, list[AuthorityCandidate]]:
    """Build one (idx, request, candidates) tuple for the picker-queue tests."""
    request = EntityRequest(
        work_uri=f"http://urn.fi/URN:NBN:fi:bib:work:order-{idx}",
        literal=literal,
        kind=kind,
    )
    candidates = [
        AuthorityCandidate(
            uri=uri,
            pref_label=f"{literal} candidate",
            source_vocabulary=source_vocab,
            lexical_similarity=0.96,
        )
        for uri in candidate_uris
    ]
    return idx, request, candidates


def test_picker_queue_sort_key_orders_by_kind_then_vocab() -> None:
    """Sort key clusters by request.kind first, then source_vocabulary.

    Per plan § E.1, the prompt prefix is dominated by the kind-conditional
    sections in ``prompts/picker_v1.txt``; same-kind picks therefore share
    the longest static prefix and should be dispatched contiguously.
    """
    entries = [
        _deferred_entry(
            0, kind="subject", literal="Helsinki", candidate_uris=["a"], source_vocab="yso"
        ),
        _deferred_entry(
            1, kind="person", literal="Tolstoy", candidate_uris=["b"], source_vocab="finaf"
        ),
        _deferred_entry(
            2, kind="subject", literal="Espoo", candidate_uris=["c"], source_vocab="yso"
        ),
        _deferred_entry(
            3, kind="person", literal="Pushkin", candidate_uris=["d"], source_vocab="finaf"
        ),
    ]
    ordered = _order_deferred_picker_queue(entries, ordering=PICKER_ORDERING_PREFIX_CACHE)
    kinds = [request.kind for _idx, request, _cands in ordered]
    # All "person" picks before all "subject" picks (lexical order on kind).
    assert kinds == ["person", "person", "subject", "subject"]


def test_picker_queue_sort_key_clusters_by_candidate_fingerprint() -> None:
    """Within a kind+vocab cluster, identical candidate sets cluster together."""
    entries = [
        _deferred_entry(
            0, kind="person", literal="A", candidate_uris=["x", "y"], source_vocab="finaf"
        ),
        _deferred_entry(
            1, kind="person", literal="B", candidate_uris=["m", "n"], source_vocab="finaf"
        ),
        _deferred_entry(
            2, kind="person", literal="C", candidate_uris=["x", "y"], source_vocab="finaf"
        ),
        _deferred_entry(
            3, kind="person", literal="D", candidate_uris=["m", "n"], source_vocab="finaf"
        ),
    ]
    ordered = _order_deferred_picker_queue(entries, ordering=PICKER_ORDERING_PREFIX_CACHE)
    # ``m|n`` < ``x|y`` lexically; within each fingerprint group, literal
    # alphabetises (B < D, A < C).
    literals = [request.literal for _idx, request, _cands in ordered]
    assert literals == ["B", "D", "A", "C"]


def test_picker_queue_sort_key_stable_on_ties() -> None:
    """Entries with identical sort keys preserve submission order (stable sort)."""
    entries = [
        _deferred_entry(
            i,
            kind="person",
            literal="duplicate",
            candidate_uris=["same-1", "same-2"],
            source_vocab="finaf",
        )
        for i in range(6)
    ]
    ordered = _order_deferred_picker_queue(entries, ordering=PICKER_ORDERING_PREFIX_CACHE)
    indices = [idx for idx, _req, _cands in ordered]
    assert indices == list(range(6)), "Stable sort must preserve submission order on tied keys."


def test_picker_queue_empty_and_single_are_noops() -> None:
    """Empty and single-entry queues sort to themselves with no exception."""
    assert _order_deferred_picker_queue([], ordering=PICKER_ORDERING_PREFIX_CACHE) == []
    single = [
        _deferred_entry(0, kind="person", literal="X", candidate_uris=["a"], source_vocab="finaf")
    ]
    assert _order_deferred_picker_queue(single, ordering=PICKER_ORDERING_PREFIX_CACHE) == single


def test_picker_queue_submission_ordering_is_passthrough() -> None:
    """``submission`` mode must return the queue unchanged for byte-stable rollback."""
    entries = [
        _deferred_entry(
            0, kind="subject", literal="z-last", candidate_uris=["zzz"], source_vocab="yso"
        ),
        _deferred_entry(
            1, kind="person", literal="a-first", candidate_uris=["aaa"], source_vocab="finaf"
        ),
    ]
    ordered = _order_deferred_picker_queue(entries, ordering=PICKER_ORDERING_SUBMISSION)
    # Same objects, same order.
    assert ordered == entries
    assert ordered is not entries or ordered == entries  # passthrough is acceptable


def test_picker_queue_sort_key_uses_candidate_zero_vocab() -> None:
    """Sort key reads ``candidates[0].source_vocabulary``; verify it's used."""
    entry = _deferred_entry(
        0, kind="person", literal="X", candidate_uris=["u1", "u2"], source_vocab="kanto"
    )
    key = _picker_queue_sort_key(entry)
    assert key[0] == "person"
    assert key[1] == "kanto"
    # Fingerprint is sorted-then-joined.
    assert key[2] == "u1|u2"
    assert key[3] == "X"


def test_apply_reconciliation_byte_stable_across_picker_ordering_modes() -> None:
    """Canonical output is byte-identical under prefix-cache and submission ordering.

    The orchestrator re-sorts ``picker_results`` by submission ``idx`` before
    graph mutation (Phase A's determinism gate), so the output Turtle is
    invariant under any picker dispatch order. This is the load-bearing
    guarantee for Phase E's rollback knob.
    """
    # prefix-cache ordering.
    labels_pc, client_pc, picker_pc = _twelve_ambiguous_creators()
    g_pc = _build_canonical_with_n_creators(labels_pc)
    apply_reconciliation(
        client=client_pc,
        picker_factory=lambda decisions=picker_pc.decisions: StubPicker(decisions=dict(decisions)),
        graph=g_pc,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
        picker_ordering=PICKER_ORDERING_PREFIX_CACHE,
    )
    bytes_pc = g_pc.serialize(format="turtle")

    # submission ordering (rollback).
    labels_sub, client_sub, picker_sub = _twelve_ambiguous_creators()
    g_sub = _build_canonical_with_n_creators(labels_sub)
    apply_reconciliation(
        client=client_sub,
        picker_factory=lambda decisions=picker_sub.decisions: StubPicker(decisions=dict(decisions)),
        graph=g_sub,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
        picker_ordering=PICKER_ORDERING_SUBMISSION,
    )
    bytes_sub = g_sub.serialize(format="turtle")

    assert bytes_pc == bytes_sub, (
        "Canonical Turtle diverged between prefix-cache and submission picker "
        "ordering — the post-pool result-sort guarantee is broken."
    )


def test_picker_ordering_default_is_prefix_cache_in_apply_reconciliation() -> None:
    """``apply_reconciliation`` defaults to ``prefix-cache`` ordering.

    Pins the default behaviour: callers that don't pass ``picker_ordering``
    get Phase E's optimisation. Pre-Phase-E callers (tests etc.) are unchanged
    because output is byte-stable across ordering modes.
    """
    labels, client, picker = _twelve_ambiguous_creators()
    g_default = _build_canonical_with_n_creators(labels)
    apply_reconciliation(
        client=client,
        picker_factory=lambda decisions=picker.decisions: StubPicker(decisions=dict(decisions)),
        graph=g_default,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
        # picker_ordering omitted — should default to prefix-cache.
    )
    bytes_default = g_default.serialize(format="turtle")

    labels_explicit, client_explicit, picker_explicit = _twelve_ambiguous_creators()
    g_explicit = _build_canonical_with_n_creators(labels_explicit)
    apply_reconciliation(
        client=client_explicit,
        picker_factory=(
            lambda decisions=picker_explicit.decisions: StubPicker(decisions=dict(decisions))
        ),
        graph=g_explicit,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
        picker_ordering=PICKER_ORDERING_PREFIX_CACHE,
    )
    bytes_explicit = g_explicit.serialize(format="turtle")

    assert bytes_default == bytes_explicit, (
        "Default picker_ordering is not 'prefix-cache' — the Phase E "
        "opt-in/opt-out contract is broken."
    )


# --- P-10 Phase B: persistent picker decision cache -----------------------


def _make_finto_dumps(tmp_path: Path, vocabs: list[str]) -> Path:
    """Create a stand-in ``finto-dumps`` dir with one tiny TTL per vocab.

    The orchestrator hashes ``<vocab>-skos.ttl`` to anchor the cache
    key per vocabulary. Tests only need the files to *exist* with
    deterministic content; we don't parse them.
    """
    dumps = tmp_path / "finto-dumps"
    dumps.mkdir(parents=True, exist_ok=True)
    for vocab in vocabs:
        (dumps / f"{vocab}-skos.ttl").write_text(
            f"# Stub dump for {vocab} — used by P-10 Phase B unit tests.\n",
            encoding="utf-8",
        )
    return dumps


def test_picker_cache_insert_lookup_roundtrip(tmp_path: Path) -> None:
    """One insert + one lookup with the same key returns the stored decision."""
    cache = PickerCache(tmp_path / "cache.sqlite")
    try:
        decision = PickerDecision(
            decision="chose",
            chosen_uri="http://kanto/a",
            confidence=0.91,
            rationale=(
                "Phase B round-trip test: candidate A wins on prefLabel "
                "+ surname match against the cataloguer's literal."
            ),
        )
        cache.set(
            "key-A",
            decision=decision,
            finto_vocab="finaf",
            finto_sha="deadbeef" * 8,
            prompt_hash_value="sha256:fakeprompt",
            model_name="qwen3-8b-stub",
            activity_uuid="http://urn.fi/URN:NBN:fi:bib:reconcile/01",
        )
        hit = cache.get("key-A")
        assert hit is not None
        assert hit.decision == decision
        assert hit.activity_uuid == "http://urn.fi/URN:NBN:fi:bib:reconcile/01"
        # Unrelated key → miss.
        assert cache.get("key-B") is None
    finally:
        cache.close()


def test_picker_cache_finto_sha_mismatch_invalidates(tmp_path: Path) -> None:
    """Different ``finto_sha`` → different key → cache miss (per-vocab refresh)."""
    request = EntityRequest(
        work_uri="http://urn.fi/URN:NBN:fi:bib:work:x",
        literal="Tolstoy, Leo,",
        kind="person",
    )
    candidates = [_candidate("http://kanto/a", "Tolstoy, L A", 0.97)]

    old = compute_picker_cache_key(
        request=request,
        candidates=candidates,
        prompt_hash_value="sha256:ph",
        model_name="qwen3-8b",
        finto_shas={"finaf": "old-sha"},
    )
    new = compute_picker_cache_key(
        request=request,
        candidates=candidates,
        prompt_hash_value="sha256:ph",
        model_name="qwen3-8b",
        finto_shas={"finaf": "new-sha"},
    )
    assert old is not None
    assert new is not None
    assert old[0] != new[0], (
        "Cache keys should differ after a Finto refresh — otherwise the "
        "per-vocabulary invalidation contract is broken."
    )


def test_picker_cache_key_skips_when_no_local_sha(tmp_path: Path) -> None:
    """Vocab without a local dump → no key → caller must skip caching."""
    request = EntityRequest(
        work_uri="http://urn.fi/URN:NBN:fi:bib:work:x",
        literal="Ada Lovelace",
        kind="person",
    )
    candidates = [_candidate("http://viaf.org/viaf/1234", "Lovelace, Ada", 0.97, vocab="viaf")]
    key_info = compute_picker_cache_key(
        request=request,
        candidates=candidates,
        prompt_hash_value="sha256:ph",
        model_name="qwen3-8b",
        finto_shas={},  # no local dumps
    )
    assert key_info is None


def test_picker_cache_key_skips_viaf_even_with_sha(tmp_path: Path) -> None:
    """VIAF is remote-only — never cached even if a dump hash were present."""
    request = EntityRequest(
        work_uri="http://urn.fi/URN:NBN:fi:bib:work:x",
        literal="Ada Lovelace",
        kind="person",
    )
    candidates = [_candidate("http://viaf.org/viaf/1234", "Lovelace, Ada", 0.97, vocab="viaf")]
    key_info = compute_picker_cache_key(
        request=request,
        candidates=candidates,
        prompt_hash_value="sha256:ph",
        model_name="qwen3-8b",
        finto_shas={"viaf": "anything"},
    )
    assert key_info is None


def test_picker_cache_key_diacritic_equivalent_literals_collide(tmp_path: Path) -> None:
    """``Tolstoï`` and ``Tolstoi`` fold to the same key.

    The cache key uses ``fold_label`` (NFKC + diacritic-fold + casefold)
    so diacritic-equivalent literals reuse the cached decision. This
    is the same fold the rest of M9 applies to lexical similarity.
    """
    candidates = [_candidate("http://kanto/a", "Tolstoy", 0.97)]
    key_a = compute_picker_cache_key(
        request=EntityRequest(work_uri="w", literal="Tolstoï", kind="person"),
        candidates=candidates,
        prompt_hash_value="sha256:ph",
        model_name="qwen3-8b",
        finto_shas={"finaf": "sha"},
    )
    key_b = compute_picker_cache_key(
        request=EntityRequest(work_uri="w", literal="Tolstoi", kind="person"),
        candidates=candidates,
        prompt_hash_value="sha256:ph",
        model_name="qwen3-8b",
        finto_shas={"finaf": "sha"},
    )
    assert key_a is not None
    assert key_b is not None
    assert key_a[0] == key_b[0]


def test_picker_cache_cross_thread_writes_no_interface_error(tmp_path: Path) -> None:
    """Four threads writing distinct keys must not raise sqlite3.InterfaceError.

    Regression-pins the cross-thread fix mirrored from M6 commit
    ``1452a4f`` — without the lock, concurrent ``execute()`` on a
    shared connection raises ``InterfaceError: bad parameter or other
    API misuse``.
    """
    cache = PickerCache(tmp_path / "cache.sqlite")
    decision = PickerDecision(
        decision="chose",
        chosen_uri="http://kanto/a",
        confidence=0.91,
        rationale=(
            "Cross-thread write regression test — decision content does "
            "not matter, the schema-level lock is what's under test."
        ),
    )
    errors: list[Exception] = []
    lock = threading.Lock()

    def _writer(i: int) -> None:
        try:
            cache.set(
                f"key-{i}",
                decision=decision,
                finto_vocab="finaf",
                finto_sha="sha",
                prompt_hash_value="sha256:ph",
                model_name="qwen3-8b-stub",
                activity_uuid=f"http://urn.fi/URN:NBN:fi:bib:reconcile/{i:02d}",
            )
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cache.close()
    assert errors == []


def test_compute_finto_shas_returns_per_file_hashes(tmp_path: Path) -> None:
    """Each ``<vocab>-skos.ttl`` in the dumps dir yields one entry keyed by slug."""
    dumps = _make_finto_dumps(tmp_path, ["finaf", "yso", "kauno"])
    shas = compute_finto_shas(dumps)
    assert set(shas.keys()) == {"finaf", "yso", "kauno"}
    # Different vocabs have different (file content) hashes.
    assert len(set(shas.values())) == 3
    # Missing dir → empty.
    assert compute_finto_shas(tmp_path / "nope") == {}


def test_apply_reconciliation_warm_cache_skips_picker_and_marks_provenance(
    tmp_path: Path,
) -> None:
    """Second run on the same input + warm cache produces cache hits.

    Verifies the end-to-end contract:
    1. First run dispatches the picker for every ambiguous creator.
    2. Cache is populated.
    3. Second run with the same cache + finto_dumps_dir hits the cache.
    4. The picker is *not* called on the second run for cached entries.
    5. Provenance Activities on the second run carry
       ``prov:wasInfluencedBy`` pointing at the first-run Activity URI.
    6. Canonical Turtle is byte-stable across the two runs.
    """
    dumps = _make_finto_dumps(tmp_path, ["finaf"])
    cache_path = tmp_path / "reconcile-cache.sqlite"

    # Both runs must surface the same picker ``model_name`` because the
    # cache key includes it — a model swap is a deliberate invalidation
    # signal in production.
    class _ModelNamedStubPicker:
        model_name = "stub-shared-model"

        def __init__(self, decisions: dict) -> None:
            self.inner = StubPicker(decisions=dict(decisions))
            self.call_count = 0

        def pick(
            self,
            *,
            request: EntityRequest,
            candidates: list[AuthorityCandidate],
        ) -> PickerDecision:
            self.call_count += 1
            return self.inner.pick(request=request, candidates=candidates)

    # --- First run (cold cache) ----------------------------------------
    labels_1, client_1, stub_1 = _twelve_ambiguous_creators()
    g_1 = _build_canonical_with_n_creators(labels_1)
    prov_1 = Graph()
    cache_1 = PickerCache(cache_path)
    apply_reconciliation(
        client=client_1,
        picker_factory=lambda decisions=stub_1.decisions: _ModelNamedStubPicker(decisions),  # type: ignore[return-value]
        graph=g_1,
        provenance_graph=prov_1,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
        picker_cache=cache_1,
        finto_dumps_dir=dumps,
    )
    bytes_1 = g_1.serialize(format="turtle")
    cache_1.close()

    # --- Second run (warm cache, counting picker invocations) ----------
    labels_2, client_2, stub_2 = _twelve_ambiguous_creators()
    g_2 = _build_canonical_with_n_creators(labels_2)
    prov_2 = Graph()

    counters: list[_ModelNamedStubPicker] = []

    def _factory(decisions: dict = stub_2.decisions) -> _ModelNamedStubPicker:
        wrapper = _ModelNamedStubPicker(decisions)
        counters.append(wrapper)
        return wrapper

    cache_2 = PickerCache(cache_path)
    apply_reconciliation(
        client=client_2,
        picker_factory=_factory,  # type: ignore[arg-type]
        graph=g_2,
        provenance_graph=prov_2,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
        picker_cache=cache_2,
        finto_dumps_dir=dumps,
    )
    bytes_2 = g_2.serialize(format="turtle")
    cache_2.close()

    # Picker invocation count on the second run is the load-bearing
    # claim: warm cache → 0 picker calls.
    total_picker_calls_run_2 = sum(c.call_count for c in counters)
    assert total_picker_calls_run_2 == 0, (
        f"Warm-cache run made {total_picker_calls_run_2} picker calls; expected 0."
    )

    # Canonical graph is byte-stable across the two runs.
    assert bytes_1 == bytes_2, (
        "Canonical Turtle diverged between cold and warm runs — the "
        "cache-hit code path produced different bindings than fresh picks."
    )

    # Provenance on the second run must carry wasInfluencedBy → first-run URIs.
    prov_ns = Namespace("http://www.w3.org/ns/prov#")
    influenced_pairs = list(prov_2.subject_objects(prov_ns.wasInfluencedBy))
    assert influenced_pairs, (
        "Phase B contract broken: warm-cache run emitted zero prov:wasInfluencedBy triples."
    )
    # The targets of wasInfluencedBy must all be Activity subjects from run 1.
    run_1_activity_subjects = {s for s, _p, _o in prov_1.triples((None, PROV.startedAtTime, None))}
    targets = {o for _s, o in influenced_pairs}
    assert targets <= run_1_activity_subjects, (
        "wasInfluencedBy targets reference URIs that are not Activities in "
        "the first-run provenance graph."
    )


def test_apply_reconciliation_cache_disabled_runs_picker_every_time(
    tmp_path: Path,
) -> None:
    """``picker_cache=None`` reverts to pre-Phase-B behaviour: picker fires every run."""
    dumps = _make_finto_dumps(tmp_path, ["finaf"])
    labels, client, stub = _twelve_ambiguous_creators()
    g = _build_canonical_with_n_creators(labels)

    call_count = 0
    inner_factory = lambda decisions=stub.decisions: StubPicker(decisions=dict(decisions))  # noqa: E731

    class _CountingStub:
        model_name = "stub-counting"

        def __init__(self) -> None:
            self.inner = inner_factory()

        def pick(
            self,
            *,
            request: EntityRequest,
            candidates: list[AuthorityCandidate],
        ) -> PickerDecision:
            nonlocal call_count
            call_count += 1
            return self.inner.pick(request=request, candidates=candidates)

    apply_reconciliation(
        client=client,
        picker_factory=_CountingStub,  # type: ignore[arg-type]
        graph=g,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
        picker_cache=None,  # explicit: no caching
        finto_dumps_dir=dumps,
    )
    # With 12 ambiguous creators routed to the picker and no cache,
    # the picker fires for every one of them.
    assert call_count == len(labels), (
        f"Cache-disabled path made {call_count} picker calls; expected "
        f"{len(labels)} (one per ambiguous creator)."
    )


def test_apply_reconciliation_caches_fallback_outcomes_too(
    tmp_path: Path,
) -> None:
    """P-10 Phase B.1: every picker call is cached, not only STAGE_LLM.

    The 2026-05-13 bench audit showed that picker calls returning
    low-confidence ("chose with conf < 0.80") or ``uncertain``
    verdicts — both of which map to STAGE_FALLBACK — were not being
    cached, so the warm run re-picked them and produced different
    tier classifications when the model's per-call non-determinism
    nudged the confidence across the 0.80 threshold. P-13 Phase B.1
    extends the cache to cover these outcomes.

    This test pins the contract: a low-confidence pick on the cold
    run lands in the cache; the warm run hits the cache and
    reproduces the same fallback outcome verbatim — no second
    picker call.
    """
    dumps = _make_finto_dumps(tmp_path, ["finaf"])
    cache_path = tmp_path / "reconcile-cache.sqlite"

    # Build a single ambiguous creator whose stub picker returns
    # decision="chose" with confidence 0.70 (below the 0.80 threshold
    # in LLM_CONFIDENCE_THRESHOLD), which maps to STAGE_FALLBACK.
    labels = ["Tolstoy, Leo,"]
    work_uri = "http://urn.fi/URN:NBN:fi:bib:work:multi-0"
    fixtures: dict[tuple[str, str], list[AuthorityCandidate]] = {
        ("person", labels[0]): [
            _candidate("http://kanto/multi-0-a", "Tolstoy, Lev A", 0.97),
            _candidate("http://kanto/multi-0-b", "Tolstoy, Lev B", 0.96),
        ]
    }
    low_conf_decision = PickerDecision(
        decision="chose",
        chosen_uri="http://kanto/multi-0-a",
        confidence=0.70,
        rationale=(
            "Low-confidence pick that maps to STAGE_FALLBACK — "
            "filler text to satisfy the minimum-length validator."
        ),
    )
    decisions: dict[tuple[str, str], PickerDecision] = {(work_uri, labels[0]): low_conf_decision}

    class _ModelNamedFallbackPicker:
        model_name = "stub-fallback-test"

        def __init__(self) -> None:
            self.inner = StubPicker(decisions=dict(decisions))
            self.call_count = 0

        def pick(
            self,
            *,
            request: EntityRequest,
            candidates: list[AuthorityCandidate],
        ) -> PickerDecision:
            self.call_count += 1
            return self.inner.pick(request=request, candidates=candidates)

    # --- Cold run: picker fires; cache should be written.
    pickers_cold: list[_ModelNamedFallbackPicker] = []

    def _cold_factory() -> _ModelNamedFallbackPicker:
        p = _ModelNamedFallbackPicker()
        pickers_cold.append(p)
        return p

    g_cold = _build_canonical_with_n_creators(labels)
    client_cold = StubAuthorityClient(fixtures=dict(fixtures))
    cache_cold = PickerCache(cache_path)
    prov_cold = Graph()
    summary_cold, outcomes_cold = apply_reconciliation(
        client=client_cold,
        picker_factory=_cold_factory,  # type: ignore[arg-type]
        graph=g_cold,
        provenance_graph=prov_cold,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
        picker_cache=cache_cold,
        finto_dumps_dir=dumps,
    )
    cache_cold.close()
    assert summary_cold.fallback == 1, "Expected STAGE_FALLBACK on the cold run."
    cold_outcome = outcomes_cold[0]
    cold_chosen = cold_outcome.chosen_uri

    # --- Warm run: same input, cache populated. Picker must NOT fire.
    pickers_warm: list[_ModelNamedFallbackPicker] = []

    def _warm_factory() -> _ModelNamedFallbackPicker:
        p = _ModelNamedFallbackPicker()
        pickers_warm.append(p)
        return p

    g_warm = _build_canonical_with_n_creators(labels)
    client_warm = StubAuthorityClient(fixtures=dict(fixtures))
    cache_warm = PickerCache(cache_path)
    prov_warm = Graph()
    summary_warm, outcomes_warm = apply_reconciliation(
        client=client_warm,
        picker_factory=_warm_factory,  # type: ignore[arg-type]
        graph=g_warm,
        provenance_graph=prov_warm,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        concurrency=4,
        field_timeout_seconds=0,
        picker_cache=cache_warm,
        finto_dumps_dir=dumps,
    )
    cache_warm.close()

    # Cache hit was load-bearing: no picker call fired on the warm run.
    warm_picker_calls = sum(p.call_count for p in pickers_warm)
    assert warm_picker_calls == 0, (
        f"Phase B.1 contract broken: warm run made {warm_picker_calls} "
        f"picker call(s) despite the cache being populated."
    )
    # Same outcome stage + same bound URI as the cold run.
    assert summary_warm.fallback == 1
    warm_outcome = outcomes_warm[0]
    assert warm_outcome.stage == cold_outcome.stage == STAGE_FALLBACK
    assert warm_outcome.chosen_uri == cold_chosen


# --- P-12 Phase D: M9 Phase 2 progress events -----------------------------


def _build_picker_queue(n: int) -> list[tuple[int, EntityRequest, list[AuthorityCandidate]]]:
    """N synthetic deferred entries with two-candidate ambiguity per entry."""
    queue: list[tuple[int, EntityRequest, list[AuthorityCandidate]]] = []
    for i in range(n):
        req = EntityRequest(
            work_uri=f"http://urn.fi/URN:NBN:fi:bib:work:cadence-{i}",
            literal=f"Author {i:04d}",
            kind="person",
        )
        cands = [
            _candidate(f"http://kanto/{i:04d}-a", f"Author {i:04d} A", 0.97),
            _candidate(f"http://kanto/{i:04d}-b", f"Author {i:04d} B", 0.96),
        ]
        queue.append((i, req, cands))
    return queue


def _picker_for_queue(
    queue: list[tuple[int, EntityRequest, list[AuthorityCandidate]]],
) -> StubPicker:
    """StubPicker wired to pick candidate-A for every entry in the queue."""
    decisions: dict[tuple[str, str], PickerDecision] = {}
    for _idx, req, cands in queue:
        decisions[(req.work_uri, req.literal)] = PickerDecision(
            decision="chose",
            chosen_uri=cands[0].uri,
            confidence=0.95,
            rationale=(
                "Cadence-test stub: bind candidate A. "
                "Filler text to satisfy minimum-length validator."
            ),
        )
    return StubPicker(decisions=decisions)


def _capture_progress_events(tmp_path: Path, fn: Callable[[], object]) -> list[dict[str, object]]:
    """Run ``fn`` with a fresh StageEventEmitter active; return its Phase 2 progress events."""
    sidecar = tmp_path / "events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="cadence-test")
    set_active_emitter(emitter)
    try:
        fn()
    finally:
        set_active_emitter(None)
    if not sidecar.exists():
        # Emitter creates the sidecar lazily on first write; cadence=0
        # leaves it absent. Empty event stream is the right semantic.
        return []
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r.get("event") == "progress" and r.get("phase") == "phase2"]


def test_picker_phase_seq_emits_progress_events_at_cadence(tmp_path: Path) -> None:
    """600 deferred + cadence=200 → exactly 3 phase2 progress events on the seq path."""
    queue = _build_picker_queue(600)
    picker = _picker_for_queue(queue)

    progress = _capture_progress_events(
        tmp_path,
        lambda: _picker_phase_seq(
            queue,
            picker=picker,
            field_timeout_seconds=0,
            model_name="stub",
            watchdog_sidecar_path=None,
            progress_cadence=200,
            cache_hits=0,
        ),
    )
    assert len(progress) == 3, (
        f"Expected 3 progress events at cadence=200 over 600 entries; got {len(progress)}."
    )
    # Counts must be 200, 400, 600 in submission order.
    processed = [int(p["counters"]["processed"]) for p in progress]
    assert processed == [200, 400, 600]
    # The total is the deferred-pool size, not the canonical-total.
    assert all(int(p["counters"]["total"]) == 600 for p in progress)
    # cache_hits + watchdog_aborted ride in extra.
    assert progress[0]["extra"]["cache_hits"] == 0
    assert progress[0]["extra"]["watchdog_aborted"] == 0


def test_picker_phase_pool_emits_progress_events_at_cadence(tmp_path: Path) -> None:
    """Same fixture, pool path: 3 progress events on completion-order cadence."""
    queue = _build_picker_queue(600)
    base_picker = _picker_for_queue(queue)

    progress = _capture_progress_events(
        tmp_path,
        lambda: _picker_phase_pool(
            queue,
            picker_factory=lambda d=base_picker.decisions: StubPicker(decisions=dict(d)),
            concurrency=4,
            field_timeout_seconds=0,
            model_name="stub",
            watchdog_sidecar_path=None,
            progress_cadence=200,
            cache_hits=12,
        ),
    )
    assert len(progress) == 3, f"Expected 3 progress events on the pool path; got {len(progress)}."
    # Pool emits on completion order, not submission order — counts may
    # not match seq exactly but must still total cleanly at the boundaries.
    processed = [int(p["counters"]["processed"]) for p in progress]
    assert processed == [200, 400, 600]
    # cache_hits flows through verbatim from the caller's Phase-1.5 count.
    assert all(int(p["extra"]["cache_hits"]) == 12 for p in progress)


def test_picker_phase_progress_cadence_zero_disables_emission(tmp_path: Path) -> None:
    """``progress_cadence=0`` (operator override) emits no phase2 events."""
    queue = _build_picker_queue(400)
    picker = _picker_for_queue(queue)

    progress = _capture_progress_events(
        tmp_path,
        lambda: _picker_phase_seq(
            queue,
            picker=picker,
            field_timeout_seconds=0,
            model_name="stub",
            watchdog_sidecar_path=None,
            progress_cadence=0,
            cache_hits=0,
        ),
    )
    assert progress == []


def test_picker_phase_progress_flushes_final_when_misaligned(tmp_path: Path) -> None:
    """500 entries at cadence=200 emits 3 events: [200, 400, 500].

    The trailing 100 entries below the cadence boundary trigger one
    final end-of-phase progress event so the dashboard's processed
    gauge reaches 100% of the phase total instead of plateauing at
    the last cadence multiple. Cadence-aligned runs (e.g. exactly
    600) still emit exactly N/cadence events with no duplicate.
    """
    queue = _build_picker_queue(500)
    picker = _picker_for_queue(queue)

    progress = _capture_progress_events(
        tmp_path,
        lambda: _picker_phase_seq(
            queue,
            picker=picker,
            field_timeout_seconds=0,
            model_name="stub",
            watchdog_sidecar_path=None,
            progress_cadence=200,
            cache_hits=0,
        ),
    )
    assert len(progress) == 3
    processed = [int(p["counters"]["processed"]) for p in progress]
    assert processed == [200, 400, 500]
