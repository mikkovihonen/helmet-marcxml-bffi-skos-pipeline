"""Local-LLM cascade for ambiguous title-language detection.

When :func:`bffi_pipeline.title_lang.tag_title` hits the collapse
heuristic — every Latin-script segment confidently mapped to the same
language by Lingua despite the cataloguer declaring multiple
languages — we can escalate to the local Qwen3 cascade. The LLM sees
the full title plus the cataloguer-declared candidate set and returns
a per-segment language assignment.

Mirrors the M9 picker in shape: versioned prompt
(``prompts/title_lang_v1.txt``), Pydantic-validated structured
output, two retry layers (validation + connection), confidence-fall-
through to a "no decision" stub when retries exhaust. Tests inject a
:class:`StubTitleLangDetector` so ``pytest`` never loads the LLM
stack.

Cascade is opt-in: callers pass an instantiated detector to
:func:`bffi_pipeline.title_lang.tag_title`. The CLI's
``bffi-pipeline bf-to-bffi`` exposes a ``--llm-title-cascade`` flag
that builds the default
:class:`LangChainTitleLangDetector` against the configured
``LLM_BASE_URL``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bffi_pipeline.config import get_settings
from bffi_pipeline.llm_json_mode import json_mode_instruction

#: Versioned prompt source. Hashed at startup so any future provenance
#: writer can pin the exact prompt that produced a decision.
TITLE_LANG_PROMPT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "prompts" / "title_lang_v1.txt"
)
_PROMPT_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^### (\w+)\s*$", re.MULTILINE)

#: Same retry shape as the M6 judge / M9 picker so cataloguers see one
#: coherent failure-handling story across the LLM-dependent stages.
TITLE_LANG_MAX_VALIDATION_RETRIES: Final[int] = 2
TITLE_LANG_MAX_CONNECTION_RETRIES: Final[int] = 3
TITLE_LANG_CONNECTION_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 30.0, 120.0)

#: Stub phrases that disqualify a rationale. Same policy as the M6
#: judge — a hand-wavy rationale isn't trustworthy and shouldn't cache.
_STUB_PHRASES: Final[tuple[str, ...]] = (
    "i don't know",
    "unable to determine",
    "n/a",
    "not sure",
)
_MIN_RATIONALE_CHARS: Final[int] = 20


# --- Schemas --------------------------------------------------------------


class TitleLangSegment(BaseModel):
    """One title segment with its assigned language code (or null)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    lang: str | None = Field(
        default=None,
        description=(
            "Two-letter BCP-47 code from the candidate set, or null when "
            "no candidate fits this segment."
        ),
    )


class TitleLangDecision(BaseModel):
    """Per-segment language assignment for one MARC 245 title.

    Boundary-4 validators mirror the M9 picker: rationale is
    substantive (≥ 20 chars, no stub phrases), at least one segment,
    and the segment language codes — when non-null — must come from
    the candidate set the prompt supplied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    segments: list[TitleLangSegment] = Field(min_length=1)
    rationale: str = Field(min_length=_MIN_RATIONALE_CHARS)

    @model_validator(mode="after")
    def _rationale_is_substantive(self) -> TitleLangDecision:
        text = self.rationale.strip()
        if len(text) < _MIN_RATIONALE_CHARS:
            raise ValueError(f"rationale shorter than {_MIN_RATIONALE_CHARS} characters")
        lowered = text.lower()
        for phrase in _STUB_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                raise ValueError(f"rationale contains stub phrase: {phrase!r}")
        return self


class TitleLangDetector(Protocol):
    """Protocol the M3 cascade target satisfies.

    The phase-1 LangChain implementation reads the versioned prompt
    and calls the local Qwen3 server. Tests inject
    :class:`StubTitleLangDetector` for deterministic outputs.
    """

    def detect(self, *, title: str, candidates: frozenset[str]) -> TitleLangDecision:
        """Return a per-segment language assignment for ``title``.

        Implementations must constrain ``segment.lang`` values to be
        either null or a member of ``candidates``.
        """
        ...


@dataclass
class StubTitleLangDetector:
    """Deterministic test detector keyed on the title text."""

    decisions: dict[str, TitleLangDecision] = field(default_factory=dict)
    default: TitleLangDecision | None = None

    def detect(self, *, title: str, candidates: frozenset[str]) -> TitleLangDecision:
        """Look up a wired decision; fall back to a single-segment untagged default."""
        del candidates  # candidates not needed by stub
        if title in self.decisions:
            return self.decisions[title]
        if self.default is not None:
            return self.default
        return TitleLangDecision(
            segments=[TitleLangSegment(text=title, lang=None)],
            rationale=("StubTitleLangDetector default: no decision wired for this title."),
        )


# --- Prompt + chain -------------------------------------------------------


@lru_cache(maxsize=1)
def title_lang_prompt_text() -> str:
    """Return the raw ``prompts/title_lang_v1.txt`` contents."""
    if not TITLE_LANG_PROMPT_PATH.is_file():
        raise FileNotFoundError(f"Title-language prompt not found at {TITLE_LANG_PROMPT_PATH!s}.")
    return TITLE_LANG_PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def title_lang_prompt_hash() -> str:
    """SHA-256 of :func:`title_lang_prompt_text`. Logged with future provenance."""
    return "sha256:" + hashlib.sha256(title_lang_prompt_text().encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def _parse_prompt_sections() -> dict[str, str]:
    """Split the prompt into ``SYSTEM`` / ``EXAMPLES`` / ``USER`` blocks."""
    raw = title_lang_prompt_text()
    sections: dict[str, str] = {}
    matches = list(_PROMPT_SECTION_RE.finditer(raw))
    if not matches:
        raise ValueError(f"No '### SECTION' markers found in {TITLE_LANG_PROMPT_PATH!s}.")
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()
    for required in ("SYSTEM", "EXAMPLES", "USER"):
        if required not in sections:
            raise ValueError(
                f"{TITLE_LANG_PROMPT_PATH!s} is missing required '### {required}' section."
            )
    return sections


def _format_candidates_for_prompt(candidates: frozenset[str]) -> str:
    """Render the candidate set as a JSON-array-shaped string the prompt expects."""
    return "[" + ", ".join(f'"{c}"' for c in sorted(candidates)) + "]"


def _is_connection_error(exc: BaseException) -> bool:
    """Treat low-level network / timeout errors as retry-worthy events."""
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


#: Hard request timeout per chain.invoke. Without this, a wedged or
#: slow Ollama can pin a worker for 10+ minutes silently — the OpenAI
#: SDK's defaults are too generous for our retry contract, which
#: relies on the call raising on hang. Same value as the M9 picker
#: and M3 contributor cascade.
TITLE_LANG_REQUEST_TIMEOUT_SECONDS: Final[float] = 120.0


def _build_chain(
    *,
    model_name: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.0,
    seed: int = 42,
) -> Any:
    """Compose ``ChatOpenAI.with_structured_output(TitleLangDecision)``."""
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    sections = _parse_prompt_sections()
    # JSON-mode schema instruction — see judge.py / llm_json_mode.py for the
    # rationale (P-02 A5).
    template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                sections["SYSTEM"]
                + "\n\n"
                + sections["EXAMPLES"]
                + "\n\n"
                + json_mode_instruction(TitleLangDecision),
            ),
            ("user", sections["USER"]),
        ]
    )
    llm = ChatOpenAI(
        base_url=base_url,
        api_key=SecretStr(api_key),
        model=model_name,
        temperature=temperature,
        seed=seed,
        timeout=TITLE_LANG_REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    return template | llm.with_structured_output(TitleLangDecision, method="json_mode")


def _filter_to_candidates(
    decision: TitleLangDecision, candidates: frozenset[str]
) -> TitleLangDecision:
    """Replace any segment ``lang`` not in ``candidates`` with ``None``.

    Defends against an LLM that hallucinates a language code outside
    the supplied candidate set. The Pydantic schema doesn't enumerate
    valid codes (we don't want to bake the ``fi/sv/en/ru`` set into
    the model), so we enforce the constraint here.
    """
    if not decision.segments:
        return decision
    fixed = [
        TitleLangSegment(
            text=seg.text,
            lang=seg.lang if seg.lang in candidates else None,
        )
        for seg in decision.segments
    ]
    return TitleLangDecision(segments=fixed, rationale=decision.rationale)


def _fallthrough_decision(title: str, reason: str) -> TitleLangDecision:
    """Build a one-segment ``lang=None`` decision when retries exhaust."""
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
    rationale = f"Detector fell through to single untagged segment: {cleaned}"
    return TitleLangDecision(
        segments=[TitleLangSegment(text=title.strip() or "(empty)", lang=None)],
        rationale=rationale,
    )


# --- LangChain-backed detector --------------------------------------------


@dataclass
class LangChainTitleLangDetector:
    """Production detector that calls Qwen3 via LangChain.

    Validation-failure retry (max 2) and connection-error retry
    (5 / 30 / 120 s, max 3) mirror the M6 judge and M9 picker
    policies. On unrecoverable error, returns a single-segment
    untagged decision so the caller always gets something.
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

    def detect(self, *, title: str, candidates: frozenset[str]) -> TitleLangDecision:
        """Invoke the chain with retries; constrain output to ``candidates``."""
        title = title.strip()
        if not title:
            return _fallthrough_decision(title, "empty title")
        if not candidates:
            return _fallthrough_decision(title, "empty candidate set")

        chain = self._resolved_chain()
        invoke_payload = {
            "title": title,
            "candidates": _format_candidates_for_prompt(candidates),
        }

        connection_attempts = 0
        validation_attempts = 0
        last_error = "unknown failure"

        while True:
            try:
                raw = chain.invoke(invoke_payload)
            except Exception as exc:
                if _is_connection_error(exc):
                    if connection_attempts < TITLE_LANG_MAX_CONNECTION_RETRIES:
                        self.sleep(TITLE_LANG_CONNECTION_BACKOFF_SECONDS[connection_attempts])
                        connection_attempts += 1
                        last_error = (
                            f"connection error after {connection_attempts} retry(ies): {exc!s}"
                        )
                        continue
                    last_error = (
                        f"connection error after {TITLE_LANG_MAX_CONNECTION_RETRIES} retries "
                        f"exhausted: {exc!s}"
                    )
                    break
                last_error = f"unrecoverable LLM error: {exc!s}"
                break

            try:
                if isinstance(raw, TitleLangDecision):
                    decision = raw
                else:
                    decision = TitleLangDecision.model_validate(raw)
            except (ValidationError, ValueError) as exc:
                if validation_attempts < TITLE_LANG_MAX_VALIDATION_RETRIES:
                    validation_attempts += 1
                    last_error = f"validation failure (attempt {validation_attempts}): {exc!s}"
                    continue
                last_error = (
                    f"validation failed after {TITLE_LANG_MAX_VALIDATION_RETRIES} retries: {exc!s}"
                )
                break

            return _filter_to_candidates(decision, candidates)

        return _fallthrough_decision(title, last_error)


__all__ = [
    "TITLE_LANG_CONNECTION_BACKOFF_SECONDS",
    "TITLE_LANG_MAX_CONNECTION_RETRIES",
    "TITLE_LANG_MAX_VALIDATION_RETRIES",
    "TITLE_LANG_PROMPT_PATH",
    "LangChainTitleLangDetector",
    "StubTitleLangDetector",
    "TitleLangDecision",
    "TitleLangDetector",
    "TitleLangSegment",
    "title_lang_prompt_hash",
    "title_lang_prompt_text",
]
