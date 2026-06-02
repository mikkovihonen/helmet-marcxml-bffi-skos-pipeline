"""Unit tests for P-41 Phase B.2 — 245$c LLM cascade fallback.

All tests use mocked LangChain chains or the :class:`StubSalvageExtractor`
so no test hits the live LLM. The integration of the LLM tier into the
salvage dispatcher is exercised in ``test_salvage.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bffi_pipeline.stages.m2.salvage_245c import ParsedAgent
from bffi_pipeline.stages.m2.salvage_245c_llm import (
    LLM_CONFIDENCE_CAP,
    LangChainSalvageExtractor,
    LlmAgent,
    SalvageCache,
    SalvageDecision,
    StubSalvageExtractor,
    _cache_key,
    _enforce_verbatim_substring,
    salvage_245c_prompt_hash,
    salvage_245c_prompt_text,
)

# --- Prompt loading -------------------------------------------------------


class TestPromptLoading:
    def test_prompt_file_exists_and_has_required_sections(self) -> None:
        text = salvage_245c_prompt_text()
        assert "### SYSTEM" in text
        assert "### EXAMPLES" in text
        assert "### USER" in text

    def test_prompt_hash_is_stable(self) -> None:
        """Two calls return the same hash — the lru_cache + sha256
        combination guarantees this. Provenance attaches this hash to
        every Synthesis Activity so a prompt change is detectable."""
        assert salvage_245c_prompt_hash() == salvage_245c_prompt_hash()
        assert salvage_245c_prompt_hash().startswith("sha256:")


# --- Pydantic schema validation ------------------------------------------


class TestSalvageDecisionValidation:
    def test_empty_agents_with_rationale_validates(self) -> None:
        d = SalvageDecision(
            agents=[],
            rationale="245$c contains no extractable personal name agent.",
        )
        assert d.agents == []

    def test_short_rationale_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 20 characters"):
            SalvageDecision(agents=[], rationale="too short")

    def test_stub_phrase_in_rationale_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="stub phrase"):
            SalvageDecision(
                agents=[],
                rationale="I don't know what to do with this 245$c text.",
            )

    def test_invalid_role_is_rejected_at_schema_level(self) -> None:
        # The Literal type constraint in LlmAgent rejects invalid roles
        # before they reach the post-processor.
        with pytest.raises(ValueError, match=r"role"):
            LlmAgent(name="X", role="banana", verbatim_substring=True)  # type: ignore[arg-type]


# --- Verbatim-substring enforcement --------------------------------------


class TestVerbatimSubstringEnforcement:
    """Load-bearing defence against LLM hallucination."""

    def test_substring_match_accepts_agent_with_capped_confidence(self) -> None:
        decision = SalvageDecision(
            agents=[LlmAgent(name="Mika Waltari", role="author", verbatim_substring=True)],
            rationale="245$c names a single author by Finnish convention.",
        )
        agents = _enforce_verbatim_substring(decision, c_subfield="kirjoittanut Mika Waltari")
        assert agents == [
            ParsedAgent(name="Mika Waltari", role="author", confidence=LLM_CONFIDENCE_CAP)
        ]

    def test_non_substring_name_is_dropped(self) -> None:
        decision = SalvageDecision(
            agents=[
                LlmAgent(name="J. K. Rowling", role="author", verbatim_substring=True),
            ],
            rationale="Hypothetical hallucination — the LLM returned a name "
            "that isn't actually in the 245$c we sent it.",
        )
        agents = _enforce_verbatim_substring(decision, c_subfield="kirjoittanut Mika Waltari")
        assert agents == []

    def test_partial_match_in_multi_agent_drops_only_the_phantom(self) -> None:
        decision = SalvageDecision(
            agents=[
                LlmAgent(name="Mika Waltari", role="author", verbatim_substring=True),
                LlmAgent(name="Hallucinated Phantom", role="editor", verbatim_substring=True),
            ],
            rationale="One valid agent and one hallucinated phantom.",
        )
        agents = _enforce_verbatim_substring(decision, c_subfield="kirjoittanut Mika Waltari")
        assert len(agents) == 1
        assert agents[0].name == "Mika Waltari"


# --- Stub extractor -------------------------------------------------------


class TestStubSalvageExtractor:
    def test_keyed_lookup_returns_the_configured_agents(self) -> None:
        stub = StubSalvageExtractor(
            decisions={
                "by Margaret Atwood": [
                    ParsedAgent(name="Margaret Atwood", role="author", confidence=0.7),
                ]
            }
        )
        result = stub.extract(c_subfield="by Margaret Atwood")
        assert result == [
            ParsedAgent(name="Margaret Atwood", role="author", confidence=0.7),
        ]

    def test_unknown_key_returns_empty(self) -> None:
        stub = StubSalvageExtractor(decisions={})
        assert stub.extract(c_subfield="anything") == []


# --- Cache ----------------------------------------------------------------


class TestSalvageCache:
    def test_get_returns_none_for_missing_key(self, tmp_path: Path) -> None:
        cache = SalvageCache(tmp_path / "synth.sqlite")
        assert cache.get("missing-key") is None
        cache.close()

    def test_set_then_get_roundtrips_the_decision(self, tmp_path: Path) -> None:
        cache = SalvageCache(tmp_path / "synth.sqlite")
        decision = SalvageDecision(
            agents=[LlmAgent(name="X", role="author", verbatim_substring=True)],
            rationale="Some rationale longer than the twenty-char minimum.",
        )
        cache.set(
            "key1",
            decision,
            model_name="qwen3:8b-q4_K_M",
            prompt_hash_value="sha256:abcdef0123456789",
        )
        recovered = cache.get("key1")
        assert recovered == decision
        cache.close()

    def test_replace_overwrites_existing_entry(self, tmp_path: Path) -> None:
        cache = SalvageCache(tmp_path / "synth.sqlite")
        d1 = SalvageDecision(
            agents=[],
            rationale="First decision — the previously-empty outcome.",
        )
        d2 = SalvageDecision(
            agents=[LlmAgent(name="Y", role="editor", verbatim_substring=True)],
            rationale="Second decision — same key but different value.",
        )
        cache.set("key1", d1, model_name="m", prompt_hash_value="h")
        cache.set("key1", d2, model_name="m", prompt_hash_value="h")
        assert cache.get("key1") == d2
        cache.close()

    def test_cache_key_includes_prompt_hash(self) -> None:
        """Different prompt versions must produce different cache keys —
        otherwise a re-prompt with the same 245$c would silently
        return the old decision."""
        k1 = _cache_key(prompt_hash_value="sha256:aaa", c_subfield="by X")
        k2 = _cache_key(prompt_hash_value="sha256:bbb", c_subfield="by X")
        assert k1 != k2


# --- LangChainSalvageExtractor (chain mocked) ----------------------------


class _MockChain:
    """Stand-in for the LangChain pipeline; ``invoke`` returns a
    canned :class:`SalvageDecision` (or raises) per the test setup."""

    def __init__(self, returns: SalvageDecision | None = None, raises: Exception | None = None):
        self._returns = returns
        self._raises = raises
        self.call_count = 0

    def invoke(self, payload: dict[str, Any]) -> SalvageDecision:
        self.call_count += 1
        if self._raises is not None:
            raise self._raises
        if self._returns is None:
            raise AssertionError("mock chain configured without a return value")
        return self._returns


class TestLangChainSalvageExtractor:
    def test_happy_path_returns_capped_confidence(self) -> None:
        chain = _MockChain(
            returns=SalvageDecision(
                agents=[LlmAgent(name="Mika Waltari", role="author", verbatim_substring=True)],
                rationale="Standard rationale longer than the minimum threshold.",
            )
        )
        extractor = LangChainSalvageExtractor(chain=chain)
        agents = extractor.extract(c_subfield="kirjoittanut Mika Waltari")
        assert agents == [
            ParsedAgent(name="Mika Waltari", role="author", confidence=LLM_CONFIDENCE_CAP)
        ]
        assert chain.call_count == 1

    def test_empty_245c_short_circuits_no_llm_call(self) -> None:
        chain = _MockChain()
        extractor = LangChainSalvageExtractor(chain=chain)
        assert extractor.extract(c_subfield="") == []
        assert extractor.extract(c_subfield="   ") == []
        assert chain.call_count == 0

    def test_hallucinated_name_is_dropped(self) -> None:
        chain = _MockChain(
            returns=SalvageDecision(
                agents=[
                    LlmAgent(name="J. K. Rowling", role="author", verbatim_substring=True),
                ],
                rationale="Hallucination — the name is not in the input 245$c.",
            )
        )
        extractor = LangChainSalvageExtractor(chain=chain)
        agents = extractor.extract(c_subfield="kirjoittanut Mika Waltari")
        assert agents == []

    def test_cache_hit_avoids_llm_call(self, tmp_path: Path) -> None:
        chain = _MockChain(
            returns=SalvageDecision(
                agents=[LlmAgent(name="Mika Waltari", role="author", verbatim_substring=True)],
                rationale="Stored rationale longer than twenty characters here.",
            )
        )
        cache = SalvageCache(tmp_path / "synth.sqlite")
        extractor = LangChainSalvageExtractor(chain=chain, cache=cache)

        # First call: LLM is invoked, decision is cached.
        first = extractor.extract(c_subfield="kirjoittanut Mika Waltari")
        assert len(first) == 1
        assert chain.call_count == 1

        # Second call with same input: served from cache, LLM not invoked.
        second = extractor.extract(c_subfield="kirjoittanut Mika Waltari")
        assert second == first
        assert chain.call_count == 1  # no additional invoke.

        cache.close()

    def test_connection_error_falls_through_to_empty(self) -> None:
        class _ConnectError(Exception):
            pass

        # Simulate a connection-error class the predicate recognises.
        _ConnectError.__name__ = "ConnectError"
        chain = _MockChain(raises=_ConnectError("backend unreachable"))
        extractor = LangChainSalvageExtractor(
            chain=chain,
            sleep=lambda _t: None,  # zero-time backoff in tests
        )
        # Should retry SALVAGE_MAX_CONNECTION_RETRIES times then return [].
        assert extractor.extract(c_subfield="kirjoittanut Mika Waltari") == []

    def test_unrecoverable_error_falls_through_to_empty(self) -> None:
        chain = _MockChain(raises=RuntimeError("unrecoverable"))
        extractor = LangChainSalvageExtractor(chain=chain, sleep=lambda _t: None)
        assert extractor.extract(c_subfield="kirjoittanut Mika Waltari") == []
        # Non-retryable error: one attempt, then bail.
        assert chain.call_count == 1
