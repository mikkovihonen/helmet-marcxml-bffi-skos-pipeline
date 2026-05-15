"""Canonical pipeline runner.

One Python entry-point that chains the standard stage sequence inside a
single process, replacing the deleted ``scripts/run-full-pipeline.sh``.
Python over shell this time so:

- per-stage timing is captured in one clock domain (no bash↔python
  drift),
- the active :class:`StageEventEmitter` is reused across stages so the
  dashboard sees one continuous timeline instead of N separate CLI
  invocations,
- ``skipped`` events flow through the same emitter so the dashboard's
  four-state model (pending / running / done / skipped) lights up
  correctly when the operator passes ``--skip`` or ``--from-stage``,
- failures in a stage's ``run()`` are caught + recorded as
  ``end status=failed`` before the exception re-raises, so the
  dashboard never sees a stage stuck in "running".

M5 (``embed``) is the one exception: it runs in a subprocess so the OS
reclaims BGE-M3 + FAISS memory before M6 boots mlx-lm. The child
inherits ``BFFI_RUN_UUID`` / ``BFFI_DATA_DIR`` (so its events land in
the shared sidecar + manifest) and sees ``BFFI_RUN_AS_CHILD=1`` (so its
``_init_observability`` skips ``write_initial_manifest`` and the
``mark_run_complete`` atexit). A non-zero child exit raises
``CalledProcessError``, which the runner's existing per-stage try/except
turns into an ``emit_failed`` + ``StageOutcome(status="failed")``.

The Prometheus metrics exporter (``bffi-pipeline serve-metrics``) is
**not** spawned by this runner — it's a long-lived background process
managed by ``make observability-up`` / ``make observability-down``
alongside the Docker stack. It tails ``runs/*/stage-events.jsonl``
via the ``--watch-glob`` plumbing so each new run's events flow
through to Prometheus without runner-side subprocess plumbing.

The runner deliberately *does not* re-implement each stage's CLI
argument surface. It dispatches to the existing
``bffi_pipeline.cli.<stage>_command`` callables — those are typer-
decorated but remain ordinary Python functions that accept the
parameter defaults declared by ``typer.Option``. Stage-level knobs
that aren't covered by ``--force-stages`` stay where they are — the
operator can still invoke individual stage CLIs for non-canonical
runs.

The runner imports ``cli.py`` at top-level; the inverse direction
(``cli.py`` importing ``run_pipeline``) lives inside the ``run``
command body so the two don't form a startup circular import.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from bffi_pipeline.cli import (
    bf_to_bffi_command,
    export_command,
    judge_command,
    load_command,
    marc_to_bf_command,
    merge_command,
    reconcile_command,
    skosify_command,
)
from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.observability import emit_failed, emit_plan, emit_skipped

#: Canonical stage order, matching the dashboard's per-stage panels and
#: the existing ``bffi-pipeline plan`` convention. ``m4`` (workkey-stats)
#: is reporting-only and not part of this chain.
CANONICAL_STAGES: Final[tuple[str, ...]] = (
    "m2",
    "m3",
    "m5",
    "m6",
    "m8",
    "m9",
    "skosify",
    "load",
    "export",
)

#: Phase declarations per stage — feeds the ``stage_phases`` extra on the
#: ``pipeline.plan`` event so the metrics exporter can pre-create
#: ``bffi_stage_phase_planned`` gauges. Dashboard panels read these to
#: render 0%-valued pending bars for not-yet-started phases instead of
#: "—" no-data tiles.
#:
#: M9 is the only stage with multiple named phases (Phase 1 = cache lookup
#: + Phase 1 SPARQL; Phase 2 = picker LLM dispatch; Phase 3 = subjects).
#: Every other canonical stage uses the implicit ``"_"`` phase placeholder
#: matching what :func:`bffi_pipeline.metrics_exporter.apply_event` sets
#: on ``start`` events without a phase.
STAGE_PHASES: Final[dict[str, tuple[str, ...]]] = {
    "m2": ("_",),
    "m3": ("_",),
    "m5": ("_",),
    "m6": ("_",),
    "m8": ("_",),
    "m9": ("phase1", "phase2", "phase3"),
    "skosify": ("_",),
    "load": ("_",),
    "export": ("_",),
}


@dataclass(frozen=True)
class StageOutcome:
    """One row in :class:`PipelineRunSummary.outcomes`."""

    stage: str
    status: str  # "completed" | "skipped" | "failed"
    elapsed_seconds: float
    reason: str = ""


@dataclass
class PipelineRunSummary:
    """Aggregate result of a :func:`run_pipeline` invocation."""

    outcomes: list[StageOutcome] = field(default_factory=list)
    total_elapsed_seconds: float = 0.0

    def render(self) -> str:
        lines = ["Pipeline run summary:"]
        for o in self.outcomes:
            marker = {"completed": "✓", "skipped": "·", "failed": "✗"}.get(o.status, "?")
            suffix = f"  ({o.reason})" if o.reason else ""
            lines.append(
                f"  {marker} {o.stage:<8} {o.status:<9} {o.elapsed_seconds:>8.1f}s{suffix}"
            )
        lines.append(f"  total: {self.total_elapsed_seconds:.1f}s")
        return "\n".join(lines)


def _dispatch_m2(*, input_dir: Path, force: bool) -> None:
    marc_to_bf_command(input_dir=input_dir, force=force)


def _dispatch_m3(*, force: bool) -> None:
    bf_to_bffi_command(force=force)


def _dispatch_m5(*, force: bool) -> None:
    # Spawned in its own process so the OS reclaims BGE-M3 + FAISS
    # memory on exit instead of letting it linger across M6 (mlx-lm
    # judge). The parent resolves run_uuid in memory only
    # (Settings._resolve_run_identity doesn't write back to os.environ),
    # so BFFI_RUN_UUID has to be pinned explicitly — otherwise the
    # child generates its own uuid and writes into a fresh, empty run
    # dir. ``data_dir`` then derives correctly from
    # ``runs_root / run_uuid`` in the child (or inherits an explicit
    # BFFI_DATA_DIR override from the parent's env, if one was set).
    # BFFI_RUN_AS_CHILD=1 tells the child's _init_observability to
    # share the parent's manifest (no ``started_at`` clobber, no
    # premature ``status=completed`` atexit) while still emitting m5
    # events into the shared sidecar.
    cmd = [sys.executable, "-m", "bffi_pipeline.cli", "embed"]
    if force:
        cmd.append("--force")
    env = {
        **os.environ,
        "BFFI_RUN_AS_CHILD": "1",
        "BFFI_RUN_UUID": get_settings().run_uuid,
    }
    subprocess.run(cmd, env=env, check=True)


def _dispatch_m6(*, force: bool) -> None:
    # M6 has no ``--force``; ``--restart`` is the closest equivalent
    # (wipe decisions + checkpoint and re-run). The runner only honours
    # ``--force-stages m6`` as a restart request.
    judge_command(restart=force)


def _dispatch_m8() -> None:
    merge_command()


def _dispatch_m9() -> None:
    reconcile_command()


def _dispatch_skosify(*, force: bool) -> None:
    skosify_command(force=force)


def _dispatch_load() -> None:
    load_command()


def _dispatch_export() -> None:
    # Default export = concatenated BFFI TTL + README only. Operators
    # who also want the per-record archive run
    # ``bffi-pipeline export --include-per-record`` (idempotent), since
    # the runner deliberately doesn't expose that knob — it's a niche
    # ~5000-file bundle that doubles archive size.
    export_command()


#: Module-level dispatch table — lets tests monkeypatch a single stage's
#: invocation without monkeypatching the entire CLI module.
_DISPATCHERS: Final[dict[str, Callable[..., None]]] = {
    "m2": _dispatch_m2,
    "m3": _dispatch_m3,
    "m5": _dispatch_m5,
    "m6": _dispatch_m6,
    "m8": _dispatch_m8,
    "m9": _dispatch_m9,
    "skosify": _dispatch_skosify,
    "load": _dispatch_load,
    "export": _dispatch_export,
}


def _call_dispatcher(stage: str, *, input_dir: Path | None, force: bool) -> None:
    """Resolve + call the dispatch function for ``stage``.

    Per-stage kwargs are filtered to what each dispatcher accepts so the
    operator-facing ``--force-stages m9`` doesn't error out when M9 has
    no ``force`` knob.
    """
    dispatcher = _DISPATCHERS.get(stage)
    if dispatcher is None:
        raise ValueError(f"Unknown stage {stage!r}; expected one of {CANONICAL_STAGES}.")
    if stage == "m2":
        if input_dir is None:
            raise ValueError("m2 dispatch requires input_dir (MARCXML source directory).")
        dispatcher(input_dir=input_dir, force=force)
    elif stage in {"m3", "m5", "m6", "skosify"}:
        dispatcher(force=force)
    else:  # m8, m9, load, export
        dispatcher()


def run_pipeline(
    *,
    input_dir: Path | None = None,
    stages: tuple[str, ...] = CANONICAL_STAGES,
    skip_stages: frozenset[str] = frozenset(),
    force_stages: frozenset[str] = frozenset(),
    description: str = "",
    from_stage: str | None = None,
) -> PipelineRunSummary:
    """Run the canonical pipeline chain in one Python process.

    Emits one ``pipeline.plan`` event listing every stage in ``stages``
    so the dashboard renders pending tiles for stages that haven't
    started yet. Stages in ``skip_stages`` emit a ``skipped`` event +
    are recorded as ``skipped`` in the summary. ``from_stage`` is a
    convenience: every stage before it (in ``stages`` order) is added
    to ``skip_stages`` with reason ``resume-from-stage``.

    The Prometheus metrics exporter (``bffi-pipeline serve-metrics``)
    runs as a long-lived background process managed by
    ``make observability-up`` / ``make observability-down``, not by
    this function. It tails ``runs/*/stage-events.jsonl`` so a fresh
    run's events surface automatically without any runner-side
    lifecycle plumbing.

    On the first failed stage the function records the failure in the
    summary, emits no extra events (the underlying stage's ``end`` event
    is its own responsibility), and re-raises. Subsequent stages are
    never dispatched — they stay "pending" in the dashboard, which is
    the correct interpretation ("the pipeline didn't decide to skip
    them; it never got the chance to run them").
    """
    if from_stage is not None:
        if from_stage not in stages:
            raise ValueError(f"--from-stage {from_stage!r} not in stages list {stages}.")
        cutoff = stages.index(from_stage)
        skip_stages = skip_stages | frozenset(stages[:cutoff])

    if stages and stages[0] == "m2" and input_dir is None and "m2" not in skip_stages:
        raise ValueError(
            "m2 is the first planned stage but no input_dir was given. "
            "Either pass --input-dir or skip m2 (--skip m2 / --from-stage m3)."
        )

    summary = PipelineRunSummary()
    overall_start = time.monotonic()

    try:
        emit_plan(
            list(stages),
            description=description,
            stage_phases={
                stage: list(STAGE_PHASES[stage]) for stage in stages if stage in STAGE_PHASES
            },
        )
        _run_stages(
            stages=stages,
            skip_stages=skip_stages,
            force_stages=force_stages,
            input_dir=input_dir,
            from_stage=from_stage,
            summary=summary,
            overall_start=overall_start,
        )
    finally:
        summary.total_elapsed_seconds = time.monotonic() - overall_start

    return summary


def _run_stages(
    *,
    stages: tuple[str, ...],
    skip_stages: frozenset[str],
    force_stages: frozenset[str],
    input_dir: Path | None,
    from_stage: str | None,
    summary: PipelineRunSummary,
    overall_start: float,
) -> None:
    """Inner loop extracted so :func:`run_pipeline` can wrap it in the
    exporter context without indenting the entire body twice."""
    for stage in stages:
        if stage in skip_stages:
            reason = (
                "resume-from-stage"
                if from_stage is not None and stages.index(stage) < stages.index(from_stage)
                else "operator-skipped"
            )
            emit_skipped(stage, reason=reason)
            summary.outcomes.append(
                StageOutcome(
                    stage=stage,
                    status="skipped",
                    elapsed_seconds=0.0,
                    reason=reason,
                )
            )
            continue

        stage_start = time.monotonic()
        try:
            _call_dispatcher(stage, input_dir=input_dir, force=(stage in force_stages))
        except BaseException as exc:
            elapsed = time.monotonic() - stage_start
            error_type = type(exc).__name__
            message = str(exc)
            summary.outcomes.append(
                StageOutcome(
                    stage=stage,
                    status="failed",
                    elapsed_seconds=elapsed,
                    reason=f"{error_type}: {message}",
                )
            )
            # Signal the failure on the stage-event stream so the
            # dashboard can distinguish "stuck mid-run" from "stage
            # raised and the runner caught it". The runner doesn't
            # track per-phase state — emit_failed routes ``phase=None``
            # to the metric's ``phase="_"`` slot.
            emit_failed(stage, error_type=error_type, message=message)
            raise

        elapsed = time.monotonic() - stage_start
        summary.outcomes.append(
            StageOutcome(stage=stage, status="completed", elapsed_seconds=elapsed)
        )


__all__ = [
    "CANONICAL_STAGES",
    "STAGE_PHASES",
    "PipelineRunSummary",
    "StageOutcome",
    "run_pipeline",
]
