"""Unit tests for ``bffi_pipeline.contrib_extract_llm``.

The LangChain stack is never loaded — every test injects either the
``StubContribExtractor`` or a hand-rolled scripted chain object."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from bffi_pipeline.contrib_extract_llm import (
    CONTRIB_MAX_VALIDATION_RETRIES,
    CONTRIB_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_CONTRIB_MODEL,
    RELATOR_URI_PREFIX,
    VALID_RELATOR_CODES,
    ContribCandidate,
    ContribExtractDecision,
    LangChainContribExtractor,
    StubContribExtractor,
    contrib_extract_prompt_hash,
    contrib_extract_prompt_text,
)

# --- Pydantic schemas ----------------------------------------------------


def test_contrib_candidate_new_agent_minimal_shape() -> None:
    c = ContribCandidate(name="Christopher Hogwood", relator_code="cnd")
    assert c.relator_code == "cnd"
    assert c.transliteration_of is None


def test_contrib_candidate_transliteration_minimal_shape() -> None:
    c = ContribCandidate(
        name="Bridžet Kollinz",
        transliteration_of="Collins, Bridget",
    )
    assert c.relator_code is None


def test_contrib_candidate_accepts_both_relator_and_transliteration() -> None:
    """Qwen3 8B sometimes emits both — e.g. 'Anssi Karttunen' as a
    transliteration of a typo'd 700 entry 'Karttunen, Assi' AND
    relator_code='prf' because it knows the role. Originally rejected
    via XOR, but that lost both signals on perfectly-handleable
    cases. ``_filter_to_valid_relators`` resolves the ambiguity at
    emission time (transliteration_of wins; relator hint preserved
    for M9 script-variant binding)."""
    c = ContribCandidate(
        name="Anssi Karttunen",
        relator_code="prf",
        transliteration_of="Karttunen, Assi",
    )
    assert c.relator_code == "prf"
    assert c.transliteration_of == "Karttunen, Assi"


def test_contrib_candidate_rejects_neither_relator_nor_transliteration() -> None:
    """An entry with neither field carries no information — the LLM
    just gave us a name with no actionable hint. Reject it."""
    with pytest.raises(ValidationError):
        ContribCandidate(name="x")


def test_contrib_candidate_extra_fields_rejected() -> None:
    """``extra=forbid`` blocks the LLM from sneaking unrecognised fields through."""
    with pytest.raises(ValidationError):
        ContribCandidate.model_validate({"name": "x", "relator_code": "aut", "secret_field": True})


def test_contrib_extract_decision_rejects_short_rationale() -> None:
    with pytest.raises(ValidationError):
        ContribExtractDecision(
            contributions=[ContribCandidate(name="x", relator_code="aut")],
            rationale="too short",
        )


def test_contrib_extract_decision_rejects_stub_phrase_rationale() -> None:
    with pytest.raises(ValidationError):
        ContribExtractDecision(
            contributions=[ContribCandidate(name="x", relator_code="aut")],
            rationale="I don't know which agents this 245$c contains.",
        )


def test_contrib_extract_decision_accepts_empty_contributions() -> None:
    """A record where the heuristic fires but the LLM finds nothing
    new is a valid outcome (false positive on the stop-word filter)."""
    d = ContribExtractDecision(
        contributions=[],
        rationale="No new agents — every name in 245$c matches an existing 100/700 entry.",
    )
    assert d.contributions == []


# --- Prompt loading + hash -----------------------------------------------


def test_contrib_extract_prompt_text_loads() -> None:
    text = contrib_extract_prompt_text()
    assert "### SYSTEM" in text
    assert "### EXAMPLES" in text
    assert "### USER" in text


def test_contrib_extract_prompt_hash_is_stable_within_a_run() -> None:
    h1 = contrib_extract_prompt_hash()
    h2 = contrib_extract_prompt_hash()
    assert h1 == h2
    assert h1.startswith("sha256:")


# --- Stub extractor ------------------------------------------------------


def test_stub_extractor_uses_wired_decision() -> None:
    c_text = "Edited by Stanley Sadie"
    decision = ContribExtractDecision(
        contributions=[
            ContribCandidate(name="Stanley Sadie", relator_code="edt"),
        ],
        rationale="Stub fixture: Stanley Sadie introduced by 'Edited by' — relator edt.",
    )
    stub = StubContribExtractor(decisions={c_text: decision})
    out = stub.extract(c_subfield=c_text, existing_agents=())
    assert out is decision


def test_stub_extractor_falls_through_to_empty_default() -> None:
    stub = StubContribExtractor()
    out = stub.extract(c_subfield="unknown", existing_agents=())
    assert out.contributions == []


# --- LangChain extractor retry logic -------------------------------------


@dataclass
class _ScriptedChain:
    """Minimal chain-shaped object for testing retry / cascade logic."""

    responses: list[object]
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


def _good_decision() -> ContribExtractDecision:
    return ContribExtractDecision(
        contributions=[
            ContribCandidate(name="Stanley Sadie", relator_code="edt"),
        ],
        rationale="Stanley Sadie introduced by 'Edited by'; relator edt for editor.",
    )


def test_lang_chain_extractor_returns_decision_on_happy_path() -> None:
    chain = _ScriptedChain(responses=[_good_decision()], calls=[])
    extractor = LangChainContribExtractor(chain=chain, sleep=_no_sleep)
    out = extractor.extract(c_subfield="Edited by Stanley Sadie", existing_agents=())
    assert out.contributions[0].name == "Stanley Sadie"
    assert len(chain.calls) == 1


def test_lang_chain_extractor_retries_validation_failures_then_recovers() -> None:
    bad_dict = {"contributions": [], "rationale": "too short"}
    chain = _ScriptedChain(responses=[bad_dict, _good_decision()], calls=[])
    extractor = LangChainContribExtractor(chain=chain, sleep=_no_sleep)
    out = extractor.extract(c_subfield="Edited by Stanley Sadie", existing_agents=())
    assert out.contributions[0].name == "Stanley Sadie"
    assert len(chain.calls) == 2  # one failed validation + one success


def test_lang_chain_extractor_falls_through_after_max_validation_retries() -> None:
    """Persistent validation failures land on an empty extraction
    decision rather than raising — same policy as M3 title cascade."""
    bad_dict = {"contributions": [], "rationale": "too short"}
    chain = _ScriptedChain(
        responses=[bad_dict for _ in range(CONTRIB_MAX_VALIDATION_RETRIES + 1)],
        calls=[],
    )
    extractor = LangChainContribExtractor(chain=chain, sleep=_no_sleep)
    out = extractor.extract(c_subfield="anything", existing_agents=())
    assert out.contributions == []
    assert "validation failed" in out.rationale.lower()


def test_lang_chain_extractor_filters_hallucinated_relator_codes() -> None:
    """An LLM that returns an invalid relator code (e.g. 'directorx')
    must have that entry dropped — protects downstream from emitting
    bogus ``bf:role <relators/directorx>`` URIs."""
    decision = ContribExtractDecision(
        contributions=[
            ContribCandidate(name="Stanley Sadie", relator_code="edt"),
            ContribCandidate(name="Bogus", relator_code="directorx"),
        ],
        rationale="Mixed valid + hallucinated codes; production filter must drop the bogus one.",
    )
    chain = _ScriptedChain(responses=[decision], calls=[])
    extractor = LangChainContribExtractor(chain=chain, sleep=_no_sleep)
    out = extractor.extract(c_subfield="...", existing_agents=())
    assert [c.name for c in out.contributions] == ["Stanley Sadie"]


def test_lang_chain_extractor_filters_phantom_transliteration_pointers() -> None:
    """A transliteration entry whose ``transliteration_of`` points at
    an agent string not in the ``existing_agents`` tuple must be
    dropped — the LLM can't bind to a name it dreamed up."""
    decision = ContribExtractDecision(
        contributions=[
            ContribCandidate(
                name="Bridžet Kollinz",
                transliteration_of="Collins, Bridget",  # in existing_agents
            ),
            ContribCandidate(
                name="Phantom Variant",
                transliteration_of="Person, Nonexistent",  # NOT in existing_agents
            ),
        ],
        rationale=(
            "One real transliteration + one hallucinated pointer; the filter keeps only the real."
        ),
    )
    chain = _ScriptedChain(responses=[decision], calls=[])
    extractor = LangChainContribExtractor(chain=chain, sleep=_no_sleep)
    out = extractor.extract(
        c_subfield="...",
        existing_agents=("Collins, Bridget",),
    )
    assert [c.name for c in out.contributions] == ["Bridžet Kollinz"]


def test_lang_chain_extractor_empty_c_subfield_falls_through() -> None:
    chain = _ScriptedChain(responses=[], calls=[])
    extractor = LangChainContribExtractor(chain=chain, sleep=_no_sleep)
    out = extractor.extract(c_subfield="   ", existing_agents=())
    assert out.contributions == []
    assert chain.calls == []


# --- Constants pinning ----------------------------------------------------


def test_valid_relator_codes_includes_common_marc_codes() -> None:
    """Pin a baseline so future edits don't accidentally remove a
    code the prompt instructs the LLM to use."""
    for code in ("aut", "trl", "ill", "pht", "edt", "cmp", "prf", "drt"):
        assert code in VALID_RELATOR_CODES


def test_relator_uri_prefix_is_loc_canonical() -> None:
    """The prefix must match LoC's canonical URI form so a bf:role URI
    built as `RELATOR_URI_PREFIX + 'trl'` resolves correctly when the
    LoC relators vocab is loaded into Fuseki."""
    assert RELATOR_URI_PREFIX == "http://id.loc.gov/vocabulary/relators/"


def test_request_timeout_is_set_to_a_finite_value() -> None:
    """Without an explicit ``timeout`` on ChatOpenAI, the OpenAI SDK's
    10-min default times retry-internally and a wedged Ollama can pin
    a worker for 30+ minutes silently — discovered during the M3
    contributor-extraction live smoke. Pinning the constant here so a
    future refactor can't accidentally remove the bound."""
    assert 10 < CONTRIB_REQUEST_TIMEOUT_SECONDS < 600


def test_default_contrib_model_is_qwen3_8b() -> None:
    """Per-cascade default picked by live benchmark on the M2 Max:
    Qwen3 8B Q4_K_M produces correct extractions in ~10s/call warm,
    vs ~45s/call for 32B at the same quality. Caller can still
    override via the constructor's ``model_name`` or the CLI's
    ``--primary-model`` flag."""
    assert DEFAULT_CONTRIB_MODEL == "qwen3:8b-q4_K_M"
