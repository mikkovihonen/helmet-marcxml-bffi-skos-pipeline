"""M9 operator entry points — :func:`reconcile_one` and :func:`apply_reconciliation`.

Three-phase orchestration:

1. **Phase 1** — walk requests through tier-0 (local exact-prefLabel)
   + Finto/VIAF candidate query; emit one ``_Phase1Result`` per
   request. Sequential or thread-pool sized by ``phase1_concurrency``.
2. **Phase 1.5** — consult the persistent picker cache (P-10 Phase B)
   for entries that need tier-2. Cache hits short-circuit Phase 2;
   misses queue for the LLM dispatch.
3. **Phase 2** — picker dispatch (sequential or thread-pool sized by
   ``concurrency``); each call wrapped in a per-field wall budget.
4. **Phase 3** — apply graph mutations + emit provenance Activities
   in submission order; write cache back where Phase 2 produced
   fresh verdicts.

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rdflib import Graph

from bffi_pipeline.cataloguer_review import append_target_row
from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.stages.m9.authority_clients import AuthorityClient
from bffi_pipeline.stages.m9.decisions import (
    _decide_with_pick,
    _fictional_outcome,
    _format_m9_details,
    _local_outcome,
    decide_reconciliation,
)
from bffi_pipeline.stages.m9.graph_mutate import (
    _apply_canonical_link,
    _bump_admin_metadata,
    _emit_provenance,
    _load_canonical_bib_ids,
)
from bffi_pipeline.stages.m9.local_concept_resolver import LocalConceptResolver
from bffi_pipeline.stages.m9.picker import LLMPicker, PickerDecision
from bffi_pipeline.stages.m9.picker_cache import (
    CacheHit,
    PickerCache,
    compute_finto_shas,
    compute_picker_cache_key,
)
from bffi_pipeline.stages.m9.picker_prompt import picker_prompt_hash
from bffi_pipeline.stages.m9.pool import (
    _phase1_pool,
    _phase1_seq,
    _Phase1Result,
    _picker_phase_pool,
    _picker_phase_seq,
)
from bffi_pipeline.stages.m9.probe import _m9_probe_dependencies
from bffi_pipeline.stages.m9.requests import _iter_creator_requests, _iter_subject_requests
from bffi_pipeline.stages.m9.schemas import (
    _CREATOR_KINDS,
    _M9_HEALTH_PROBE_CADENCE,
    _MIN_CONCURRENCY_FOR_FACTORY,
    _SUBJECT_KINDS,
    ALL_AUTHORITY_KINDS,
    DEFAULT_TOP_K,
    LEXICAL_FLOOR,
    PICKER_ORDERING_PREFIX_CACHE,
    STAGE_FALLBACK,
    STAGE_FICTIONAL,
    STAGE_LEXICAL,
    STAGE_LLM,
    STAGE_LOCAL,
    STAGE_NO_CANDIDATE,
    AuthorityCandidate,
    AuthorityKind,
    EntityRequest,
    PickerOrdering,
    ReconciliationOutcome,
    ReconciliationSummary,
)


def reconcile_one(
    *,
    request: EntityRequest,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None,
    picker: LLMPicker,
    top_k: int = DEFAULT_TOP_K,
    local_resolver: LocalConceptResolver | None = None,
) -> ReconciliationOutcome:
    """Run the decision logic for ``request`` end-to-end.

    Order of short-circuits:

    1. ``fictional_character`` kind → ``reconciliation-fictional-character``
       outcome with no candidates. Cataloguer marked the entity as
       fictional; no authority carries it.
    2. Tier-0 ``local_resolver`` exact prefLabel match → no HTTP call,
       no LLM.
    3. Tier-1 ``client.query`` (with optional ``fallback_client``) and
       the four-tier decision logic.
    """
    if request.kind == "fictional_character":
        return _fictional_outcome(request)
    if local_resolver is not None:
        hit = local_resolver.resolve(literal=request.literal, kind=request.kind)
        if hit is not None:
            return _local_outcome(request, hit)
    candidates = client.query(request=request, top_k=top_k)
    if not candidates and fallback_client is not None:
        candidates = fallback_client.query(request=request, top_k=top_k)
    return decide_reconciliation(request=request, candidates=candidates, picker=picker)


def _collect_requests(
    graph: Graph, selected_kinds: frozenset[AuthorityKind]
) -> list[EntityRequest]:
    """Walk the canonical graph and yield reconciliation requests filtered by kind."""
    out: list[EntityRequest] = []
    if selected_kinds & _CREATOR_KINDS:
        out.extend(r for r in _iter_creator_requests(graph) if r.kind in selected_kinds)
    if selected_kinds & _SUBJECT_KINDS:
        out.extend(r for r in _iter_subject_requests(graph) if r.kind in selected_kinds)
    return out


@dataclass(frozen=True)
class _CachePending:
    """Phase-2 → Phase-3 hand-off for cache misses that need write-back.

    Stored per-``idx`` while the picker pool runs. Phase 3 reads this
    after :func:`_emit_provenance` mints the Activity URI; the URI is
    then committed to the cache as ``activity_uuid``.
    """

    cache_key: str
    finto_vocab: str
    finto_sha: str
    decision: PickerDecision


def apply_reconciliation(  # noqa: PLR0912, PLR0915 — three-phase orchestrator (tier-0/1, picker dispatch, mutation + provenance); splitting fragments shared state across phases.
    canonical_path: Path | None = None,
    *,
    output_path: Path | None = None,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None = None,
    picker: LLMPicker | None = None,
    picker_factory: Callable[[], LLMPicker] | None = None,
    provenance_graph: Graph | None = None,
    requests: Iterable[EntityRequest] | None = None,
    graph: Graph | None = None,
    top_k: int = DEFAULT_TOP_K,
    now: datetime | None = None,
    kinds: set[AuthorityKind] | frozenset[AuthorityKind] | None = None,
    local_resolver: LocalConceptResolver | None = None,
    concurrency: int = 1,
    field_timeout_seconds: int = 0,
    watchdog_sidecar_path: Path | None = None,
    phase1_concurrency: int = 1,
    picker_ordering: PickerOrdering = PICKER_ORDERING_PREFIX_CACHE,
    picker_cache: PickerCache | None = None,
    finto_dumps_dir: Path | None = None,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
) -> tuple[ReconciliationSummary, list[ReconciliationOutcome]]:
    """Walk canonical.ttl, reconcile creators + subjects, and write the graph back.

    ``graph`` is an injection point for tests so the same orchestrator
    can run against an in-memory graph without serialising to disk.
    Production callers leave it ``None``; the orchestrator parses
    ``canonical_path``, mutates the graph, and serialises it back.

    ``kinds`` filters which reconciliation paths to walk. ``None`` means
    "all kinds" (creators + subjects + genre/forms). Pass
    ``{"person", "corporate_body"}`` to limit to creators, or
    ``{"subject", "genre_form", "music_form"}`` to limit to the subject
    side. Explicit ``requests=`` overrides this filter — the caller is
    assumed to have done the filtering already.

    P-10 Phase A: tier-2 picker calls are dispatched through a
    ``ThreadPoolExecutor(max_workers=concurrency)``; tier-0, tier-1,
    tier-3 (no-candidate) stay single-threaded. ``concurrency == 1``
    keeps the pre-Phase-A sequential behaviour for rollback /
    deterministic tests. Each picker call is wrapped in a
    ``field_timeout_seconds``-second wall budget; on exceed, the field
    falls through to tier-3 fallback (highest-lexical + needs-review)
    with the provenance Activity stamped
    ``bffi-prov:stage = "watchdog-aborted"``.

    P-10 Phase A2: Phase 1 (tier-0 SPARQL + Finto/VIAF candidate
    query) is also dispatched through its own pool sized by
    ``phase1_concurrency``. Defaults to ``1`` (sequential) so existing
    callers / tests are byte-stable; production CLI passes the
    ``M9_PHASE1_CONCURRENCY`` setting (default 8) — Phase 1's
    binding constraint is HTTP / SPARQL throughput rather than the
    GPU-bound mlx-lm picker, so it tolerates higher concurrency.

    P-10 Phase E: ``picker_ordering`` controls the dispatch order of
    deferred picker entries. ``"submission"`` (default) preserves the
    walk order ``_collect_requests`` yielded; ``"prefix-cache"`` sorts
    so consecutive ``POST /v1/chat/completions`` calls share the longest
    possible prompt prefix, intended to lift mlx-lm prefix-cache reuse.
    The 2026-05-13 A/B bench showed ``"prefix-cache"`` regressed the
    picker-phase wall by 5 % on the heterogeneous 5 k sample, so the
    default stays on ``"submission"``. Output is byte-stable under both
    modes — the orchestrator re-sorts results by submission ``idx``
    before graph mutation.

    P-10 Phase B: ``picker_cache`` is a :class:`PickerCache` shared
    across worker threads; when set, picker-bound entries first
    consult the cache before dispatching to the LLM. Cache hits skip
    the picker entirely and write a provenance Activity with
    ``prov:wasInfluencedBy <cached-activity>``. Cache misses run the
    picker as before, then write the verdict back so a re-run hits.
    ``finto_dumps_dir`` (defaults to ``settings.finto_dump_dir`` —
    ``<repo>/finto-dumps`` out of the box, overridable via
    ``BFFI_FINTO_DUMP_DIR``) locates the per-vocab dumps whose SHA-256 anchors cache validity —
    a refresh of one ``<vocab>-skos.ttl`` invalidates that vocab's
    cached entries on the next lookup.

    Pass ``picker_factory`` for concurrent runs (one ``LLMPicker`` is
    built per worker thread). Pass ``picker`` for single-threaded
    runs (existing callers / tests). At least one of the two must be
    supplied.
    """
    settings = get_settings()
    canonical_path = canonical_path or (settings.data_dir / "canonical.ttl")
    output_path = output_path or canonical_path
    moment = (now or datetime.now(UTC)).replace(microsecond=0)
    # P-31 Phase C: load the canonical-map sidecar so the M9 target-
    # review row carries member_bib_ids — cataloguers use those to
    # locate the source MARC and estimate the bug's severity (a wrong
    # cluster spanning two famous authors is more impactful than one
    # between two obscure ones).
    canonical_bib_ids = _load_canonical_bib_ids(settings.data_dir / "canonical-map.jsonl")
    # P-12 Phase D: cadence is operator-tunable via BFFI_M9_PROGRESS_CADENCE
    # so short benches can crank it down (e.g. 50) for a livelier dashboard.
    # Default 200 matches the pre-P-12 module-level constant.
    progress_cadence = settings.m9_progress_cadence
    selected_kinds: frozenset[AuthorityKind] = (
        ALL_AUTHORITY_KINDS if kinds is None else frozenset(kinds)
    )

    if picker is None and picker_factory is None:
        raise ValueError("apply_reconciliation requires picker or picker_factory")
    if concurrency >= _MIN_CONCURRENCY_FOR_FACTORY and picker_factory is None:
        raise ValueError(
            "apply_reconciliation requires picker_factory when concurrency >= 2 "
            "(each worker thread builds its own LLMPicker)"
        )
    # Single picker for the c=1 path. ``picker`` takes precedence if both are
    # supplied so tests that inject a ``StubPicker`` continue to work.
    seq_picker: LLMPicker | None = picker
    if seq_picker is None and picker_factory is not None:
        seq_picker = picker_factory()

    own_graph = graph is None
    target_graph: Graph
    if own_graph:
        target_graph = Graph()
        target_graph.parse(str(canonical_path), format="turtle")
    else:
        assert graph is not None  # narrow for mypy
        target_graph = graph

    request_list: list[EntityRequest] = (
        list(requests) if requests is not None else _collect_requests(target_graph, selected_kinds)
    )

    summary = ReconciliationSummary(total=len(request_list))

    # P-11 Phase A: stage-level start event so the status CLI / dashboard
    # see M9 begin. Per-phase progress events fire from the helpers below.
    emit_if_active(
        stage="m9",
        event="start",
        counters={"total": len(request_list)},
        extra={
            "concurrency": concurrency,
            "phase1_concurrency": phase1_concurrency,
            "field_timeout_seconds": field_timeout_seconds,
            "picker_ordering": picker_ordering,
        },
    )
    # P-11 Phase C: probe Fuseki / mlx-lm / Finto at entry; surfaces a
    # red panel on the dashboard immediately if any are unreachable.
    _m9_probe_dependencies(local_resolver)

    # --- Phase 1: walk requests through tier-0 + candidate-query ----------
    # Each request resolves either to a final outcome (fictional / tier-0 /
    # lexical / no-candidate) or to a deferred ``(request, sorted_candidates)``
    # picker entry for Phase 2. P-10 Phase A2 dispatches the walk through a
    # ``ThreadPoolExecutor`` sized by ``phase1_concurrency`` — Phase 1's
    # cost is dominated by HTTP / SPARQL throughput, not GPU, so it scales
    # independently of the picker concurrency.
    pre_outcomes: dict[int, ReconciliationOutcome] = {}
    deferred: list[tuple[int, EntityRequest, list[AuthorityCandidate]]] = []
    started_at: dict[int, datetime] = {}

    emit_if_active(
        stage="m9",
        event="phase_boundary",
        phase="phase1",
        counters={"total": len(request_list)},
    )

    phase1_results: list[_Phase1Result]
    if phase1_concurrency <= 1:
        phase1_results = _phase1_seq(
            request_list,
            client=client,
            fallback_client=fallback_client,
            top_k=top_k,
            local_resolver=local_resolver,
        )
    else:
        phase1_results = _phase1_pool(
            request_list,
            client=client,
            fallback_client=fallback_client,
            top_k=top_k,
            local_resolver=local_resolver,
            phase1_concurrency=phase1_concurrency,
        )

    # Phase 1 result collation + per-cadence progress emission. We tally
    # per-tier outcomes as we go so the dashboard's M9 outcome bargauge
    # populates live during Phase 1 — split out by tier (local /
    # lexical / no_candidate / fictional) so the exporter can mirror
    # the keys into ``bffi_stage_outcomes_total`` via the
    # ``_PROGRESS_OUTCOME_KEYS`` bridge.
    phase1_local = 0
    phase1_deferred = 0
    tier_local = 0
    tier_lexical = 0
    tier_no_candidate = 0
    tier_fictional = 0
    for i, result in enumerate(phase1_results):
        started_at[result.idx] = result.started_at
        if result.outcome is not None:
            pre_outcomes[result.idx] = result.outcome
            phase1_local += 1
            if result.outcome.stage == STAGE_LOCAL:
                tier_local += 1
            elif result.outcome.stage == STAGE_LEXICAL:
                tier_lexical += 1
            elif result.outcome.stage == STAGE_NO_CANDIDATE:
                tier_no_candidate += 1
            elif result.outcome.stage == STAGE_FICTIONAL:
                tier_fictional += 1
        else:
            assert result.sorted_candidates is not None  # invariant from _Phase1Result
            deferred.append((result.idx, result.request, result.sorted_candidates))
            phase1_deferred += 1
        if progress_cadence > 0 and (i + 1) % progress_cadence == 0:
            emit_if_active(
                stage="m9",
                event="progress",
                phase="phase1",
                counters={"processed": i + 1, "total": len(request_list)},
                extra={
                    "resolved": phase1_local,
                    "deferred_to_picker": phase1_deferred,
                    "local": tier_local,
                    "lexical": tier_lexical,
                    "no_candidate": tier_no_candidate,
                    "fictional": tier_fictional,
                },
            )
        # P-11 Phase C: re-probe mid-stage so the dashboard catches a
        # late-run dependency outage (e.g. Fuseki OOM at hour 4 of an
        # overnight run). Cheap — one probe per 1000 entities.
        if (i + 1) % _M9_HEALTH_PROBE_CADENCE == 0:
            _m9_probe_dependencies(local_resolver)

    # End-of-phase flush: emit one final progress event when the walk
    # didn't land on a cadence boundary so the dashboard reads 100 %
    # of phase 1 instead of plateauing at the last cadence multiple.
    if (
        progress_cadence > 0
        and len(phase1_results) > 0
        and len(phase1_results) % progress_cadence != 0
    ):
        emit_if_active(
            stage="m9",
            event="progress",
            phase="phase1",
            counters={
                "processed": len(phase1_results),
                "total": len(request_list),
            },
            extra={
                "resolved": phase1_local,
                "deferred_to_picker": phase1_deferred,
                "local": tier_local,
                "lexical": tier_lexical,
                "no_candidate": tier_no_candidate,
                "fictional": tier_fictional,
            },
        )

    # --- Phase 1.5: consult the picker cache for deferred entries ---------
    # P-10 Phase B: single-threaded loop *before* the pool dispatch so that
    # N worker threads cannot race on the same uncached key. Cache hits
    # short-circuit Phase 2 entirely with the cached PickerDecision +
    # the original Activity URI (later wired through wasInfluencedBy).
    # Cache misses stay in ``deferred_misses`` and feed Phase 2; their
    # write-back metadata is stashed in ``cache_pending`` for Phase 3.
    model_name_for_cache = (
        getattr(seq_picker, "model_name", None) if seq_picker is not None else None
    ) or "unknown"
    prompt_hash_value = picker_prompt_hash() if picker_cache is not None else ""
    finto_shas: dict[str, str] = {}
    if picker_cache is not None:
        dumps_dir = finto_dumps_dir if finto_dumps_dir is not None else settings.finto_dump_dir
        finto_shas = compute_finto_shas(dumps_dir)
    cache_lookup_keys: dict[int, tuple[str, str, str]] = {}
    deferred_misses: list[tuple[int, EntityRequest, list[AuthorityCandidate]]] = []
    cache_hits = 0
    for idx, request, sorted_candidates in deferred:
        key_info: tuple[str, str, str] | None = None
        if picker_cache is not None:
            key_info = compute_picker_cache_key(
                request=request,
                candidates=sorted_candidates,
                prompt_hash_value=prompt_hash_value,
                model_name=model_name_for_cache,
                finto_shas=finto_shas,
            )
        hit: CacheHit | None = None
        if key_info is not None and picker_cache is not None:
            hit = picker_cache.get(key_info[0])
        if hit is not None:
            outcome = _decide_with_pick(
                request=request,
                sorted_candidates=sorted_candidates,
                pick=hit.decision,
                lexical_fallback_floor=lexical_fallback_floor,
                lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
                disable_fallback=disable_fallback,
            )
            pre_outcomes[idx] = ReconciliationOutcome(
                request=outcome.request,
                stage=outcome.stage,
                chosen_uri=outcome.chosen_uri,
                confidence=outcome.confidence,
                rationale=outcome.rationale,
                candidates=outcome.candidates,
                needs_review=outcome.needs_review,
                was_watchdog_aborted=outcome.was_watchdog_aborted,
                cached_activity_uuid=hit.activity_uuid,
            )
            cache_hits += 1
        else:
            deferred_misses.append((idx, request, sorted_candidates))
            if key_info is not None:
                cache_lookup_keys[idx] = key_info

    emit_if_active(
        stage="m9",
        event="progress",
        phase="cache-lookup",
        counters={
            "deferred_to_picker": len(deferred),
            "cache_hits": cache_hits,
            "cache_misses": len(deferred_misses),
        },
    )

    # --- Phase 2: dispatch deferred picker calls --------------------------
    # P-10 Phase E: reorder the queue so consecutive picker calls share
    # the longest possible prompt prefix. Output Turtle stays byte-stable
    # because the result-merge below sorts by submission ``idx``.
    from bffi_pipeline.stages.m9.pool import _order_deferred_picker_queue

    deferred_misses = _order_deferred_picker_queue(deferred_misses, ordering=picker_ordering)
    emit_if_active(
        stage="m9",
        event="phase_boundary",
        phase="phase2",
        counters={
            # ``total`` echoes ``deferred_to_picker`` so the exporter sets
            # ``bffi_stage_entities_total{phase="phase2"}`` at phase entry.
            # Without this the dashboard's M9 phase-2 bar stays empty until
            # the first progress event lands at ``processed=cadence``
            # (~2-3 min into Phase 2). Phase 1 / Phase 3 already follow
            # this pattern.
            "total": len(deferred_misses),
            "deferred_to_picker": len(deferred_misses),
            "cache_hits": cache_hits,
        },
        extra={"picker_ordering": picker_ordering},
    )
    picker_results: list[tuple[int, ReconciliationOutcome]] = []
    if deferred_misses:
        # Derive a model_name string for watchdog events. Falls back to "
        # unknown" if the picker is a stub or doesn't expose model_name.
        probe_picker = (
            seq_picker
            if seq_picker is not None
            else (picker_factory() if picker_factory is not None else None)
        )
        model_name = getattr(probe_picker, "model_name", None) or "unknown"

        if concurrency <= 1:
            assert seq_picker is not None  # narrow for mypy; validated above
            picker_results = _picker_phase_seq(
                deferred_misses,
                picker=seq_picker,
                field_timeout_seconds=field_timeout_seconds,
                model_name=model_name,
                watchdog_sidecar_path=watchdog_sidecar_path,
                progress_cadence=progress_cadence,
                cache_hits=cache_hits,
                lexical_fallback_floor=lexical_fallback_floor,
                lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
                disable_fallback=disable_fallback,
            )
        else:
            assert picker_factory is not None  # narrow; validated above
            picker_results = _picker_phase_pool(
                deferred_misses,
                picker_factory=picker_factory,
                concurrency=concurrency,
                field_timeout_seconds=field_timeout_seconds,
                model_name=model_name,
                watchdog_sidecar_path=watchdog_sidecar_path,
                progress_cadence=progress_cadence,
                cache_hits=cache_hits,
                lexical_fallback_floor=lexical_fallback_floor,
                lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
                disable_fallback=disable_fallback,
            )

    # P-10 Phase B: stash write-back data per idx — Phase 3 reads it
    # after _emit_provenance returns the freshly-minted Activity URI.
    # P-10 Phase B.1: cache *every* picker decision, not only STAGE_LLM
    # successes. Storing the raw ``PickerDecision`` lets the warm-run
    # lookup replay ``_decide_with_pick`` byte-stably — including for
    # low-confidence picks that map to STAGE_FALLBACK. Without this,
    # the model's per-call non-determinism near the 0.80 LLM-confidence
    # threshold flips cold→warm tier classifications (see
    # ``scripts/p10-phase-b-cold-warm-audit.py``). Watchdog-aborted
    # outcomes still aren't cached: those reflect a budget timeout, not
    # a real picker verdict, and a re-run should re-attempt.
    cache_pending: dict[int, _CachePending] = {}
    for idx, outcome in picker_results:
        pre_outcomes[idx] = outcome
        if (
            picker_cache is not None
            and idx in cache_lookup_keys
            and not outcome.was_watchdog_aborted
            and outcome.picker_decision is not None
        ):
            cache_key, vocab, finto_sha = cache_lookup_keys[idx]
            cache_pending[idx] = _CachePending(
                cache_key=cache_key,
                finto_vocab=vocab,
                finto_sha=finto_sha,
                decision=outcome.picker_decision,
            )

    # --- Phase 3: apply graph mutations + provenance in request order -----
    emit_if_active(
        stage="m9",
        event="phase_boundary",
        phase="phase3",
        counters={"total": len(request_list)},
    )
    # Deterministic by construction: sorted by the original request index.
    outcomes: list[ReconciliationOutcome] = []
    for idx in range(len(request_list)):
        outcome = pre_outcomes[idx]
        outcomes.append(outcome)

        if outcome.was_watchdog_aborted:
            summary.watchdog_aborted += 1
        if outcome.stage == STAGE_LOCAL:
            summary.local += 1
        elif outcome.stage == STAGE_LEXICAL:
            summary.lexical += 1
        elif outcome.stage == STAGE_LLM:
            summary.llm_pick += 1
        elif outcome.stage == STAGE_FALLBACK:
            summary.fallback += 1
        elif outcome.stage == STAGE_FICTIONAL:
            summary.fictional += 1
        else:
            summary.no_candidate += 1

        # P-31 Phase C: pipeline-transformation review surfaces. Three
        # M9 outcome shapes get a target-review row so the cataloguer
        # can verify whether the pipeline got the reconciliation right
        # (and feed pipeline-incorrect rows back into prompt iteration,
        # gold-set growth, FP veto plans). member_bib_ids resolves
        # canonical Work URI → source Helmet bib_ids via the M8
        # canonical-map sidecar; cataloguers use those to inspect the
        # source MARC and estimate the bug's severity.
        target_bib_ids = canonical_bib_ids.get(outcome.request.work_uri, [])
        if outcome.stage == STAGE_FALLBACK:
            append_target_row(
                member_bib_ids=target_bib_ids,
                reason="m9-fallback",
                confidence=outcome.confidence,
                details=_format_m9_details(outcome),
                dedup_key=outcome.request.work_uri,
            )
        elif outcome.stage == STAGE_FICTIONAL:
            append_target_row(
                member_bib_ids=target_bib_ids,
                reason="fictional-character",
                confidence=None,
                details=_format_m9_details(outcome),
                dedup_key=outcome.request.work_uri,
            )
        elif outcome.chosen_uri is None and outcome.stage != STAGE_FICTIONAL:
            # no-candidate path (everything not handled above with a
            # bound chosen_uri AND not the fictional short-circuit)
            append_target_row(
                member_bib_ids=target_bib_ids,
                reason="m9-no-candidate",
                confidence=None,
                details=_format_m9_details(outcome),
                dedup_key=outcome.request.work_uri,
            )

        if outcome.chosen_uri is not None:
            _apply_canonical_link(target_graph, outcome.request, outcome.chosen_uri)
            _bump_admin_metadata(
                target_graph,
                outcome.request.work_uri,
                chosen_uri=outcome.chosen_uri,
                needs_review=outcome.needs_review,
                now=moment,
            )
        activity_uri = _emit_provenance(
            provenance_graph,
            outcome=outcome,
            started_at=started_at[idx],
            ended_at=datetime.now(UTC),
        )
        # P-10 Phase B: commit fresh picker verdicts to the cache *after*
        # the Activity URI is minted, so the cache row's ``activity_uuid``
        # matches the URI a future cache hit will hand to wasInfluencedBy.
        # Cache hits don't reappear here (cache_pending only has misses
        # that went through the picker).
        if picker_cache is not None and activity_uri is not None and idx in cache_pending:
            pending = cache_pending[idx]
            picker_cache.set(
                pending.cache_key,
                decision=pending.decision,
                finto_vocab=pending.finto_vocab,
                finto_sha=pending.finto_sha,
                prompt_hash_value=prompt_hash_value,
                model_name=model_name_for_cache,
                activity_uuid=str(activity_uri),
            )

    if own_graph:
        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        target_graph.serialize(destination=str(tmp), format="turtle")
        tmp.replace(output_path)

    emit_if_active(
        stage="m9",
        event="end",
        counters={
            "total": summary.total,
            "local": summary.local,
            "lexical": summary.lexical,
            "llm_pick": summary.llm_pick,
            "fallback": summary.fallback,
            "no_candidate": summary.no_candidate,
            "fictional": summary.fictional,
            "watchdog_aborted": summary.watchdog_aborted,
        },
    )
    return summary, outcomes
