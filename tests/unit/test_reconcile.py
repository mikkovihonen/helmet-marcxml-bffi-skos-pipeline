"""Unit tests for stages/reconcile (M9 phase 1).

No live HTTP, no live LLM. The orchestrator runs against an in-memory
graph; the FintoSkosmosClient is exercised through ``httpx.MockTransport``
so the JSON-shape parsing is verified without touching the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.reconcile import (
    ALL_AUTHORITY_KINDS,
    LEXICAL_DIRECT_THRESHOLD,
    LEXICAL_FLOOR,
    LLM_CONFIDENCE_THRESHOLD,
    PICKER_MAX_VALIDATION_RETRIES,
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
    PickerDecision,
    StubAuthorityClient,
    StubPicker,
    _iter_subject_requests,
    apply_reconciliation,
    decide_reconciliation,
    lexical_similarity,
    picker_prompt_hash,
    picker_prompt_text,
    reconcile_one,
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
