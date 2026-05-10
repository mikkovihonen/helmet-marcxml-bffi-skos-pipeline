"""Local-LLM cascade for MARC 245$c contributor extraction.

When :func:`bffi_pipeline.contrib_extract.extract_contributions` finds
a record where 245$c contains name-like tokens not covered by 100/700,
it can escalate to the local Qwen3 cascade. The LLM sees the 245$c
text and the existing 100/700 agent labels and returns a list of new
agents (with MARC relator codes) plus any transliteration variants of
agents already structurally captured.

Mirrors the M3 title-language and M9 picker cascades exactly:
versioned prompt at ``prompts/contrib_extract_v1.txt``,
Pydantic-validated structured output, two retry layers
(validation + connection backoff), confidence-fall-through to a
"no extraction" stub when retries exhaust. Tests inject a
:class:`StubContribExtractor` so ``pytest`` never loads the LLM stack.

Cascade is opt-in: callers pass an instantiated extractor to
:func:`bffi_pipeline.contrib_extract.extract_contributions`. The CLI's
``bffi-pipeline bf-to-bffi`` exposes a ``--llm-contrib-cascade`` flag
that builds the default :class:`LangChainContribExtractor` against the
configured ``LLM_BASE_URL``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bffi_pipeline.config import get_settings

CONTRIB_PROMPT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "prompts" / "contrib_extract_v1.txt"
)
_PROMPT_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^### (\w+)\s*$", re.MULTILINE)

CONTRIB_MAX_VALIDATION_RETRIES: Final[int] = 2
CONTRIB_MAX_CONNECTION_RETRIES: Final[int] = 3
CONTRIB_CONNECTION_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 30.0, 120.0)

#: Valid MARC relator codes the prompt instructs the LLM to use. Kept
#: in code so a post-parse filter can reject hallucinated codes — same
#: defence the M9 picker uses for hallucinated authority URIs.
VALID_RELATOR_CODES: Final[frozenset[str]] = frozenset(
    {
        "aut",
        "trl",
        "ill",
        "pht",
        "edt",
        "cmp",
        "prf",
        "aft",
        "aui",
        "ctb",
        "nrt",
        "drt",
        "aus",
        "pro",
        "arr",
        "lyr",
        "cnd",
        "mus",
        "adp",
        "sng",
    }
)

#: Stable display URI prefix for relator codes. Used by callers building
#: ``bf:role <relator-uri>`` triples.
RELATOR_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/relators/"

_STUB_PHRASES: Final[tuple[str, ...]] = (
    "i don't know",
    "unable to determine",
    "n/a",
    "not sure",
)
_MIN_RATIONALE_CHARS: Final[int] = 20


# --- Schemas --------------------------------------------------------------


class ContribCandidate(BaseModel):
    """One agent extracted from (or matched against) MARC 245$c.

    Two distinct roles in the output list:

    1. **New agent**: ``relator_code`` is one of :data:`VALID_RELATOR_CODES`,
       ``transliteration_of`` is ``None``. Downstream emits a
       ``bffi:Contribution`` with ``bf:role <relators/{relator_code}>``.
    2. **Transliteration variant**: ``relator_code`` is ``None`` and
       ``transliteration_of`` is the exact 100/700 agent label this name
       maps to. Downstream uses the pointer to share a KANTO URI across
       both forms (M9 reconciliation already binds the canonical agent).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    relator_code: str | None = None
    role_text: str | None = Field(
        default=None,
        description=(
            "Cataloguer-language role marker as printed in 245$c "
            "(e.g. 'kääntänyt', 'edited by'). Carried through for "
            "human-readable provenance; not used for machine binding."
        ),
    )
    transliteration_of: str | None = None

    @model_validator(mode="after")
    def _exactly_one_of_relator_or_transliteration(self) -> ContribCandidate:
        has_relator = self.relator_code is not None
        has_translit = self.transliteration_of is not None
        if has_relator == has_translit:
            raise ValueError(
                "exactly one of relator_code / transliteration_of must be set "
                "(new agent vs. transliteration variant of an existing agent)"
            )
        return self


class ContribExtractDecision(BaseModel):
    """Per-record output of the contributor-extraction cascade."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contributions: list[ContribCandidate] = Field(default_factory=list)
    rationale: str = Field(min_length=_MIN_RATIONALE_CHARS)

    @model_validator(mode="after")
    def _rationale_is_substantive(self) -> ContribExtractDecision:
        text = self.rationale.strip()
        if len(text) < _MIN_RATIONALE_CHARS:
            raise ValueError(f"rationale shorter than {_MIN_RATIONALE_CHARS} characters")
        lowered = text.lower()
        for phrase in _STUB_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                raise ValueError(f"rationale contains stub phrase: {phrase!r}")
        return self


class ContribExtractor(Protocol):
    """Protocol the contributor-extraction target satisfies."""

    def extract(
        self,
        *,
        c_subfield: str,
        existing_agents: tuple[str, ...],
    ) -> ContribExtractDecision:
        """Return new contributions found in ``c_subfield`` not in ``existing_agents``."""
        ...


@dataclass
class StubContribExtractor:
    """Deterministic test extractor keyed on (c_subfield) text."""

    decisions: dict[str, ContribExtractDecision] = field(default_factory=dict)
    default: ContribExtractDecision | None = None

    def extract(
        self,
        *,
        c_subfield: str,
        existing_agents: tuple[str, ...],
    ) -> ContribExtractDecision:
        del existing_agents  # not needed by stub
        if c_subfield in self.decisions:
            return self.decisions[c_subfield]
        if self.default is not None:
            return self.default
        return ContribExtractDecision(
            contributions=[],
            rationale="StubContribExtractor default: no decision wired for this 245$c.",
        )


# --- Prompt + chain -------------------------------------------------------


@lru_cache(maxsize=1)
def contrib_extract_prompt_text() -> str:
    if not CONTRIB_PROMPT_PATH.is_file():
        raise FileNotFoundError(f"Contrib-extract prompt not found at {CONTRIB_PROMPT_PATH!s}.")
    return CONTRIB_PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def contrib_extract_prompt_hash() -> str:
    return (
        "sha256:" + hashlib.sha256(contrib_extract_prompt_text().encode("utf-8")).hexdigest()[:16]
    )


@lru_cache(maxsize=1)
def _parse_prompt_sections() -> dict[str, str]:
    raw = contrib_extract_prompt_text()
    sections: dict[str, str] = {}
    matches = list(_PROMPT_SECTION_RE.finditer(raw))
    if not matches:
        raise ValueError(f"No '### SECTION' markers found in {CONTRIB_PROMPT_PATH!s}.")
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()
    for required in ("SYSTEM", "EXAMPLES", "USER"):
        if required not in sections:
            raise ValueError(
                f"{CONTRIB_PROMPT_PATH!s} is missing required '### {required}' section."
            )
    return sections


def _format_existing_agents(agents: tuple[str, ...]) -> str:
    """Render the existing-agents list as a JSON-array string."""
    return json.dumps(list(agents), ensure_ascii=False)


def _is_connection_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "RemoteProtocolError",
        "APIConnectionError",
        "APITimeoutError",
        "Timeout",
    }:
        return True
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_connection_error(cause)
    return False


def _build_chain(
    *,
    model_name: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.0,
    seed: int = 42,
) -> Any:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    sections = _parse_prompt_sections()
    template = ChatPromptTemplate.from_messages(
        [
            ("system", sections["SYSTEM"] + "\n\n" + sections["EXAMPLES"]),
            ("user", sections["USER"]),
        ]
    )
    llm = ChatOpenAI(
        base_url=base_url,
        api_key=SecretStr(api_key),
        model=model_name,
        temperature=temperature,
        seed=seed,
    )
    return template | llm.with_structured_output(ContribExtractDecision, method="json_schema")


def _filter_to_valid_relators(
    decision: ContribExtractDecision,
    existing_agents: tuple[str, ...],
) -> ContribExtractDecision:
    """Drop new-agent entries whose relator_code isn't in
    :data:`VALID_RELATOR_CODES`, and drop transliteration entries whose
    pointer doesn't appear in ``existing_agents``. Defends against an
    LLM that hallucinates a relator code or a phantom canonical agent.
    """
    if not decision.contributions:
        return decision
    existing = set(existing_agents)
    cleaned: list[ContribCandidate] = []
    for c in decision.contributions:
        if c.relator_code is not None:
            if c.relator_code not in VALID_RELATOR_CODES:
                continue
        elif c.transliteration_of not in existing:
            continue
        cleaned.append(c)
    return ContribExtractDecision(contributions=cleaned, rationale=decision.rationale)


def _fallthrough_decision(reason: str) -> ContribExtractDecision:
    cleaned = reason.strip() or "no error message available"
    lowered = cleaned.lower()
    for phrase in _STUB_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            cleaned = re.sub(
                rf"\b{re.escape(phrase)}\b",
                "[stub phrase elided]",
                cleaned,
                flags=re.IGNORECASE,
            )
    rationale = f"Extractor fell through to empty result: {cleaned}"
    return ContribExtractDecision(contributions=[], rationale=rationale)


# --- LangChain-backed extractor ------------------------------------------


@dataclass
class LangChainContribExtractor:
    """Production extractor that calls Qwen3 via LangChain.

    Validation-failure retry (max 2) and connection-error retry
    (5 / 30 / 120 s, max 3) mirror the M3 title cascade and M6 / M9
    policies. On unrecoverable error, returns an empty result so the
    caller always gets something.
    """

    model_name: str | None = None
    chain: Any = None
    sleep: Callable[[float], None] = time.sleep

    def _resolved_chain(self) -> Any:
        if self.chain is not None:
            return self.chain
        settings = get_settings()
        return _build_chain(
            model_name=self.model_name or settings.llm_model_primary,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )

    def extract(
        self,
        *,
        c_subfield: str,
        existing_agents: tuple[str, ...],
    ) -> ContribExtractDecision:
        c_subfield = c_subfield.strip()
        if not c_subfield:
            return _fallthrough_decision("empty 245$c")

        chain = self._resolved_chain()
        invoke_payload = {
            "c_subfield": c_subfield,
            "existing_agents": _format_existing_agents(existing_agents),
        }

        connection_attempts = 0
        validation_attempts = 0
        last_error = "unknown failure"

        while True:
            try:
                raw = chain.invoke(invoke_payload)
            except Exception as exc:
                if _is_connection_error(exc):
                    if connection_attempts < CONTRIB_MAX_CONNECTION_RETRIES:
                        self.sleep(CONTRIB_CONNECTION_BACKOFF_SECONDS[connection_attempts])
                        connection_attempts += 1
                        last_error = (
                            f"connection error after {connection_attempts} retry(ies): {exc!s}"
                        )
                        continue
                    last_error = (
                        f"connection error after {CONTRIB_MAX_CONNECTION_RETRIES} retries "
                        f"exhausted: {exc!s}"
                    )
                    break
                last_error = f"unrecoverable LLM error: {exc!s}"
                break

            try:
                if isinstance(raw, ContribExtractDecision):
                    decision = raw
                else:
                    decision = ContribExtractDecision.model_validate(raw)
            except (ValidationError, ValueError) as exc:
                if validation_attempts < CONTRIB_MAX_VALIDATION_RETRIES:
                    validation_attempts += 1
                    last_error = f"validation failure (attempt {validation_attempts}): {exc!s}"
                    continue
                last_error = (
                    f"validation failed after {CONTRIB_MAX_VALIDATION_RETRIES} retries: {exc!s}"
                )
                break

            return _filter_to_valid_relators(decision, existing_agents)

        return _fallthrough_decision(last_error)


__all__ = [
    "CONTRIB_CONNECTION_BACKOFF_SECONDS",
    "CONTRIB_MAX_CONNECTION_RETRIES",
    "CONTRIB_MAX_VALIDATION_RETRIES",
    "CONTRIB_PROMPT_PATH",
    "RELATOR_URI_PREFIX",
    "VALID_RELATOR_CODES",
    "ContribCandidate",
    "ContribExtractDecision",
    "ContribExtractor",
    "LangChainContribExtractor",
    "StubContribExtractor",
    "contrib_extract_prompt_hash",
    "contrib_extract_prompt_text",
]
