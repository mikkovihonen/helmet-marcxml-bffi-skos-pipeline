"""M6 cascade — :func:`judge_pair` (single-call, retry + cache) and
:func:`cascade_judge` (32 B primary → 72 B fallback).

Spec § 7 protocols:

- :data:`MAX_VALIDATION_RETRIES` parse / Boundary-4 retries
  (3 LLM attempts total) on each ``judge_pair`` call.
- :data:`MAX_CONNECTION_RETRIES` connection-error / timeout retries
  with exponential backoff
  (:data:`CONNECTION_BACKOFF_SECONDS` per retry; 4 attempts total).
- :func:`cascade_judge` escalates from primary → fallback when the
  primary returns ``uncertain`` or ``same_work`` below
  :data:`FALLBACK_CONFIDENCE_THRESHOLD`.

Watchdog events (P-03) fire when individual calls or the
shared-per-pair budget exceed the configured ceilings — the pair
lands as ``uncertain`` with the watchdog stage tag.

P-38 Phase D: extracted from m6/runner.py. No logic change.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.watchdog import emit_watchdog_event
from bffi_pipeline.stages.m6.cache import JudgeCache, _cache_key, default_cache_path
from bffi_pipeline.stages.m6.clients import (
    _build_chain,
    _is_connection_error,
    _is_timeout_error,
)
from bffi_pipeline.stages.m6.outcome import (
    STAGE_PRIMARY,
    STAGE_SECOND_OPINION,
    CascadeStep,
    JudgeOutcome,
    _uncertain_decision,
)
from bffi_pipeline.stages.m6.prompts import prompt_hash, prompt_hash_fast
from bffi_pipeline.stages.m6.validation import (
    WorkMatchDecision,
    WorkMatchDecisionFast,
    WorkRecord,
)

#: Confidence cutoff below which the primary's ``same_work`` decision is
#: re-run on the 72 B fallback. Documented in spec § 7 / docs/local-inference.md.
FALLBACK_CONFIDENCE_THRESHOLD: Final[float] = 0.85

#: Validation retry: spec § 7 calls for max 2 retries on parse / Boundary-4
#: failures. The total number of LLM attempts is therefore 3.
MAX_VALIDATION_RETRIES: Final[int] = 2

#: Connection retry: spec § 7 calls for max 3 retries with exponential
#: backoff after a connection error or timeout. Total attempts = 4.
MAX_CONNECTION_RETRIES: Final[int] = 3
CONNECTION_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 30.0, 120.0)

#: Type alias for the injectable chain — anything with .invoke({record_a, record_b, sim}).
ChainLike = Any


def judge_pair(  # noqa: PLR0912, PLR0915 — two retry layers (connection + validation) keep this single-purpose, splitting would scatter state.
    record_a: WorkRecord,
    record_b: WorkRecord,
    sim: float,
    *,
    model_name: str | None = None,
    base_url: str | None = None,
    chain: ChainLike | None = None,
    cache: JudgeCache | None = None,
    sleep: Callable[[float], None] = time.sleep,
    full_rationale: bool = True,
    watchdog_sidecar_path: Path | None = None,
    pair_deadline: float | None = None,
) -> tuple[WorkMatchDecision, bool, float]:
    """Judge a single Work-pair with retry, post-validation cache.

    Returns ``(decision, cache_hit, latency_seconds)``. ``cache_hit`` lets
    the caller (e.g. cascade_judge / the future batch driver) record
    whether this answer cost an LLM call.

    ``chain`` and ``cache`` are injection points for tests; production
    callers leave them ``None`` so the defaults — a fresh
    ``ChatOpenAI`` chain pointed at the configured base URL, and the
    SQLite cache under ``data_dir`` — are constructed lazily.

    ``full_rationale=True`` (default) uses the strict
    :class:`WorkMatchDecision` schema; the LLM must produce a
    substantive rationale on every call. ``full_rationale=False``
    swaps in :class:`WorkMatchDecisionFast` + ``judge_v1_fast.txt`` so
    confident ``same_work``/``different_work`` decisions may set
    ``rationale=null``; the boundary conversion synthesises a
    placeholder rationale from ``matching_fields`` /
    ``diverging_fields`` so cached + JSONL + provenance outputs see
    one unified shape regardless of mode.
    """
    settings = get_settings()
    effective_model = model_name or settings.llm_model_primary
    effective_base_url = base_url or settings.llm_base_url
    chain = chain or _build_chain(
        model_name=effective_model,
        base_url=effective_base_url,
        api_key=settings.llm_api_key,
        full_rationale=full_rationale,
        timeout=settings.llm_call_timeout_seconds,
    )
    pair_id = f"{record_a.record_id}+{record_b.record_id}"

    own_cache = cache is None
    if own_cache:
        cache = JudgeCache(default_cache_path())

    started = time.monotonic()
    try:
        # Prompt hash discriminates strict vs. fast — a re-run that
        # flips the flag invalidates cached entries automatically.
        ph = prompt_hash() if full_rationale else prompt_hash_fast()
        key = _cache_key(
            model_name=effective_model,
            prompt_hash_value=ph,
            record_a=record_a,
            record_b=record_b,
        )

        cached = cache.get(key) if cache else None
        if cached is not None:
            return cached, True, time.monotonic() - started

        invoke_payload = {
            "record_a": record_a.model_dump_json(indent=2, exclude_none=True),
            "record_b": record_b.model_dump_json(indent=2, exclude_none=True),
            "sim": sim,
        }

        connection_attempts = 0
        validation_attempts = 0
        last_error: str = "unknown failure"
        schema: type[BaseModel] = WorkMatchDecision if full_rationale else WorkMatchDecisionFast

        while True:
            # Per-pair budget check (plan P-03 Phase B). Fires when
            # cumulative wall time across cascade tiers + retries
            # exceeds the budget — orthogonal to the per-call
            # ceiling. No further retries; the pair lands as
            # ``uncertain``.
            if pair_deadline is not None and time.monotonic() > pair_deadline:
                emit_watchdog_event(
                    pair_id=pair_id,
                    event="pair_budget_exceeded",
                    model_name=effective_model,
                    elapsed_seconds=time.monotonic() - started,
                    retry_n=connection_attempts,
                    sidecar_path=watchdog_sidecar_path,
                )
                last_error = (
                    "pair budget exceeded — cumulative cascade wall time "
                    "passed LLM_PAIR_TIMEOUT_SECONDS"
                )
                break
            try:
                raw = chain.invoke(invoke_payload)
            except Exception as exc:
                if _is_connection_error(exc):
                    if _is_timeout_error(exc):
                        # Surface the wedge to the operator + audit trail.
                        # Same retry behaviour as any other connection
                        # error, but tagged distinctly so the dry-run
                        # bench can count specifically watchdog events.
                        emit_watchdog_event(
                            pair_id=pair_id,
                            event="timeout",
                            model_name=effective_model,
                            elapsed_seconds=time.monotonic() - started,
                            retry_n=connection_attempts,
                            sidecar_path=watchdog_sidecar_path,
                        )
                    if connection_attempts < MAX_CONNECTION_RETRIES:
                        sleep(CONNECTION_BACKOFF_SECONDS[connection_attempts])
                        connection_attempts += 1
                        last_error = (
                            f"connection error after {connection_attempts} retry(ies): {exc!s}"
                        )
                        continue
                    if _is_timeout_error(exc):
                        emit_watchdog_event(
                            pair_id=pair_id,
                            event="give_up",
                            model_name=effective_model,
                            elapsed_seconds=time.monotonic() - started,
                            retry_n=connection_attempts,
                            sidecar_path=watchdog_sidecar_path,
                        )
                    last_error = (
                        f"connection error after {MAX_CONNECTION_RETRIES} retries exhausted: "
                        f"{exc!s}"
                    )
                    break
                last_error = f"unrecoverable LLM error: {exc!s}"
                break

            try:
                parsed = raw if isinstance(raw, schema) else schema.model_validate(raw)
            except (ValidationError, ValueError) as exc:
                if validation_attempts < MAX_VALIDATION_RETRIES:
                    validation_attempts += 1
                    last_error = f"validation failure (attempt {validation_attempts}): {exc!s}"
                    continue
                last_error = f"validation failed after {MAX_VALIDATION_RETRIES} retries: {exc!s}"
                break

            # Convert fast → strict at the boundary so downstream code
            # (cache.set, JSONL serialiser, provenance writer) sees one
            # consistent schema regardless of mode.
            if isinstance(parsed, WorkMatchDecisionFast):
                decision: WorkMatchDecision = parsed.to_strict()
            else:
                decision = parsed  # type: ignore[assignment]

            if cache is not None:
                cache.set(key, decision, model_name=effective_model, prompt_hash_value=ph)
            return decision, False, time.monotonic() - started

        return _uncertain_decision(last_error), False, time.monotonic() - started
    finally:
        if own_cache and cache is not None:
            cache.close()


def _needs_second_opinion(decision: WorkMatchDecision) -> bool:
    if decision.decision == "uncertain":
        return True
    return decision.decision == "same_work" and decision.confidence < FALLBACK_CONFIDENCE_THRESHOLD


def cascade_judge(
    record_a: WorkRecord,
    record_b: WorkRecord,
    sim: float,
    *,
    primary_model: str | None = None,
    fallback_model: str | None = None,
    primary_base_url: str | None = None,
    fallback_base_url: str | None = None,
    primary_chain: ChainLike | None = None,
    fallback_chain: ChainLike | None = None,
    cache: JudgeCache | None = None,
    sleep: Callable[[float], None] = time.sleep,
    full_rationale: bool = True,
    watchdog_sidecar_path: Path | None = None,
) -> JudgeOutcome:
    """Two-stage cascade per spec § 7 / docs/local-inference.md.

    Runs the primary model first; re-runs the fallback model when the
    primary returns ``uncertain`` or ``same_work`` with confidence below
    :data:`FALLBACK_CONFIDENCE_THRESHOLD`. Both decisions are returned
    in :attr:`JudgeOutcome.steps` so the future provenance writer can
    log them with the ``llm-judge-primary`` and
    ``llm-judge-second-opinion`` ``bffi-prov:stage`` tags.

    ``full_rationale`` is propagated to both per-stage ``judge_pair``
    calls. The fast-mode prompt instructs the LLM to skip rationale
    for high-confidence ``same_work``/``different_work``; the fallback
    stage always fires for ``uncertain`` or low-confidence primaries
    where rationale is required regardless of mode.
    """
    settings = get_settings()
    primary_name = primary_model or settings.llm_model_primary
    fallback_name = fallback_model or settings.llm_model_fallback

    # Per-tier base URL resolution (plan P-02 § D1). Explicit kwarg
    # wins; otherwise fall through to the per-tier env var; otherwise
    # to the single ``llm_base_url`` (Ollama-shaped setups where one
    # process serves both models). Empty string means "unset".
    primary_url = primary_base_url or settings.llm_base_url_primary or settings.llm_base_url
    fallback_url = fallback_base_url or settings.llm_base_url_fallback or settings.llm_base_url

    # Per-pair wall-time ceiling (P-03 Phase B). Single deadline
    # shared across both cascade tiers — the budget belongs to the
    # pair, not the individual model attempt.
    pair_deadline: float | None = None
    if settings.llm_pair_timeout_seconds and settings.llm_pair_timeout_seconds > 0:
        pair_deadline = time.monotonic() + settings.llm_pair_timeout_seconds

    own_cache = cache is None
    if own_cache:
        cache = JudgeCache(default_cache_path())

    try:
        primary_decision, primary_cache_hit, primary_latency = judge_pair(
            record_a,
            record_b,
            sim,
            model_name=primary_name,
            base_url=primary_url,
            chain=primary_chain,
            cache=cache,
            sleep=sleep,
            full_rationale=full_rationale,
            watchdog_sidecar_path=watchdog_sidecar_path,
            pair_deadline=pair_deadline,
        )
        steps = [
            CascadeStep(
                stage=STAGE_PRIMARY,
                model_name=primary_name,
                decision=primary_decision,
                cache_hit=primary_cache_hit,
                latency_seconds=primary_latency,
            )
        ]
        if not _needs_second_opinion(primary_decision):
            return JudgeOutcome(final=primary_decision, steps=steps)

        fallback_decision, fallback_cache_hit, fallback_latency = judge_pair(
            record_a,
            record_b,
            sim,
            model_name=fallback_name,
            base_url=fallback_url,
            chain=fallback_chain,
            cache=cache,
            sleep=sleep,
            full_rationale=full_rationale,
            watchdog_sidecar_path=watchdog_sidecar_path,
            pair_deadline=pair_deadline,
        )
        steps.append(
            CascadeStep(
                stage=STAGE_SECOND_OPINION,
                model_name=fallback_name,
                decision=fallback_decision,
                cache_hit=fallback_cache_hit,
                latency_seconds=fallback_latency,
            )
        )
        return JudgeOutcome(final=fallback_decision, steps=steps)
    finally:
        if own_cache and cache is not None:
            cache.close()
