"""M9 production picker — LangChain chain construction + retry loop.

``_build_picker_chain`` composes
``ChatPromptTemplate | ChatOpenAI(...).with_structured_output(PickerDecision)``;
:class:`LangChainLLMPicker` wraps the chain with the same validation-
retry / connection-retry policy the M6 judge uses, plus a
post-parse sanity check that the LLM's ``chosen_uri`` is in the
candidate-set the picker was asked to choose from.

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from pydantic import ValidationError

from bffi_pipeline.config import get_settings
from bffi_pipeline.llm_json_mode import json_mode_instruction
from bffi_pipeline.stages.m9.picker import (
    PICKER_STUB_PHRASES,
    PickerDecision,
)
from bffi_pipeline.stages.m9.picker_prompt import (
    _format_candidates_for_prompt,
    _parse_picker_prompt_sections,
)
from bffi_pipeline.stages.m9.schemas import AuthorityCandidate, EntityRequest

#: Validation retry: same shape as the M6 judge — max 2 retries on
#: parse / Boundary-4 failures (3 attempts total).
PICKER_MAX_VALIDATION_RETRIES: Final[int] = 2

#: Connection retry: 5 / 30 / 120 seconds backoff (3 retries, 4 attempts).
PICKER_MAX_CONNECTION_RETRIES: Final[int] = 3
PICKER_CONNECTION_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 30.0, 120.0)


def _is_picker_connection_error(exc: BaseException) -> bool:
    """Mirror :func:`bffi_pipeline.stages.m6._is_connection_error` for the picker."""
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
        return _is_picker_connection_error(cause)
    return False


def _build_picker_chain(
    *,
    model_name: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.0,
    seed: int = 42,
    request_timeout_seconds: float | None = None,
) -> Any:
    """Compose ``ChatOpenAI(...).with_structured_output(PickerDecision)``.

    ``request_timeout_seconds`` plumbs the per-call HTTP timeout
    (``LLM_CALL_TIMEOUT_SECONDS``) onto the LangChain client so a
    stuck mlx-lm response is bounded at the HTTP layer rather than
    relying solely on the orchestrator-level per-field budget (plan
    P-10 Phase A.4). ``None`` means "rely on LangChain's own default"
    — used by the existing single-threaded callers and tests.
    """
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    sections = _parse_picker_prompt_sections()
    # JSON-mode schema instruction — see judge.py / llm_json_mode.py for the
    # rationale (P-02 A5: mlx-lm 0.31 has no constrained decoding, model
    # otherwise copies few-shot prose).
    template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                sections["SYSTEM"]
                + "\n\n"
                + sections["EXAMPLES"]
                + "\n\n"
                + json_mode_instruction(PickerDecision),
            ),
            ("user", sections["USER"]),
        ]
    )
    chat_kwargs: dict[str, Any] = {
        "base_url": base_url,
        "api_key": SecretStr(api_key),
        "model": model_name,
        "temperature": temperature,
        "seed": seed,
    }
    if request_timeout_seconds is not None:
        chat_kwargs["request_timeout"] = request_timeout_seconds
    llm = ChatOpenAI(**chat_kwargs)
    return template | llm.with_structured_output(PickerDecision, method="json_mode")


def _picker_uncertain(reason: str) -> PickerDecision:
    """Build a fall-through 'uncertain' decision when the chain can't produce one."""
    cleaned = reason.strip() or "no error message available"
    lowered = cleaned.lower()
    for phrase in PICKER_STUB_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            cleaned = re.sub(
                rf"\b{re.escape(phrase)}\b",
                "[stub phrase elided]",
                cleaned,
                flags=re.IGNORECASE,
            )
    rationale = f"Picker fell through to uncertain after retries exhausted: {cleaned}"
    return PickerDecision(
        decision="uncertain",
        chosen_uri=None,
        confidence=0.0,
        rationale=rationale,
    )


@dataclass
class LangChainLLMPicker:
    """Production picker that calls the local Qwen3 cascade via LangChain.

    Phase-1 callers passed a ``StubPicker``; the production CLI builds
    one of these. Validation-failure retry (max 2) and connection-error
    retry (5 / 30 / 120 s, max 3) mirror the M6 judge's policies so
    cataloguers see one consistent failure-handling story across stages.

    The picker also enforces a *post-parse* check: the LLM's
    ``chosen_uri`` must be one of the URIs the authority client actually
    returned. A pick of an out-of-set URI falls through to ``uncertain``
    with the offending URI in the rationale, never silently bound to a
    bad authority.
    """

    model_name: str | None = None
    chain: Any = None
    sleep: Callable[[float], None] = time.sleep

    def _resolved_chain(self) -> Any:
        if self.chain is not None:
            return self.chain
        settings = get_settings()
        return _build_picker_chain(
            model_name=self.model_name or settings.llm_model_primary,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            request_timeout_seconds=float(settings.llm_call_timeout_seconds),
        )

    def pick(
        self,
        *,
        request: EntityRequest,
        candidates: list[AuthorityCandidate],
    ) -> PickerDecision:
        """Invoke the configured chain with retries and the candidate-set sanity check."""
        if not candidates:
            return PickerDecision(
                decision="uncertain",
                chosen_uri=None,
                confidence=0.0,
                rationale="No candidates supplied; nothing to pick from.",
            )

        chain = self._resolved_chain()
        invoke_payload = {
            "input_literal": request.literal,
            "source_vocabulary": candidates[0].source_vocabulary,
            "candidates": _format_candidates_for_prompt(candidates),
        }

        connection_attempts = 0
        validation_attempts = 0
        last_error = "unknown failure"

        while True:
            try:
                raw = chain.invoke(invoke_payload)
            except Exception as exc:
                if _is_picker_connection_error(exc):
                    if connection_attempts < PICKER_MAX_CONNECTION_RETRIES:
                        self.sleep(PICKER_CONNECTION_BACKOFF_SECONDS[connection_attempts])
                        connection_attempts += 1
                        last_error = (
                            f"connection error after {connection_attempts} retry(ies): {exc!s}"
                        )
                        continue
                    last_error = (
                        f"connection error after {PICKER_MAX_CONNECTION_RETRIES} retries "
                        f"exhausted: {exc!s}"
                    )
                    break
                last_error = f"unrecoverable LLM error: {exc!s}"
                break

            try:
                if isinstance(raw, PickerDecision):
                    decision = raw
                else:
                    decision = PickerDecision.model_validate(raw)
            except (ValidationError, ValueError) as exc:
                if validation_attempts < PICKER_MAX_VALIDATION_RETRIES:
                    validation_attempts += 1
                    last_error = f"validation failure (attempt {validation_attempts}): {exc!s}"
                    continue
                last_error = (
                    f"validation failed after {PICKER_MAX_VALIDATION_RETRIES} retries: {exc!s}"
                )
                break

            # Post-parse sanity check: the chosen URI must be in the candidate set.
            if decision.decision == "chose":
                allowed = {c.uri for c in candidates}
                if decision.chosen_uri not in allowed:
                    return PickerDecision(
                        decision="uncertain",
                        chosen_uri=None,
                        confidence=0.0,
                        rationale=(
                            f"LLM picked {decision.chosen_uri!r} which is not in the "
                            f"candidate URI set; falling through to uncertain."
                        ),
                    )
            return decision

        return _picker_uncertain(last_error)
