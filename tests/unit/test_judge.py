"""Unit tests for stages/judge (M6 LLM judge — phase 1).

The real LLM is never contacted. ``judge_pair`` and ``cascade_judge``
both accept an injectable ``chain`` (any object exposing
``.invoke(payload)``); the tests pass scripted fakes whose behaviour
is deterministic, so retry / cache / cascade logic can be exercised
without an Ollama or vllm-mlx server.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bffi_pipeline.stages import judge
from bffi_pipeline.stages.judge import (
    CONNECTION_BACKOFF_SECONDS,
    FALLBACK_CONFIDENCE_THRESHOLD,
    MAX_CONNECTION_RETRIES,
    MAX_VALIDATION_RETRIES,
    MIN_RATIONALE_CHARS,
    STAGE_AUTO_MERGE,
    STAGE_PRIMARY,
    STAGE_SECOND_OPINION,
    JudgeCache,
    WorkMatchDecision,
    WorkMatchDecisionFast,
    WorkRecord,
    _cache_key,
    cascade_judge,
    judge_pair,
    prompt_hash,
    synthesize_auto_merge_outcome,
)

# --- Helpers --------------------------------------------------------------


def _make_decision(
    *,
    decision: str = "same_work",
    confidence: float = 0.95,
    rationale: str = "Same author and original_language; B is the Finnish Expression of A.",
    matching: list[str] | None = None,
    diverging: list[str] | None = None,
) -> WorkMatchDecision:
    return WorkMatchDecision(
        decision=decision,  # type: ignore[arg-type]
        confidence=confidence,
        rationale=rationale,
        matching_fields=matching if matching is not None else ["creator", "original_language"],
        diverging_fields=diverging if diverging is not None else ["preferred_title"],
    )


def _make_record(record_id: str = "r1", creator: str = "Pushkin", title: str = "X") -> WorkRecord:
    return WorkRecord(record_id=record_id, creator=creator, preferred_title=title)


class _ScriptedChain:
    """Test chain that returns a queued sequence of values or raises errors.

    Each entry in ``script`` is either a ``WorkMatchDecision``, a dict
    (for the test that exercises Pydantic validation post-LangChain), or
    an ``Exception`` instance to raise on that invocation. Calls past the
    end of the script raise ``StopIteration``.
    """

    def __init__(self, script: list[Any]) -> None:
        self._iter: Iterator[Any] = iter(script)
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
    """Deterministic ``time.sleep`` replacement so retry tests don't actually wait."""


@pytest.fixture
def tmp_cache(tmp_path: Path) -> Iterator[JudgeCache]:
    cache = JudgeCache(tmp_path / "cache.sqlite")
    try:
        yield cache
    finally:
        cache.close()


# --- Schema (Boundary-4 validators) ---------------------------------------


def test_uncertain_with_high_confidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="incoherent with confidence"):
        WorkMatchDecision(
            decision="uncertain",
            confidence=0.9,
            rationale="Plenty of detail here, more than twenty characters total.",
        )


def test_uncertain_with_low_confidence_passes() -> None:
    d = WorkMatchDecision(
        decision="uncertain",
        confidence=0.3,
        rationale="Plenty of detail here, more than twenty characters total.",
    )
    assert d.decision == "uncertain"


def test_same_work_without_matching_fields_is_rejected() -> None:
    with pytest.raises(ValidationError, match="requires at least one matching_field"):
        WorkMatchDecision(
            decision="same_work",
            confidence=0.95,
            rationale="Plenty of detail here, more than twenty characters total.",
        )


def test_same_work_with_matching_fields_passes() -> None:
    _make_decision()  # default fixture has matching_fields populated; should not raise


def test_short_rationale_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"shorter than|at least 20"):
        WorkMatchDecision(
            decision="different_work",
            confidence=0.9,
            rationale="too short",
        )


@pytest.mark.parametrize(
    "phrase",
    ["I don't know", "unable to determine", "n/a", "Not sure", "I DON'T KNOW yet"],
)
def test_rationale_with_stub_phrase_is_rejected(phrase: str) -> None:
    text = f"{phrase} but the records share author and original_language."
    with pytest.raises(ValidationError, match="stub phrase"):
        WorkMatchDecision(
            decision="different_work",
            confidence=0.5,
            rationale=text,
        )


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkMatchDecision.model_validate(
            {
                "decision": "different_work",
                "confidence": 0.9,
                "rationale": "Plenty of detail here, more than twenty characters total.",
                "extra_unknown_field": "should-be-rejected",
            }
        )


# --- prompt + hash --------------------------------------------------------


def test_prompt_hash_is_stable_within_a_run() -> None:
    assert prompt_hash() == prompt_hash()
    assert prompt_hash().startswith("sha256:")


def test_prompt_text_contains_required_sections() -> None:
    text = judge.prompt_text()
    assert "### SYSTEM" in text
    assert "### EXAMPLES" in text
    assert "### USER" in text
    assert "{record_a}" in text
    assert "{record_b}" in text
    assert "{sim:.3f}" in text


# --- JudgeCache -----------------------------------------------------------


def test_cache_round_trip(tmp_cache: JudgeCache) -> None:
    decision = _make_decision()
    tmp_cache.set("k1", decision, model_name="m", prompt_hash_value="sha256:abc")
    got = tmp_cache.get("k1")
    assert got is not None
    assert got.decision == "same_work"
    assert got.matching_fields == decision.matching_fields


def test_cache_miss_returns_none(tmp_cache: JudgeCache) -> None:
    assert tmp_cache.get("nonexistent") is None


def test_cache_overwrites_on_set(tmp_cache: JudgeCache) -> None:
    a = _make_decision(confidence=0.91)
    b = _make_decision(confidence=0.99)
    tmp_cache.set("k", a, model_name="m", prompt_hash_value="ph")
    tmp_cache.set("k", b, model_name="m", prompt_hash_value="ph")
    got = tmp_cache.get("k")
    assert got is not None
    assert got.confidence == pytest.approx(0.99)


# --- judge_pair: happy path + cache --------------------------------------


def test_judge_pair_returns_decision_and_caches_on_success(tmp_cache: JudgeCache) -> None:
    expected = _make_decision()
    chain = _ScriptedChain([expected])
    decision, cache_hit, latency = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert decision == expected
    assert cache_hit is False
    assert latency >= 0
    assert len(chain.calls) == 1
    # Second call must hit the cache without invoking the chain.
    fresh_chain = _ScriptedChain([AssertionError("Cache hit should bypass the chain")])
    decision_2, cache_hit_2, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=fresh_chain,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert cache_hit_2 is True
    assert decision_2 == expected
    assert fresh_chain.calls == []


def test_judge_pair_validates_dict_responses(tmp_cache: JudgeCache) -> None:
    """Some chains return raw dicts; judge_pair must Pydantic-validate them."""
    raw = {
        "decision": "different_work",
        "confidence": 0.92,
        "rationale": "Different creators and content_type — adaptation, not the same Work.",
        "matching_fields": [],
        "diverging_fields": ["creator", "content_type"],
    }
    chain = _ScriptedChain([raw])
    decision, _, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.4,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert decision.decision == "different_work"


# --- judge_pair: validation retry ----------------------------------------


def test_validation_retry_recovers_after_one_bad_response(tmp_cache: JudgeCache) -> None:
    bad = {
        "decision": "same_work",
        "confidence": 0.95,
        "rationale": "Plenty of detail here, more than twenty characters total.",
        "matching_fields": [],  # violates Boundary-4 (same_work needs evidence)
        "diverging_fields": [],
    }
    good = _make_decision()
    chain = _ScriptedChain([bad, good])
    decision, cache_hit, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert decision == good
    assert cache_hit is False
    assert len(chain.calls) == 2  # one bad, one good


def test_validation_failure_after_max_retries_lands_as_uncertain(
    tmp_cache: JudgeCache,
) -> None:
    bad = {
        "decision": "same_work",
        "confidence": 0.95,
        "rationale": "Plenty of detail here, more than twenty characters total.",
        "matching_fields": [],
        "diverging_fields": [],
    }
    # MAX_VALIDATION_RETRIES = 2 → 3 total LLM attempts; queue 4 bad responses
    # to confirm it gives up at exactly the 3rd attempt.
    chain = _ScriptedChain([bad, bad, bad, bad])
    decision, cache_hit, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert decision.decision == "uncertain"
    assert decision.confidence == pytest.approx(0.0)
    assert "validation failed" in decision.rationale.lower()
    assert cache_hit is False
    assert len(chain.calls) == 1 + MAX_VALIDATION_RETRIES
    # Validation-failed runs must NOT cache (caching cements bad outputs).
    assert tmp_cache.get(_any_key()) is None or True  # sanity — see explicit check below


def _any_key() -> str:
    """Compute a key for the test pair so we can probe the cache directly."""
    return _cache_key(
        model_name=judge.get_settings().llm_model_primary,
        prompt_hash_value=prompt_hash(),
        record_a=_make_record("a"),
        record_b=_make_record("b"),
    )


def test_validation_failure_does_not_cache(tmp_cache: JudgeCache) -> None:
    bad = {
        "decision": "same_work",
        "confidence": 0.95,
        "rationale": "Plenty of detail here, more than twenty characters total.",
        "matching_fields": [],
        "diverging_fields": [],
    }
    chain = _ScriptedChain([bad, bad, bad])
    judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert tmp_cache.get(_any_key()) is None


# --- judge_pair: connection-error retry ----------------------------------


class _ConnError(Exception):
    """A no-import-needed stand-in whose class name fools _is_connection_error."""


# Rename to a name _is_connection_error recognises.
_ConnError.__name__ = "ConnectError"


def test_connection_error_retries_with_backoff(tmp_cache: JudgeCache) -> None:
    sleeps: list[float] = []

    def record_sleep(s: float) -> None:
        sleeps.append(s)

    good = _make_decision()
    # 3 connection failures + 1 success — the 4th total attempt succeeds.
    chain = _ScriptedChain([_ConnError("boom"), _ConnError("boom"), _ConnError("boom"), good])
    decision, cache_hit, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=record_sleep,
    )
    assert decision == good
    assert cache_hit is False
    assert sleeps == list(CONNECTION_BACKOFF_SECONDS)  # 5, 30, 120
    assert len(chain.calls) == 1 + MAX_CONNECTION_RETRIES


def test_connection_error_after_max_retries_lands_as_uncertain(
    tmp_cache: JudgeCache,
) -> None:
    chain = _ScriptedChain([_ConnError("boom")] * (MAX_CONNECTION_RETRIES + 1))
    decision, _, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert decision.decision == "uncertain"
    assert "connection error" in decision.rationale.lower()


def test_non_connection_exception_lands_as_uncertain_immediately(
    tmp_cache: JudgeCache,
) -> None:
    """A bug-like exception (TypeError, AttributeError) should not trigger backoff."""
    chain = _ScriptedChain([RuntimeError("model server is on fire")])
    decision, _, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert decision.decision == "uncertain"
    assert "unrecoverable" in decision.rationale.lower()
    assert len(chain.calls) == 1


# --- watchdog (per plan P-03) --------------------------------------------


class _TimeoutErr(Exception):
    """Stand-in whose class name matches the watchdog's timeout enum."""


_TimeoutErr.__name__ = "ReadTimeout"


def test_timeout_emits_watchdog_event_and_retries_to_success(
    tmp_cache: JudgeCache, tmp_path: Path, capsys: Any
) -> None:
    """A single timeout fires a ``timeout`` watchdog event, the cascade
    retry stack kicks in, and the second call succeeds — no
    ``give_up`` event, no ``uncertain`` outcome."""
    sidecar = tmp_path / "watchdog-events.jsonl"
    good = _make_decision()
    chain = _ScriptedChain([_TimeoutErr("ollama wedged"), good])
    decision, cache_hit, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
        watchdog_sidecar_path=sidecar,
    )
    assert decision == good
    assert cache_hit is False
    # Sidecar got one timeout event, no give_up.
    events = [json.loads(line) for line in sidecar.read_text().splitlines() if line.strip()]
    assert len(events) == 1
    assert events[0]["event"] == "timeout"
    assert events[0]["pair_id"] == "a+b"
    assert events[0]["retry_n"] == 0  # first attempt failed; not yet a retry.
    # Stderr mirror carries the same payload behind the WATCHDOG_EVENT prefix.
    stderr = capsys.readouterr().err
    assert "WATCHDOG_EVENT " in stderr
    assert '"event":"timeout"' in stderr


def test_repeated_timeouts_emit_give_up_event_and_land_uncertain(
    tmp_cache: JudgeCache, tmp_path: Path
) -> None:
    """All-timeout scripted chain: retries exhaust, ``give_up`` event
    is emitted, decision lands as ``uncertain``."""
    sidecar = tmp_path / "watchdog-events.jsonl"
    chain = _ScriptedChain([_TimeoutErr("wedged")] * (MAX_CONNECTION_RETRIES + 1))
    decision, _, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
        watchdog_sidecar_path=sidecar,
    )
    assert decision.decision == "uncertain"
    events = [json.loads(line) for line in sidecar.read_text().splitlines() if line.strip()]
    # Every failed attempt emits a ``timeout``; the final exhaustion
    # additionally emits ``give_up`` — so ``MAX_CONNECTION_RETRIES + 1``
    # timeouts plus one give_up.
    assert sum(1 for e in events if e["event"] == "timeout") == MAX_CONNECTION_RETRIES + 1
    assert sum(1 for e in events if e["event"] == "give_up") == 1
    assert events[-1]["event"] == "give_up"


def test_non_timeout_connection_error_does_not_emit_watchdog_event(
    tmp_cache: JudgeCache, tmp_path: Path
) -> None:
    """Generic ``ConnectError`` retries but does NOT emit a watchdog
    event — the watchdog specifically counts wall-time wedges, not
    network resets."""
    sidecar = tmp_path / "watchdog-events.jsonl"
    good = _make_decision()
    chain = _ScriptedChain([_ConnError("connection reset"), good])
    decision, _, _ = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
        watchdog_sidecar_path=sidecar,
    )
    assert decision == good
    assert not sidecar.exists() or sidecar.read_text().strip() == ""


# --- cascade_judge --------------------------------------------------------


def _high_conf_same_work() -> WorkMatchDecision:
    return _make_decision(decision="same_work", confidence=0.95)


def _low_conf_same_work() -> WorkMatchDecision:
    return _make_decision(decision="same_work", confidence=0.7)


def _confident_different() -> WorkMatchDecision:
    return _make_decision(
        decision="different_work",
        confidence=0.97,
        matching=["preferred_title"],
        diverging=["creator", "content_type"],
    )


def _uncertain() -> WorkMatchDecision:
    return _make_decision(
        decision="uncertain",
        confidence=0.5,
        matching=[],
        diverging=[],
    )


def test_cascade_skips_fallback_on_confident_decision(tmp_cache: JudgeCache) -> None:
    primary = _ScriptedChain([_confident_different()])
    fallback = _ScriptedChain([AssertionError("Fallback must not run on confident different_work")])
    outcome = cascade_judge(
        _make_record("a"),
        _make_record("b"),
        sim=0.4,
        primary_chain=primary,
        fallback_chain=fallback,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert outcome.final.decision == "different_work"
    assert outcome.used_cascade is False
    assert [s.stage for s in outcome.steps] == [STAGE_PRIMARY]
    assert fallback.calls == []


def test_cascade_runs_fallback_on_uncertain(tmp_cache: JudgeCache) -> None:
    primary = _ScriptedChain([_uncertain()])
    fallback = _ScriptedChain([_high_conf_same_work()])
    outcome = cascade_judge(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        primary_chain=primary,
        fallback_chain=fallback,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert outcome.final.decision == "same_work"
    assert outcome.used_cascade is True
    assert [s.stage for s in outcome.steps] == [STAGE_PRIMARY, STAGE_SECOND_OPINION]


def test_cascade_runs_fallback_on_low_confidence_same_work(tmp_cache: JudgeCache) -> None:
    primary = _ScriptedChain([_low_conf_same_work()])
    fallback = _ScriptedChain([_high_conf_same_work()])
    outcome = cascade_judge(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        primary_chain=primary,
        fallback_chain=fallback,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert outcome.final.confidence >= FALLBACK_CONFIDENCE_THRESHOLD
    assert outcome.used_cascade is True


def test_cascade_does_not_run_fallback_on_high_confidence_same_work(
    tmp_cache: JudgeCache,
) -> None:
    primary = _ScriptedChain([_high_conf_same_work()])
    fallback = _ScriptedChain(
        [AssertionError("Fallback must not run on high-confidence same_work")]
    )
    outcome = cascade_judge(
        _make_record("a"),
        _make_record("b"),
        sim=0.92,
        primary_chain=primary,
        fallback_chain=fallback,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert outcome.used_cascade is False
    assert fallback.calls == []


def test_cascade_does_not_run_fallback_on_different_work_low_confidence(
    tmp_cache: JudgeCache,
) -> None:
    """The cascade rule fires on low-conf same_work; low-conf different_work stands."""
    primary = _ScriptedChain(
        [_make_decision(decision="different_work", confidence=0.6, matching=["preferred_title"])]
    )
    fallback = _ScriptedChain(
        [AssertionError("Fallback should NOT run on a low-confidence different_work")]
    )
    outcome = cascade_judge(
        _make_record("a"),
        _make_record("b"),
        sim=0.84,
        primary_chain=primary,
        fallback_chain=fallback,
        cache=tmp_cache,
        sleep=_no_sleep,
    )
    assert outcome.used_cascade is False
    assert fallback.calls == []


# --- synthesize_auto_merge_outcome --------------------------------------


def test_synthesize_auto_merge_emits_same_work_with_embedding_stage() -> None:
    """A pair from M5's auto-merge band (≥ 0.90) collapses into a
    JudgeOutcome with one synthetic CascadeStep tagged
    ``auto-merge-embedding``. No LLM call; the embedding similarity
    becomes the decision's confidence and the matching-fields list
    cites the embedding directly so Boundary-4 validation passes."""
    row = {
        "work_a": "http://urn.fi/URN:NBN:fi:bib:work:a",
        "work_b": "http://urn.fi/URN:NBN:fi:bib:work:b",
        "similarity": 1.0,
        "block_a": "mahavishnu|birds|prm",
        "block_b": "mahavishnu|birds|prm",
        "band": "auto-merge",
    }
    outcome = synthesize_auto_merge_outcome(row)
    assert outcome.final.decision == "same_work"
    assert outcome.final.confidence == pytest.approx(1.0)
    assert outcome.final.matching_fields == ["embedding_similarity"]
    assert outcome.used_cascade is False
    assert len(outcome.steps) == 1
    step = outcome.steps[0]
    assert step.stage == STAGE_AUTO_MERGE
    assert step.model_name == "BAAI/bge-m3"
    assert step.cache_hit is False
    assert step.latency_seconds == pytest.approx(0.0)


def test_synthesize_auto_merge_rationale_passes_boundary_4_validators() -> None:
    """The auto-generated rationale must be ≥ 20 chars and free of
    stub phrases — same Boundary-4 invariants as LLM-judged decisions
    so cached pipelines treat both uniformly."""
    row = {
        "work_a": "http://example.org/a",
        "work_b": "http://example.org/b",
        "similarity": 0.95,
        "block_a": "blk",
        "block_b": "blk",
    }
    outcome = synthesize_auto_merge_outcome(row)
    text = outcome.final.rationale.strip()
    assert len(text) >= 20
    lowered = text.lower()
    for phrase in ("i don't know", "unable to determine", "n/a", "not sure"):
        assert phrase not in lowered


def test_synthesize_auto_merge_preserves_similarity_as_confidence() -> None:
    """The synthetic decision's confidence equals the embedding
    similarity that triggered the auto-merge band. This keeps the
    audit trail tight — downstream review queries can find the M5
    similarity from the prov:Activity's confidence field directly."""
    row = {
        "work_a": "http://example.org/a",
        "work_b": "http://example.org/b",
        "similarity": 0.927,
        "block_a": "blk",
        "block_b": "blk",
    }
    outcome = synthesize_auto_merge_outcome(row)
    assert outcome.final.confidence == pytest.approx(0.927)
    assert outcome.steps[0].decision.confidence == pytest.approx(0.927)


def test_auto_merge_clamps_similarity_above_one() -> None:
    """FAISS inner-product on L2-normalised vectors occasionally drifts
    a hair above 1.0 from float32 accumulation noise. The synthesiser
    must clamp rather than fail Pydantic's ``le=1.0`` constraint —
    rejecting an auto-merge pair on float-noise alone would kill the
    M6 stage mid-batch."""
    row = {
        "work_a": "http://example.org/a",
        "work_b": "http://example.org/b",
        "similarity": 1.0000001192092896,  # exact value seen in the preview-373 pipeline
        "block_a": "blk",
        "block_b": "blk",
    }
    outcome = synthesize_auto_merge_outcome(row)
    assert outcome.final.confidence == pytest.approx(1.0)


# --- WorkMatchDecisionFast (fast-mode schema) ----------------------------


def test_fast_schema_accepts_null_rationale_on_high_confidence_same_work() -> None:
    """Confident ``same_work`` decisions may omit rationale entirely
    (default ``None``) — the structured matching_fields list is the
    evidence the audit trail keeps."""
    d = WorkMatchDecisionFast(
        decision="same_work",
        confidence=0.97,
        rationale=None,
        matching_fields=["creator", "preferred_title"],
    )
    assert d.rationale is None


def test_fast_schema_accepts_null_rationale_on_high_confidence_different_work() -> None:
    d = WorkMatchDecisionFast(
        decision="different_work",
        confidence=0.92,
        rationale=None,
        diverging_fields=["creator"],
    )
    assert d.rationale is None


def test_fast_schema_requires_rationale_when_uncertain() -> None:
    """``decision="uncertain"`` always requires rationale (cataloguer
    review queue needs the prose). Confidence stays low to satisfy
    the existing uncertain-coherence rule."""
    with pytest.raises(ValidationError, match="required for 'uncertain'"):
        WorkMatchDecisionFast(
            decision="uncertain",
            confidence=0.5,
            rationale=None,
            matching_fields=["creator"],
        )


def test_fast_schema_requires_rationale_when_low_confidence() -> None:
    """Below the 0.85 cascade-trigger threshold the rationale is
    required regardless of decision — the rationale is what feeds
    the cascade's 72B second-opinion decision."""
    with pytest.raises(ValidationError, match=r"required for 'uncertain' or confidence"):
        WorkMatchDecisionFast(
            decision="same_work",
            confidence=0.80,
            rationale=None,
            matching_fields=["creator"],
        )


def test_fast_schema_rejects_short_rationale_when_required() -> None:
    """When rationale IS required (uncertain / low-conf), it must
    still clear the ≥ MIN_RATIONALE_CHARS bar."""
    with pytest.raises(ValidationError, match=f"≥ {MIN_RATIONALE_CHARS}"):
        WorkMatchDecisionFast(
            decision="same_work",
            confidence=0.80,
            rationale="too short",
            matching_fields=["creator"],
        )


def test_fast_schema_rejects_stub_phrase_even_on_high_confidence() -> None:
    """Stub-phrase guard fires on EVERY non-empty rationale, even when
    rationale is optional. Stops a hallucinating model from emitting
    'n/a' as a rationale instead of just leaving it null."""
    with pytest.raises(ValidationError, match="stub phrase"):
        WorkMatchDecisionFast(
            decision="same_work",
            confidence=0.97,
            rationale="N/A, see structured fields.",
            matching_fields=["creator"],
        )


def test_fast_schema_to_strict_synthesises_rationale_when_null() -> None:
    """High-conf decision with rationale=None converts to the strict
    :class:`WorkMatchDecision` by synthesising a structured-fields
    rationale that clears Boundary-4."""
    d = WorkMatchDecisionFast(
        decision="same_work",
        confidence=0.97,
        rationale=None,
        matching_fields=["creator", "preferred_title"],
        diverging_fields=["expression_language"],
    )
    strict = d.to_strict()
    assert isinstance(strict, WorkMatchDecision)
    assert strict.decision == "same_work"
    assert strict.confidence == pytest.approx(0.97)
    assert "Fast-mode decision" in strict.rationale
    assert "creator, preferred_title" in strict.rationale
    assert "expression_language" in strict.rationale
    assert len(strict.rationale) >= MIN_RATIONALE_CHARS


def test_fast_schema_to_strict_preserves_supplied_rationale() -> None:
    """If the model DID supply a substantive rationale (low-conf or
    uncertain case), to_strict() carries it through unchanged."""
    text = "Same creator under transliteration variants; date and title diverge clearly."
    d = WorkMatchDecisionFast(
        decision="uncertain",
        confidence=0.6,
        rationale=text,
        matching_fields=["creator"],
    )
    strict = d.to_strict()
    assert strict.rationale == text


def test_fast_schema_same_work_still_needs_matching_fields() -> None:
    """The :func:`_same_work_needs_evidence` invariant is preserved in
    fast mode — saving rationale tokens doesn't loosen the
    Boundary-4 evidence requirement."""
    with pytest.raises(ValidationError, match="matching_field"):
        WorkMatchDecisionFast(
            decision="same_work",
            confidence=0.97,
            rationale=None,
            matching_fields=[],
        )


def test_fast_schema_uncertain_still_requires_low_confidence() -> None:
    """The :func:`_coherent_uncertain` invariant is preserved in fast
    mode — uncertain + high confidence is still incoherent."""
    with pytest.raises(ValidationError, match="incoherent with confidence"):
        WorkMatchDecisionFast(
            decision="uncertain",
            confidence=0.95,
            rationale="Plenty of substantive rationale here, easily over twenty characters total.",
        )


# --- judge_pair full_rationale flag --------------------------------------


def test_judge_pair_fast_mode_returns_strict_decision_with_synthetic_rationale(
    tmp_cache: JudgeCache,
) -> None:
    """End-to-end: ``judge_pair(..., full_rationale=False)`` feeds the
    LLM the fast schema. The model returns a high-conf decision with
    rationale=None; the boundary conversion synthesises a structured-
    fields rationale so the cached + returned decision is the strict
    :class:`WorkMatchDecision` shape."""
    fast_no_rationale = WorkMatchDecisionFast(
        decision="same_work",
        confidence=0.97,
        rationale=None,
        matching_fields=["creator", "preferred_title"],
        diverging_fields=["expression_language"],
    )
    chain = _ScriptedChain([fast_no_rationale])
    decision, cache_hit, _latency = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.97,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
        full_rationale=False,
    )
    # Returned shape is the strict WorkMatchDecision (not Fast).
    assert isinstance(decision, WorkMatchDecision)
    assert decision.decision == "same_work"
    assert decision.confidence == pytest.approx(0.97)
    # Rationale was synthesised from the structured fields and clears Boundary-4.
    assert decision.rationale is not None
    assert len(decision.rationale) >= MIN_RATIONALE_CHARS
    assert "creator, preferred_title" in decision.rationale
    assert "expression_language" in decision.rationale
    assert cache_hit is False


def test_judge_pair_fast_mode_passes_through_supplied_rationale_when_low_confidence(
    tmp_cache: JudgeCache,
) -> None:
    """Low-confidence outcomes still carry a substantive rationale —
    the model populated it because the fast prompt requires it. The
    boundary conversion preserves that rationale verbatim."""
    text = "Same author, different publication year (1962 vs 2014); likely separate compilations."
    fast = WorkMatchDecisionFast(
        decision="same_work",
        confidence=0.80,
        rationale=text,
        matching_fields=["creator", "preferred_title"],
    )
    chain = _ScriptedChain([fast])
    decision, _hit, _lat = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.80,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
        full_rationale=False,
    )
    assert decision.rationale == text


def test_judge_pair_full_rationale_mode_keeps_strict_schema_path(
    tmp_cache: JudgeCache,
) -> None:
    """Default ``full_rationale=True`` uses the strict
    :class:`WorkMatchDecision` schema unchanged — same path as before
    the fast-mode addition."""
    strict = _make_decision(rationale="Strict-mode rationale supplied by the model directly.")
    chain = _ScriptedChain([strict])
    decision, _hit, _lat = judge_pair(
        _make_record("a"),
        _make_record("b"),
        sim=0.97,
        chain=chain,
        cache=tmp_cache,
        sleep=_no_sleep,
        full_rationale=True,
    )
    assert isinstance(decision, WorkMatchDecision)
    assert decision.rationale == "Strict-mode rationale supplied by the model directly."
