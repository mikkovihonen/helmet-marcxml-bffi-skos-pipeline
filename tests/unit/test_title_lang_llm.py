"""Unit tests for ``bffi_pipeline.title_lang_llm``.

The LangChain stack is never loaded — every test injects either the
``StubTitleLangDetector`` or a hand-rolled scripted chain object.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from bffi_pipeline.title_lang import TaggedSegment, tag_title
from bffi_pipeline.title_lang_llm import (
    TITLE_LANG_MAX_VALIDATION_RETRIES,
    LangChainTitleLangDetector,
    StubTitleLangDetector,
    TitleLangDecision,
    TitleLangSegment,
    title_lang_prompt_hash,
    title_lang_prompt_text,
)

# --- Pydantic schema -----------------------------------------------------


def test_title_lang_decision_valid_minimal_shape() -> None:
    d = TitleLangDecision(
        segments=[TitleLangSegment(text="Sota ja rauha", lang="fi")],
        rationale="Single Finnish segment; vocabulary is unambiguously Finnish.",
    )
    assert d.segments[0].lang == "fi"


def test_title_lang_decision_rejects_short_rationale() -> None:
    with pytest.raises(ValidationError):
        TitleLangDecision(
            segments=[TitleLangSegment(text="x", lang="fi")],
            rationale="too short",
        )


def test_title_lang_decision_rejects_stub_phrase_rationale() -> None:
    with pytest.raises(ValidationError):
        TitleLangDecision(
            segments=[TitleLangSegment(text="x", lang="fi")],
            rationale="I don't know which language this is, sorry.",
        )


def test_title_lang_decision_extra_fields_rejected() -> None:
    """``extra=forbid`` blocks the LLM from sneaking unrecognised fields through."""
    with pytest.raises(ValidationError):
        TitleLangDecision.model_validate(
            {
                "segments": [{"text": "x", "lang": "fi"}],
                "rationale": "Plenty of rationale text here that exceeds twenty.",
                "secret_extra": True,
            }
        )


def test_title_lang_segment_lang_can_be_null() -> None:
    s = TitleLangSegment(text="Tšarka", lang=None)
    assert s.lang is None


# --- Prompt loading + hash -----------------------------------------------


def test_title_lang_prompt_text_loads() -> None:
    text = title_lang_prompt_text()
    assert "### SYSTEM" in text
    assert "### EXAMPLES" in text
    assert "### USER" in text


def test_title_lang_prompt_hash_is_stable_within_a_run() -> None:
    h1 = title_lang_prompt_hash()
    h2 = title_lang_prompt_hash()
    assert h1 == h2
    assert h1.startswith("sha256:")


# --- Stub detector -------------------------------------------------------


def test_stub_detector_uses_wired_decision() -> None:
    title = "Tšarka : the Russian charka = venäläinen tšarkka = russkaja tšarka"
    decision = TitleLangDecision(
        segments=[
            TitleLangSegment(text="Tšarka : the Russian charka", lang="en"),
            TitleLangSegment(text="venäläinen tšarkka", lang="fi"),
            TitleLangSegment(text="russkaja tšarka", lang="ru"),
        ],
        rationale="LLM split on ' = ' and aligned segments by vocabulary signals.",
    )
    stub = StubTitleLangDetector(decisions={title: decision})
    out = stub.detect(title=title, candidates=frozenset({"fi", "sv", "en", "ru"}))
    assert [s.lang for s in out.segments] == ["en", "fi", "ru"]


def test_stub_detector_falls_through_when_unwired() -> None:
    stub = StubTitleLangDetector()
    out = stub.detect(title="Unknown", candidates=frozenset({"fi"}))
    assert len(out.segments) == 1
    assert out.segments[0].lang is None


# --- Cascade integration via tag_title ----------------------------------


def test_cascade_fires_only_when_lingua_collapses_on_multi_segment_title() -> None:
    """The Tšarka pattern: lingua maps all 3 Latin segments to Finnish.
    Without the LLM detector, the collapse heuristic emits one full-string
    segment. With the LLM detector, we get the LLM's per-segment splits."""
    title = (
        "Tšarka : the Russian charka : the silver vodka cup of the Romavov era = "
        "venäläinen tšarkka : hopeinen votkakuppi Romanovien ajalta = "
        "russkaja tšarka : vo vremena Romanovyh : 1613-1917"
    )
    candidates = frozenset({"fi", "sv", "en", "ru"})

    # Without LLM detector → single segment via collapse fallback.
    no_llm = tag_title(title, candidates)
    assert len(no_llm) == 1

    # With LLM detector that returns the right splits → 3 segments.
    decision = TitleLangDecision(
        segments=[
            TitleLangSegment(
                text="Tšarka : the Russian charka : the silver vodka cup of the Romavov era",
                lang="en",
            ),
            TitleLangSegment(
                text="venäläinen tšarkka : hopeinen votkakuppi Romanovien ajalta",
                lang="fi",
            ),
            TitleLangSegment(
                text="russkaja tšarka : vo vremena Romanovyh : 1613-1917",
                lang="ru",
            ),
        ],
        rationale=(
            "Split on ' = '. Segment 1 has English function words; segment 2 is "
            "Finnish vocabulary; segment 3 is romanized Russian."
        ),
    )
    detector = StubTitleLangDetector(decisions={title: decision})
    with_llm = tag_title(title, candidates, llm_detector=detector)
    assert [s.lang for s in with_llm] == ["en", "fi", "ru"]
    assert with_llm[0].text.startswith("Tšarka")
    assert with_llm[2].text.startswith("russkaja")


def test_cascade_does_not_fire_on_clean_single_language_titles() -> None:
    """If Lingua confidently picks a language for a single-segment title, we
    don't burn an LLM call on it."""

    @dataclass
    class _AssertNotCalled:
        def detect(self, *, title: str, candidates: frozenset[str]) -> TitleLangDecision:
            raise AssertionError(f"detector should not have been called for {title!r}")

    out = tag_title(
        "Aatelisrosvo Dubrovskij",
        frozenset({"fi", "sv", "en", "ru"}),
        llm_detector=_AssertNotCalled(),
    )
    assert out == [TaggedSegment(text="Aatelisrosvo Dubrovskij", lang="fi")]


def test_cascade_does_not_fire_when_lingua_already_distinguishes_segments() -> None:
    """Latin/Cyrillic split surfaces two distinct languages already; the LLM
    isn't needed for that case."""

    @dataclass
    class _AssertNotCalled:
        def detect(self, *, title: str, candidates: frozenset[str]) -> TitleLangDecision:
            raise AssertionError(f"detector should not have been called for {title!r}")

    out = tag_title(
        "Eti punktualnyje nemtsy = Эти пунктуальные немцы",
        frozenset({"fi", "sv", "en", "ru"}),
        llm_detector=_AssertNotCalled(),
    )
    # Cyrillic side tags 'ru'; Latin side may tag 'fi' (mis-classified) or
    # stay None. Either way we have at least one ru and the LLM wasn't used.
    assert any(s.lang == "ru" for s in out)


# --- LangChainTitleLangDetector retry logic -----------------------------


@dataclass
class _ScriptedChain:
    """Minimal chain-shaped object for testing retry / cascade logic."""

    responses: list[object]  # mix of TitleLangDecision and Exception instances
    calls: list[dict[str, object]]

    def invoke(self, payload: dict[str, object]) -> object:
        self.calls.append(payload)
        if not self.responses:
            raise AssertionError("no more scripted responses")
        head = self.responses.pop(0)
        if isinstance(head, Exception):
            raise head
        return head


def _no_sleep(_seconds: float) -> None:
    """Skip retry backoff in tests."""


def _good_decision() -> TitleLangDecision:
    return TitleLangDecision(
        segments=[TitleLangSegment(text="Sota ja rauha", lang="fi")],
        rationale="Unambiguously Finnish vocabulary; single segment.",
    )


def test_lang_chain_detector_returns_decision_on_happy_path() -> None:
    chain = _ScriptedChain(responses=[_good_decision()], calls=[])
    detector = LangChainTitleLangDetector(chain=chain, sleep=_no_sleep)
    out = detector.detect(title="Sota ja rauha", candidates=frozenset({"fi", "sv", "en"}))
    assert out.segments[0].lang == "fi"
    assert len(chain.calls) == 1


def test_lang_chain_detector_retries_validation_failures_then_recovers() -> None:
    """A dict response that fails post-parse validation triggers a retry;
    the next attempt with a valid decision succeeds."""
    bad_dict = {"segments": [], "rationale": "too short"}  # 0 segments, short rationale
    chain = _ScriptedChain(responses=[bad_dict, _good_decision()], calls=[])
    detector = LangChainTitleLangDetector(chain=chain, sleep=_no_sleep)
    out = detector.detect(title="Sota ja rauha", candidates=frozenset({"fi"}))
    assert out.segments[0].lang == "fi"
    assert len(chain.calls) == 2  # one failed validation + one success


def test_lang_chain_detector_falls_through_after_max_validation_retries() -> None:
    """Persistent validation failures land on a one-segment untagged
    decision rather than raising — same policy as M9 picker."""
    bad_dict = {"segments": [], "rationale": "too short"}
    chain = _ScriptedChain(
        responses=[bad_dict for _ in range(TITLE_LANG_MAX_VALIDATION_RETRIES + 1)],
        calls=[],
    )
    detector = LangChainTitleLangDetector(chain=chain, sleep=_no_sleep)
    out = detector.detect(title="Sota ja rauha", candidates=frozenset({"fi"}))
    assert len(out.segments) == 1
    assert out.segments[0].lang is None
    assert "validation failed" in out.rationale.lower()


def test_lang_chain_detector_filters_out_of_candidate_codes_to_null() -> None:
    """Production filter rewrites any segment.lang outside the candidate set
    to null — defends against an LLM that hallucinates German on a fi/sv/en
    record."""
    decision = TitleLangDecision(
        segments=[
            TitleLangSegment(text="Tšarka", lang="en"),
            TitleLangSegment(text="venäläinen tšarkka", lang="fi"),
            TitleLangSegment(text="Meisterwerke am Klavier", lang="de"),
        ],
        rationale="LLM picked German; production filter should rewrite to null.",
    )
    chain = _ScriptedChain(responses=[decision], calls=[])
    detector = LangChainTitleLangDetector(chain=chain, sleep=_no_sleep)
    out = detector.detect(
        title="Tšarka = venäläinen tšarkka = Meisterwerke am Klavier",
        candidates=frozenset({"fi", "sv", "en"}),  # no `de`
    )
    assert [s.lang for s in out.segments] == ["en", "fi", None]


def test_lang_chain_detector_empty_title_falls_through() -> None:
    chain = _ScriptedChain(responses=[], calls=[])
    detector = LangChainTitleLangDetector(chain=chain, sleep=_no_sleep)
    out = detector.detect(title="   ", candidates=frozenset({"fi"}))
    assert len(out.segments) == 1
    assert out.segments[0].lang is None
    assert chain.calls == []
