"""M6 batch driver — :func:`judge_batch` over M5's candidate JSONL.

Reads ``embed-candidates.jsonl``, splits the auto-merge band off as
deterministic ``same_work`` decisions (no LLM call), and runs every
escalate-band pair through :func:`cascade.cascade_judge`. Resumable
via the ``.checkpoint`` sibling :mod:`sidecars` mirrors every
:data:`CHECKPOINT_INTERVAL` completed pairs, configurable concurrency
(thread pool) for mlx-lm's concurrent mode, optional provenance
emission to a ``ProvenanceWriter``.

P-38 Phase D: extracted from m6/runner.py. No logic change.
"""

from __future__ import annotations

import json as _json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.observability.probes import emit_health_probes, probe_mlx_lm
from bffi_pipeline.provenance.writer import ProvenanceWriter
from bffi_pipeline.stages.m6.cache import JudgeCache, default_cache_path
from bffi_pipeline.stages.m6.cascade import ChainLike, cascade_judge
from bffi_pipeline.stages.m6.graph_extract import _load_work_records_from_corpus
from bffi_pipeline.stages.m6.outcome import (
    JudgeOutcome,
    _uncertain_decision,
    synthesize_auto_merge_outcome,
)
from bffi_pipeline.stages.m6.prompts import prompt_hash
from bffi_pipeline.stages.m6.sidecars import (
    JudgeCheckpoint,
    _checkpoint_path_for,
    _load_auto_merge_candidates,
    _load_candidate_jsonl,
    _load_checkpoint,
    _serialise_decision,
    _write_checkpoint,
)
from bffi_pipeline.stages.m6.validation import WorkRecord

#: How many completed pairs the batch driver runs between checkpoint
#: flushes + progress emits. Tests monkeypatch this down to surface
#: checkpoint behaviour on smaller fixtures.
CHECKPOINT_INTERVAL: Final[int] = 100

#: Default thread-pool size for the per-pair cascade. mlx-lm's
#: concurrent mode is the production target; the default keeps the
#: dev / Ollama path serial.
DEFAULT_CONCURRENCY: Final[int] = 1

CascadeFn = Callable[..., JudgeOutcome]


@dataclass(frozen=True)
class JudgeBatchProgress:
    """Snapshot of an in-flight ``judge_batch`` run."""

    completed: int
    total: int
    cache_hits: int
    fresh_calls: int
    cascade_used: int
    elapsed_seconds: float
    eta_seconds: float | None

    @property
    def avg_seconds_per_pair(self) -> float | None:
        """Average time per completed pair, or ``None`` before the first one finishes."""
        return self.elapsed_seconds / self.completed if self.completed else None

    def render(self) -> str:
        """Format this progress sample as a one-line CLI status string."""
        avg = self.avg_seconds_per_pair
        if avg is None:
            return f"{self.completed:,} / {self.total:,} pairs"
        if self.eta_seconds is None:
            eta = "ETA --"
        else:
            hours, remainder = divmod(int(self.eta_seconds), 3600)
            minutes = remainder // 60
            eta = f"ETA {hours}h {minutes:02d}m"
        return (
            f"{self.completed:,} / {self.total:,} pairs · "
            f"{avg:.1f}s/pair · {eta} · "
            f"{self.cache_hits:,} cache hits · "
            f"{self.fresh_calls:,} fresh calls"
        )


@dataclass
class JudgeBatchResult:
    """End-of-run summary for ``judge_batch``."""

    total_pairs: int
    completed: int
    cache_hits: int
    fresh_calls: int
    cascade_used: int
    auto_merged: int = 0
    decision_counts: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    output_path: str = ""
    checkpoint_path: str = ""

    def render(self) -> str:
        """Format the batch result as paste-ready text for the judge CLI."""
        lines = [
            "M6 judge batch complete",
            f"  total candidates: {self.total_pairs:,}",
            f"  completed:        {self.completed:,}",
            f"  auto-merged:      {self.auto_merged:,}",
            f"  cache hits:       {self.cache_hits:,}",
            f"  fresh calls:      {self.fresh_calls:,}",
            f"  cascade used:     {self.cascade_used:,}",
            f"  elapsed:          {self.elapsed_seconds / 60:.1f} min",
            f"  output JSONL:     {self.output_path}",
        ]
        if self.decision_counts:
            lines.append("  decision counts:")
            for label in ("same_work", "different_work", "uncertain"):
                lines.append(f"    {label:<16s} {self.decision_counts.get(label, 0):>8,}")
        return "\n".join(lines)


def _emit_provenance(
    writer: ProvenanceWriter,
    row: dict[str, Any],
    outcome: JudgeOutcome,
    seen_models: set[str],
) -> None:
    """Log every cascade step in ``outcome`` as a separate WorkMergeDecision.

    Per spec § 8 / docs/local-inference.md the cascade's primary and
    second-opinion calls each become their own ``prov:Activity`` with
    distinct ``bffi-prov:stage`` tags so a SPARQL query can ask
    "which decisions did the 32 B alone make?" vs "which got a 72 B
    second opinion?". Software-agent blocks are emitted once per
    model_id (caller tracks the seen set across the batch run).
    """
    inputs = (row["work_a"], row["work_b"])
    similarity = float(row.get("similarity", 0.0))
    for step in outcome.steps:
        if step.model_name not in seen_models:
            writer.add_software_agent(model_id=step.model_name)
            seen_models.add(step.model_name)
        writer.add_merge_decision(
            inputs=inputs,
            decision=step.decision.decision,
            confidence=step.decision.confidence,
            embedding_similarity=similarity,
            rationale=step.decision.rationale,
            matching_fields=step.decision.matching_fields,
            diverging_fields=step.decision.diverging_fields,
            prompt_hash=prompt_hash(),
            raw_response=step.decision.model_dump_json(),
            model_id=step.model_name,
            stage=step.stage,
            cache_hit=step.cache_hit,
        )


def judge_batch(  # noqa: PLR0912, PLR0915 — orchestrates resume + per-pair retry + checkpoint write; splitting fragments state.
    candidates_path: Path | None = None,
    output_path: Path | None = None,
    *,
    bffi_corpus_dir: Path | None = None,
    work_records: dict[str, WorkRecord] | None = None,
    resume: bool = True,
    primary_model: str | None = None,
    fallback_model: str | None = None,
    primary_chain: ChainLike | None = None,
    fallback_chain: ChainLike | None = None,
    cache: JudgeCache | None = None,
    cascade: CascadeFn | None = None,
    progress_callback: Callable[[JudgeBatchProgress], None] | None = None,
    decision_callback: Callable[[dict[str, Any], JudgeOutcome], None] | None = None,
    provenance_writer: ProvenanceWriter | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    sleep: Callable[[float], None] = time.sleep,
    full_rationale: bool = True,
    watchdog_sidecar_path: Path | None = None,
) -> JudgeBatchResult:
    """Run the cascade over every escalate-band pair from M5.

    Inputs / outputs default under ``BFFI_DATA_DIR``. ``resume=True``
    (the default) skips past ``last_completed_idx`` recorded in the
    checkpoint; ``resume=False`` blows away both the output JSONL and
    its checkpoint sibling before starting.

    ``decision_callback`` is a generic per-pair hook used by tests and
    custom integrations. ``provenance_writer`` is the spec § 8
    integration: when supplied, every cascade step is logged as a
    discrete ``bffi-prov:WorkMergeDecision`` Activity (so a primary +
    second-opinion pair produces two Activities), and each distinct
    model gets a ``prov:SoftwareAgent`` block emitted once.
    """
    settings = get_settings()
    candidates_path = candidates_path or (settings.data_dir / "embed-candidates.jsonl")
    output_path = output_path or (settings.data_dir / "judge-decisions.jsonl")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = _checkpoint_path_for(output_path)
    if watchdog_sidecar_path is None:
        # Default sidecar lives alongside the other M6 artefacts so a
        # ``BFFI_DATA_DIR`` swap rotates it automatically.
        watchdog_sidecar_path = settings.data_dir / "watchdog-events.jsonl"

    cascade_fn: CascadeFn = cascade if cascade is not None else cascade_judge

    candidates = _load_candidate_jsonl(candidates_path)
    total = len(candidates)
    emit_if_active(
        stage="m6",
        event="start",
        counters={"total": total},
        extra={"concurrency": concurrency, "resume": resume},
    )
    # P-11 Phase C: probe both mlx-lm cascade ports at entry. The
    # primary at :8001 and the fallback at :8002 are independent
    # processes; either can be down without the other.
    #
    # P-12 Phase B: when the fallback URL equals the primary URL
    # (degenerate cascade — the same process probed twice — typical
    # on dev hosts where the 32B fallback doesn't fit, see CLAUDE.md
    # § "Dev machine constraints"), pass an empty URL into
    # probe_mlx_lm so it short-circuits to status="not_configured".
    # The dashboard then greys the cell instead of colouring it red.
    primary_url = settings.llm_base_url_primary or settings.llm_base_url
    raw_fallback = settings.llm_base_url_fallback or settings.llm_base_url
    fallback_url = raw_fallback if raw_fallback != primary_url else ""
    emit_health_probes(
        "m6",
        {
            "mlx-lm-primary": probe_mlx_lm(primary_url, dep="mlx-lm-primary"),
            "mlx-lm-fallback": probe_mlx_lm(fallback_url, dep="mlx-lm-fallback"),
        },
    )

    if work_records is None:
        if bffi_corpus_dir is None:
            bffi_corpus_dir = settings.data_dir
        work_records = _load_work_records_from_corpus(bffi_corpus_dir)

    own_cache = cache is None
    if own_cache:
        cache = JudgeCache(default_cache_path())

    seen_models: set[str] = set()

    started = time.monotonic()

    start_idx = 0
    cache_hits = 0
    fresh_calls = 0
    cascade_used = 0
    decision_counts: dict[str, int] = {}

    if not resume:
        if output_path.exists():
            output_path.unlink()
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        start_time_iso = datetime.now(UTC).isoformat()
        write_mode = "w"
    else:
        existing = _load_checkpoint(checkpoint_path)
        if existing is not None and existing.total_pairs == total:
            start_idx = existing.last_completed_idx + 1
            cache_hits = existing.cache_hits
            fresh_calls = existing.fresh_calls
            cascade_used = existing.cascade_used
            start_time_iso = existing.start_time
            write_mode = "a"
        else:
            start_time_iso = datetime.now(UTC).isoformat()
            if output_path.exists():
                output_path.unlink()
            write_mode = "w"

    if concurrency < 1:
        raise ValueError(f"concurrency must be ≥ 1, got {concurrency!r}")

    def _judge_one(idx: int) -> tuple[dict[str, Any], JudgeOutcome]:
        row = candidates[idx]
        a = work_records.get(row["work_a"]) if work_records else None
        b = work_records.get(row["work_b"]) if work_records else None
        if a is None or b is None:
            return row, JudgeOutcome(
                final=_uncertain_decision(
                    f"missing WorkRecord for {row['work_a']} or {row['work_b']}; "
                    "M2 + M3 must run before M6."
                ),
                steps=[],
            )
        return row, cascade_fn(
            a,
            b,
            row["similarity"],
            primary_model=primary_model,
            fallback_model=fallback_model,
            primary_chain=primary_chain,
            fallback_chain=fallback_chain,
            cache=cache,
            sleep=sleep,
            full_rationale=full_rationale,
            watchdog_sidecar_path=watchdog_sidecar_path,
        )

    def _record_outcome(
        idx: int,
        row: dict[str, Any],
        outcome: JudgeOutcome,
        fh: Any,
    ) -> None:
        nonlocal cache_hits, fresh_calls, cascade_used
        fh.write(_json.dumps(_serialise_decision(row, outcome), ensure_ascii=False) + "\n")
        fh.flush()

        if decision_callback is not None:
            decision_callback(row, outcome)
        if provenance_writer is not None:
            _emit_provenance(provenance_writer, row, outcome, seen_models)

        if outcome.used_cascade:
            cascade_used += 1
        for step in outcome.steps:
            if step.cache_hit:
                cache_hits += 1
            else:
                fresh_calls += 1
        decision_counts[outcome.final.decision] = decision_counts.get(outcome.final.decision, 0) + 1

        completed = idx + 1
        if completed % CHECKPOINT_INTERVAL == 0 or completed == total:
            elapsed = time.monotonic() - started
            avg = elapsed / max(1, completed - start_idx)
            remaining = max(0, total - completed)
            eta_seconds = remaining * avg if avg > 0 else None
            _write_checkpoint(
                checkpoint_path,
                JudgeCheckpoint(
                    start_time=start_time_iso,
                    last_completed_idx=idx,
                    total_pairs=total,
                    cache_hits=cache_hits,
                    fresh_calls=fresh_calls,
                    cascade_used=cascade_used,
                ),
            )
            # P-12 Phase D + follow-up: emit progress at the checkpoint
            # cadence so the exporter can derive throughput / ETA + the
            # dashboard's M6 outcome bargauge populates live (via the
            # _PROGRESS_OUTCOME_KEYS bridge in metrics_exporter.py).
            emit_if_active(
                stage="m6",
                event="progress",
                counters={"processed": completed, "total": total},
                extra={
                    "cache_hits": cache_hits,
                    "fresh_calls": fresh_calls,
                    "cascade_used": cascade_used,
                    "auto_merged": auto_merge_written,
                },
            )
            if progress_callback is not None:
                progress_callback(
                    JudgeBatchProgress(
                        completed=completed,
                        total=total,
                        cache_hits=cache_hits,
                        fresh_calls=fresh_calls,
                        cascade_used=cascade_used,
                        elapsed_seconds=elapsed,
                        eta_seconds=eta_seconds,
                    )
                )

    auto_merge_rows = _load_auto_merge_candidates(candidates_path)
    auto_merge_written = 0

    try:
        with output_path.open(write_mode, encoding="utf-8") as fh:
            # Auto-merge band: spec § 6 says sim ≥ 0.90 merges without
            # an LLM call. Write these synthetic same_work decisions
            # once per fresh run (write_mode == "w"); resumed runs
            # ("a" mode) leave them in place from the prior invocation.
            if write_mode == "w":
                for am_row in auto_merge_rows:
                    am_outcome = synthesize_auto_merge_outcome(am_row)
                    fh.write(
                        _json.dumps(_serialise_decision(am_row, am_outcome), ensure_ascii=False)
                        + "\n"
                    )
                    if decision_callback is not None:
                        decision_callback(am_row, am_outcome)
                    if provenance_writer is not None:
                        _emit_provenance(provenance_writer, am_row, am_outcome, seen_models)
                    decision_counts[am_outcome.final.decision] = (
                        decision_counts.get(am_outcome.final.decision, 0) + 1
                    )
                    auto_merge_written += 1
                fh.flush()

            if concurrency == 1:
                for idx in range(start_idx, total):
                    row, outcome = _judge_one(idx)
                    _record_outcome(idx, row, outcome, fh)
            else:
                # Submit/drain in fixed-size chunks so output JSONL stays in input
                # order and the checkpoint can rely on `last_completed_idx` being
                # contiguous. Reuses one thread pool across chunks.
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    for chunk_start in range(start_idx, total, concurrency):
                        chunk_end = min(chunk_start + concurrency, total)
                        idxs = list(range(chunk_start, chunk_end))
                        futures = [pool.submit(_judge_one, idx) for idx in idxs]
                        for offset, future in enumerate(futures):
                            row, outcome = future.result()
                            _record_outcome(idxs[offset], row, outcome, fh)
    finally:
        if own_cache and cache is not None:
            cache.close()

    emit_if_active(
        stage="m6",
        event="end",
        counters={
            "total_pairs": total,
            "completed": total,
            "cache_hits": cache_hits,
            "fresh_calls": fresh_calls,
            "cascade_used": cascade_used,
            "auto_merged": auto_merge_written,
        },
        extra={"elapsed_seconds": time.monotonic() - started},
    )
    return JudgeBatchResult(
        total_pairs=total,
        completed=total,
        cache_hits=cache_hits,
        fresh_calls=fresh_calls,
        cascade_used=cascade_used,
        auto_merged=auto_merge_written,
        decision_counts=decision_counts,
        elapsed_seconds=time.monotonic() - started,
        output_path=str(output_path),
        checkpoint_path=str(checkpoint_path),
    )
