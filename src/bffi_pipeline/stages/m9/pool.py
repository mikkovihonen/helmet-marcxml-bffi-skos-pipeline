"""M9 Phase 1 + Phase 2 concurrent dispatch helpers.

Phase 1 (tier-0 + candidate query) and Phase 2 (LLM picker call) each
have a sequential and a thread-pool variant; the orchestrator
(:mod:`apply`) selects based on the per-phase concurrency setting.
Worker results are sorted by submission ``idx`` before merging so
graph mutation + provenance emission stay deterministic regardless of
completion order.

Picker calls run inside a per-field wall budget
(:func:`_picker_call_with_budget`) so a stuck mlx-lm request can't
freeze a long batch run — the field falls through to tier-3 fallback
with ``was_watchdog_aborted=True``.

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.observability.watchdog import emit_watchdog_event
from bffi_pipeline.stages.m9.authority_clients import AuthorityClient
from bffi_pipeline.stages.m9.decisions import (
    _decide_before_picker,
    _decide_with_pick,
    _fictional_outcome,
    _local_outcome,
    _watchdog_aborted_outcome,
)
from bffi_pipeline.stages.m9.local_concept_resolver import LocalConceptResolver
from bffi_pipeline.stages.m9.picker import LLMPicker
from bffi_pipeline.stages.m9.schemas import (
    _M9_PROGRESS_CADENCE,
    LEXICAL_FLOOR,
    PICKER_ORDERING_SUBMISSION,
    STAGE_FALLBACK,
    STAGE_LLM,
    AuthorityCandidate,
    EntityRequest,
    PickerOrdering,
    ReconciliationOutcome,
)


@dataclass(frozen=True)
class _Phase1Result:
    """One Phase 1 (tier-0 + candidate query) outcome.

    Either ``outcome`` is set (the entity resolved at fictional /
    tier-0 / lexical / no-candidate, picker dispatch unnecessary) or
    ``sorted_candidates`` is set (the entity needs tier-2 picker
    dispatch in Phase 2). Never both, never neither.
    """

    idx: int
    request: EntityRequest
    outcome: ReconciliationOutcome | None
    sorted_candidates: list[AuthorityCandidate] | None
    started_at: datetime


def _phase1_resolve_one(
    *,
    idx: int,
    request: EntityRequest,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None,
    top_k: int,
    local_resolver: LocalConceptResolver | None,
) -> _Phase1Result:
    """Run tier-0 + candidate query for one entity.

    Stateless worker — all dependencies passed in. Thread-safe given
    that ``client``, ``fallback_client``, and ``local_resolver`` are
    HTTP-client-backed and stateless.
    """
    started = datetime.now(UTC)
    # Fictional-character marker short-circuit (tier-0 sibling).
    if request.kind == "fictional_character":
        return _Phase1Result(
            idx=idx,
            request=request,
            outcome=_fictional_outcome(request),
            sorted_candidates=None,
            started_at=started,
        )
    # Tier-0: local exact-prefLabel match.
    if local_resolver is not None:
        hit = local_resolver.resolve(literal=request.literal, kind=request.kind)
        if hit is not None:
            return _Phase1Result(
                idx=idx,
                request=request,
                outcome=_local_outcome(request, hit),
                sorted_candidates=None,
                started_at=started,
            )
    # Authority client candidate query.
    candidates = client.query(request=request, top_k=top_k)
    if not candidates and fallback_client is not None:
        candidates = fallback_client.query(request=request, top_k=top_k)
    # Tier-1 short-circuit OR queue for picker dispatch.
    outcome_or_none, sorted_candidates = _decide_before_picker(
        request=request, candidates=candidates
    )
    if outcome_or_none is not None:
        return _Phase1Result(
            idx=idx,
            request=request,
            outcome=outcome_or_none,
            sorted_candidates=None,
            started_at=started,
        )
    return _Phase1Result(
        idx=idx,
        request=request,
        outcome=None,
        sorted_candidates=sorted_candidates,
        started_at=started,
    )


def _phase1_seq(
    request_list: list[EntityRequest],
    *,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None,
    top_k: int,
    local_resolver: LocalConceptResolver | None,
) -> list[_Phase1Result]:
    """Sequential (``phase1_concurrency <= 1``) path through Phase 1."""
    return [
        _phase1_resolve_one(
            idx=idx,
            request=request,
            client=client,
            fallback_client=fallback_client,
            top_k=top_k,
            local_resolver=local_resolver,
        )
        for idx, request in enumerate(request_list)
    ]


def _phase1_pool(
    request_list: list[EntityRequest],
    *,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None,
    top_k: int,
    local_resolver: LocalConceptResolver | None,
    phase1_concurrency: int,
) -> list[_Phase1Result]:
    """Concurrent (``phase1_concurrency >= 2``) path through Phase 1.

    Workers share the orchestrator's ``client`` / ``fallback_client`` /
    ``local_resolver`` — all built on ``httpx.Client`` (thread-safe)
    plus stateless SPARQL queries. Results are sorted by submission
    index so downstream graph mutations + provenance emit
    deterministically regardless of completion order.
    """
    results: list[_Phase1Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=phase1_concurrency) as pool:
        futures = [
            pool.submit(
                _phase1_resolve_one,
                idx=idx,
                request=request,
                client=client,
                fallback_client=fallback_client,
                top_k=top_k,
                local_resolver=local_resolver,
            )
            for idx, request in enumerate(request_list)
        ]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: r.idx)
    return results


def _field_id(request: EntityRequest) -> str:
    """Stable key for one M9 reconciliation field.

    Used as the ``pair_id`` argument when emitting watchdog events
    (the watchdog API uses ``pair_id`` for both M6 pairs and M9
    fields). The format mirrors what cataloguers see in the
    canonical graph: ``<work_uri>|<predicate>|<literal>``.
    """
    predicate = request.predicate_uri or request.kind
    return f"{request.work_uri}|{predicate}|{request.literal}"


def _picker_queue_sort_key(
    entry: tuple[int, EntityRequest, list[AuthorityCandidate]],
) -> tuple[str, str, str, str]:
    """Sort key for the deferred picker queue (P-10 Phase E).

    Orders entries so that consecutive ``POST /v1/chat/completions``
    calls share the longest possible prompt prefix — mlx-lm's
    prompt-prefix cache then collapses per-call wall to roughly
    decode-time on runs of same-kind / same-vocabulary calls.

    Key, in order:

    1. ``request.kind`` — clusters fictional-character picks together,
       then person, then corporate_body, etc. The picker prompt has
       kind-conditional sections in ``prompts/picker_v1.txt``, so picks
       of the same kind share the longest static prompt prefix.
    2. ``candidates[0].source_vocabulary`` — within a kind, cluster by
       the dominant candidate vocabulary (``yso``, ``finaf``, ``kauno``,
       ``viaf``, …). Same-vocabulary candidates share authority-style
       formatting in the rendered candidate list.
    3. A stable fingerprint of ``sorted(c.uri for c in candidates)`` —
       within a kind+vocab cluster, group calls with overlapping
       candidate sets. Identical / near-identical candidate sets share
       long prompt-body prefixes.
    4. ``request.literal`` — final tie-breaker for byte-stability
       across runs (the literal varies last in the prompt).

    Output of ``_apply_reconciliation`` is byte-stable regardless of the
    ordering chosen, because the orchestrator sorts ``picker_results`` by
    submission ``idx`` before applying graph mutations.
    """
    _idx, request, candidates = entry
    vocab = candidates[0].source_vocabulary if candidates else ""
    fingerprint = "|".join(sorted(c.uri for c in candidates))
    return (request.kind, vocab, fingerprint, request.literal)


def _order_deferred_picker_queue(
    deferred: list[tuple[int, EntityRequest, list[AuthorityCandidate]]],
    *,
    ordering: PickerOrdering,
) -> list[tuple[int, EntityRequest, list[AuthorityCandidate]]]:
    """Return ``deferred`` in the order requested by ``ordering``.

    The orchestrator dispatches the returned list to the picker pool;
    the result-merge that follows re-sorts by submission ``idx`` so the
    canonical Turtle is byte-stable across both ordering modes. See
    :func:`_picker_queue_sort_key` for the prefix-cache key.
    """
    if ordering == PICKER_ORDERING_SUBMISSION:
        return deferred
    # ``prefix-cache`` — Python's ``sorted`` is stable, so equal keys
    # preserve their submission order (deterministic tie-break).
    return sorted(deferred, key=_picker_queue_sort_key)


def _emit_picker_progress(
    completed: int,
    *,
    total: int,
    cache_hits: int,
    watchdog_aborted: int,
    llm_pick: int,
    fallback: int,
) -> None:
    """Emit one M9 Phase 2 ``progress`` event.

    Centralised here so the seq + pool paths share one payload shape;
    the dashboard's m9 progress panel can render both cold and warm
    runs without per-path branching. P-12 Phase D.

    ``llm_pick`` and ``fallback`` are mid-run cumulative tier counts
    the exporter mirrors into ``bffi_stage_outcomes_total`` so the
    dashboard's M9 outcome bargauge populates live during Phase 2
    instead of jumping from empty to fully populated at the ``end``
    event.
    """
    emit_if_active(
        stage="m9",
        event="progress",
        phase="phase2",
        counters={"processed": completed, "total": total},
        extra={
            "cache_hits": cache_hits,
            "watchdog_aborted": watchdog_aborted,
            "llm_pick": llm_pick,
            "fallback": fallback,
        },
    )


def _picker_call_with_budget(
    *,
    picker: LLMPicker,
    request: EntityRequest,
    sorted_candidates: list[AuthorityCandidate],
    field_timeout_seconds: int,
    model_name: str,
    watchdog_sidecar_path: Path | None,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
) -> tuple[ReconciliationOutcome, int]:
    """Run ``picker.pick`` with a per-field wall budget.

    Returns ``(outcome, watchdog_event_count)``. When the budget is
    exceeded, the outcome is a tier-3 fallback marked
    ``was_watchdog_aborted=True`` and one
    ``field_budget_exceeded`` event is emitted to stderr +
    ``watchdog_sidecar_path``. ``field_timeout_seconds <= 0`` disables
    budget enforcement (test / rollback use case).

    Budget enforcement uses a single-thread ``ThreadPoolExecutor``
    inside the worker so a stuck picker call doesn't block the outer
    thread's progress. The inner thread is then ``shutdown(wait=False)``
    — the stuck call eventually completes (bounded by
    ``LLM_CALL_TIMEOUT_SECONDS`` times the picker retry count via the
    underlying httpx client) and reclaims its slot.
    """
    pair_id = _field_id(request)
    started = time.monotonic()

    if field_timeout_seconds <= 0:
        pick = picker.pick(request=request, candidates=sorted_candidates)
        outcome = _decide_with_pick(
            request=request,
            sorted_candidates=sorted_candidates,
            pick=pick,
            lexical_fallback_floor=lexical_fallback_floor,
            lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
            disable_fallback=disable_fallback,
        )
        return outcome, 0

    inner = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix=f"picker-budget-{pair_id[:32]}"
    )
    fut = inner.submit(picker.pick, request=request, candidates=sorted_candidates)
    try:
        pick = fut.result(timeout=field_timeout_seconds)
    except concurrent.futures.TimeoutError:
        elapsed = time.monotonic() - started
        inner.shutdown(wait=False)
        emit_watchdog_event(
            pair_id=pair_id,
            event="field_budget_exceeded",
            model_name=model_name,
            elapsed_seconds=elapsed,
            retry_n=0,
            sidecar_path=watchdog_sidecar_path,
        )
        outcome = _watchdog_aborted_outcome(
            request=request,
            sorted_candidates=sorted_candidates,
            elapsed_seconds=elapsed,
            budget_seconds=field_timeout_seconds,
        )
        return outcome, 1

    inner.shutdown(wait=False)
    outcome = _decide_with_pick(
        request=request,
        sorted_candidates=sorted_candidates,
        pick=pick,
        lexical_fallback_floor=lexical_fallback_floor,
        lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
        disable_fallback=disable_fallback,
    )
    return outcome, 0


def _picker_phase_seq(
    deferred: list[tuple[int, EntityRequest, list[AuthorityCandidate]]],
    *,
    picker: LLMPicker,
    field_timeout_seconds: int,
    model_name: str,
    watchdog_sidecar_path: Path | None,
    progress_cadence: int = _M9_PROGRESS_CADENCE,
    cache_hits: int = 0,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
) -> list[tuple[int, ReconciliationOutcome]]:
    """Sequential (c=1) path: call the shared picker inline per field.

    P-12 Phase D: emit a ``progress`` event every ``progress_cadence``
    completed calls. ``cache_hits`` is fixed at Phase-1.5 exit time so
    the caller passes it in once; ``watchdog_aborted`` is tallied
    locally from the results stream.

    P-16: ``lexical_fallback_floor`` / ``lexical_fallback_floor_per_vocab``
    / ``disable_fallback`` forward to :func:`_decide_with_pick` to gate
    the tier-3 fallback. Defaults preserve pre-P-16 behaviour.
    """
    results: list[tuple[int, ReconciliationOutcome]] = []
    watchdog_aborted = 0
    llm_pick = 0
    fallback = 0
    for idx, request, sorted_candidates in deferred:
        outcome, _events = _picker_call_with_budget(
            picker=picker,
            request=request,
            sorted_candidates=sorted_candidates,
            field_timeout_seconds=field_timeout_seconds,
            model_name=model_name,
            watchdog_sidecar_path=watchdog_sidecar_path,
            lexical_fallback_floor=lexical_fallback_floor,
            lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
            disable_fallback=disable_fallback,
        )
        results.append((idx, outcome))
        if outcome.was_watchdog_aborted:
            watchdog_aborted += 1
        if outcome.stage == STAGE_LLM:
            llm_pick += 1
        elif outcome.stage == STAGE_FALLBACK:
            fallback += 1
        if progress_cadence > 0 and len(results) % progress_cadence == 0:
            _emit_picker_progress(
                len(results),
                total=len(deferred),
                cache_hits=cache_hits,
                watchdog_aborted=watchdog_aborted,
                llm_pick=llm_pick,
                fallback=fallback,
            )
    # End-of-phase flush: emit one final progress event when the run
    # didn't land on a cadence boundary, so the dashboard's processed
    # gauge ends at 100 % of the phase total instead of plateauing at
    # the last cadence multiple.
    if progress_cadence > 0 and len(results) > 0 and len(results) % progress_cadence != 0:
        _emit_picker_progress(
            len(results),
            total=len(deferred),
            cache_hits=cache_hits,
            watchdog_aborted=watchdog_aborted,
            llm_pick=llm_pick,
            fallback=fallback,
        )
    return results


def _picker_phase_pool(
    deferred: list[tuple[int, EntityRequest, list[AuthorityCandidate]]],
    *,
    picker_factory: Callable[[], LLMPicker],
    concurrency: int,
    field_timeout_seconds: int,
    model_name: str,
    watchdog_sidecar_path: Path | None,
    progress_cadence: int = _M9_PROGRESS_CADENCE,
    cache_hits: int = 0,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
) -> list[tuple[int, ReconciliationOutcome]]:
    """Concurrent (c>=2) path: thread-local pickers, parallel dispatch.

    Each worker thread constructs its own ``LLMPicker`` on first use
    via ``threading.local()``. LangChain's underlying OpenAI-compat
    client has no documented thread-safety guarantee, and building
    one picker per worker is cheap.

    Worker results are collected and returned in submission-index
    order so the caller can apply graph mutations deterministically
    regardless of completion order.

    P-12 Phase D: the orchestrator-side ``as_completed`` loop is
    single-threaded, so the cadence counter + emit run inline there
    (no worker-thread emission, no shared lock). Each completed
    future increments ``completed``; on a cadence boundary the
    progress event fires with the running counts of cache hits +
    watchdog-aborted picks so the dashboard surfaces picker stress
    live.
    """
    thread_local = threading.local()
    _RunArgs = tuple[int, EntityRequest, list[AuthorityCandidate]]
    _RunResult = tuple[int, ReconciliationOutcome]

    def _run(args: _RunArgs) -> _RunResult:
        idx, request, sorted_candidates = args
        picker = getattr(thread_local, "picker", None)
        if picker is None:
            picker = picker_factory()
            thread_local.picker = picker
        outcome, _events = _picker_call_with_budget(
            picker=picker,
            request=request,
            sorted_candidates=sorted_candidates,
            field_timeout_seconds=field_timeout_seconds,
            model_name=model_name,
            watchdog_sidecar_path=watchdog_sidecar_path,
            lexical_fallback_floor=lexical_fallback_floor,
            lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
            disable_fallback=disable_fallback,
        )
        return idx, outcome

    results: list[tuple[int, ReconciliationOutcome]] = []
    watchdog_aborted = 0
    llm_pick = 0
    fallback = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_run, item) for item in deferred]
        for fut in concurrent.futures.as_completed(futures):
            idx, outcome = fut.result()
            results.append((idx, outcome))
            if outcome.was_watchdog_aborted:
                watchdog_aborted += 1
            if outcome.stage == STAGE_LLM:
                llm_pick += 1
            elif outcome.stage == STAGE_FALLBACK:
                fallback += 1
            if progress_cadence > 0 and len(results) % progress_cadence == 0:
                _emit_picker_progress(
                    len(results),
                    total=len(deferred),
                    cache_hits=cache_hits,
                    watchdog_aborted=watchdog_aborted,
                    llm_pick=llm_pick,
                    fallback=fallback,
                )
    # End-of-phase flush — see _picker_phase_seq for rationale.
    if progress_cadence > 0 and len(results) > 0 and len(results) % progress_cadence != 0:
        _emit_picker_progress(
            len(results),
            total=len(deferred),
            cache_hits=cache_hits,
            watchdog_aborted=watchdog_aborted,
            llm_pick=llm_pick,
            fallback=fallback,
        )
    results.sort(key=lambda t: t[0])
    return results
