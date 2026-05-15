"""M6 LangChain chain construction + retry-error classification.

Builds the ``ChatPromptTemplate | ChatOpenAI.with_structured_output``
pipeline that ``judge_pair`` invokes. The system-prompt prefix is
pre-computed at module-import time (P-02 Phase B) so mlx-lm's
server-side prefix cache keys against byte-stable input.

``_is_connection_error`` and ``_is_timeout_error`` walk the exception
``__cause__`` / ``__context__`` chain so the retry stack works even
when LangChain wraps the original ``httpx`` exception.

P-38 Phase B: extracted from m6/runner.py to keep the runner focused
on the cascade orchestration. No logic change — moves only.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel

from bffi_pipeline.llm_json_mode import json_mode_instruction
from bffi_pipeline.stages.m6.prompts import (
    _parse_prompt_sections,
    _parse_prompt_sections_fast,
)
from bffi_pipeline.stages.m6.validation import (
    WorkMatchDecision,
    WorkMatchDecisionFast,
)

#: Subset of timeout-shaped exception names that the watchdog
#: specifically counts as "the LLM took too long" — narrower than
#: :func:`_is_connection_error` which also catches network resets etc.
_TIMEOUT_EXCEPTION_NAMES: Final[frozenset[str]] = frozenset(
    {"ReadTimeout", "ConnectTimeout", "APITimeoutError", "Timeout"}
)


def _is_connection_error(exc: BaseException) -> bool:
    """Treat any low-level network or timeout error as a retry-worthy event.

    LangChain wraps OpenAI client errors; the underlying httpx ``ConnectError``,
    ``ReadTimeout`` and ``RemoteProtocolError`` should all backoff. We
    detect by class-name suffix so this stays robust if LangChain changes
    the wrapping path.
    """
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
    # Walk the cause chain — LangChain wraps original errors in OutputParserException etc.
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_connection_error(cause)
    return False


def _is_timeout_error(exc: BaseException) -> bool:
    """Narrower variant of :func:`_is_connection_error` for the watchdog.

    Returns True only for exceptions where the LLM call hit a
    wall-time ceiling — not for generic network resets / RPC errors.
    The watchdog emits ``timeout`` / ``give_up`` events on this
    subset; the cascade's retry stack covers both.
    """
    name = type(exc).__name__
    if name in _TIMEOUT_EXCEPTION_NAMES:
        return True
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_timeout_error(cause)
    return False


def _build_m6_prompt_prefix(sections: dict[str, str], schema: type[BaseModel]) -> str:
    prefix = (
        sections["SYSTEM"] + "\n\n" + sections["EXAMPLES"] + "\n\n" + json_mode_instruction(schema)
    )
    # The user-message template is appended downstream as a separate
    # ChatMessage. A trailing newline here keeps any future suffix
    # concatenation from accidentally splicing across the last
    # schema-instruction token, which mlx-lm tokenises into a clean
    # boundary every time.
    if not prefix.endswith("\n"):
        prefix = prefix + "\n"
    return prefix


#: Pre-computed system-prompt prefix per spec § 7. Byte-stable so mlx-lm's
#: prefix cache keys against identical input across all pairs in a run.
#: ``tests/unit/test_judge.py::test_m6_prompt_prefix_is_byte_stable`` pins
#: the bytes against recorded fixtures.
_M6_PROMPT_PREFIX_FULL: Final[str] = _build_m6_prompt_prefix(
    _parse_prompt_sections(), WorkMatchDecision
)
_M6_PROMPT_PREFIX_FAST: Final[str] = _build_m6_prompt_prefix(
    _parse_prompt_sections_fast(), WorkMatchDecisionFast
)


def _build_chain(
    *,
    model_name: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.0,
    seed: int = 42,
    full_rationale: bool = True,
    timeout: int | None = None,
) -> Any:
    """Compose ``ChatOpenAI(...).with_structured_output(schema)``.

    ``full_rationale=True`` (default) uses the strict
    :class:`WorkMatchDecision` schema + the original prompt — every
    decision returns a substantive ≥ MIN_RATIONALE_CHARS rationale.

    ``full_rationale=False`` swaps in :class:`WorkMatchDecisionFast`
    and ``judge_v1_fast.txt``. The model is instructed (and
    structured-output schema permits) to set ``rationale=null`` for
    confident ``same_work``/``different_work`` decisions; rationale
    stays required for ``uncertain`` or ``confidence < 0.85``. Saves
    ~50-200 tokens per high-conf pair at the cost of a thinner
    natural-language audit trail.
    """
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    sections = _parse_prompt_sections() if full_rationale else _parse_prompt_sections_fast()
    schema: type[BaseModel] = WorkMatchDecision if full_rationale else WorkMatchDecisionFast
    # System prefix is the module-level byte-stable constant (P-02 Phase B);
    # pinning the bytes preserves the prefix-cache hit rate on mlx-lm.
    # ``method="json_mode"`` only sets ``response_format={"type":"json_object"}``
    # — LangChain does not auto-inject a schema description into the prompt.
    # Ollama tolerates that because ``format=json`` is constrained decoding;
    # mlx-lm 0.31 has no constrained-decoding fallback and otherwise copies
    # the few-shot prose. The instruction inside the prefix makes the JSON
    # contract explicit on both backends. P-02 A5.
    prefix = _M6_PROMPT_PREFIX_FULL if full_rationale else _M6_PROMPT_PREFIX_FAST
    template = ChatPromptTemplate.from_messages(
        [
            ("system", prefix),
            ("user", sections["USER"]),
        ]
    )
    llm_kwargs: dict[str, Any] = {
        "base_url": base_url,
        "api_key": SecretStr(api_key),
        "model": model_name,
        "temperature": temperature,
        "seed": seed,
    }
    if timeout is not None:
        # ChatOpenAI propagates ``timeout`` to the underlying httpx client.
        # httpx raises ``ReadTimeout`` when the server doesn't produce a
        # complete response within the budget — caught by the watchdog
        # path in :func:`judge_pair`.
        llm_kwargs["timeout"] = timeout
    llm = ChatOpenAI(**llm_kwargs)
    return template | llm.with_structured_output(schema, method="json_mode")
