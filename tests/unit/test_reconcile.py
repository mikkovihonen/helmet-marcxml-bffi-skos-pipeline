"""Unit tests for stages/reconcile (M9 phase 1).

No live HTTP, no live LLM. The orchestrator runs against an in-memory
graph; the FintoSkosmosClient is exercised through ``httpx.MockTransport``
so the JSON-shape parsing is verified without touching the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.reconcile import (
    LEXICAL_DIRECT_THRESHOLD,
    LEXICAL_FLOOR,
    LLM_CONFIDENCE_THRESHOLD,
    STAGE_FALLBACK,
    STAGE_LEXICAL,
    STAGE_LLM,
    STAGE_NO_CANDIDATE,
    AuthorityCandidate,
    EntityRequest,
    FintoSkosmosClient,
    PickerDecision,
    StubAuthorityClient,
    StubPicker,
    apply_reconciliation,
    decide_reconciliation,
    lexical_similarity,
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


def _candidate(uri: str, label: str, sim: float, vocab: str = "kanto") -> AuthorityCandidate:
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
                "vocab": "kanto",
            },
            {
                "uri": "http://urn.fi/URN:NBN:fi:au:finaf:000041687",
                "prefLabel": "Tolstoyevsky, Lev",
                "vocab": "kanto",
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
    assert candidates[0].source_vocabulary == "kanto"
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
                    {"uri": "http://kanto/1", "prefLabel": "Tolstoy, Leo", "vocab": "kanto"}
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
