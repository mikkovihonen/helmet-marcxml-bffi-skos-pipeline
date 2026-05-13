"""Typer entry point for the bffi-pipeline CLI. Stages are wired up here."""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path
from typing import Annotated

import httpx
import typer

from bffi_pipeline import status as status_module
from bffi_pipeline.config import Settings, get_settings
from bffi_pipeline.eval import embed_benchmark
from bffi_pipeline.eval import grow as eval_grow
from bffi_pipeline.eval import harness as eval_harness
from bffi_pipeline.provenance import writer as prov_writer
from bffi_pipeline.stages import (
    bf_to_bffi,
    embeddings,
    judge,
    load,
    load_finto,
    local_concept_resolver,
    marc_to_bf,
    merge,
    reconcile,
    skosify_run,
    workkey,
    ysa_disambiguation_report,
)
from bffi_pipeline.stages.observability import (
    StageEventEmitter,
    set_active_emitter,
)


def _init_observability(settings: Settings) -> StageEventEmitter | None:
    """Construct + register the per-invocation StageEventEmitter (P-11 Phase A).

    Resolves ``settings.observability_sidecar`` and
    ``settings.run_uuid`` into a configured :class:`StageEventEmitter`
    and parks it as the process-wide active emitter so any
    pipeline-stage call can emit without function-signature plumbing.

    ``observability_sidecar`` resolution:
    - empty (default): ``<data_dir>/stage-events.jsonl``.
    - ``"none"`` or ``"/dev/null"`` (case-insensitive): stderr-only,
      no JSONL append.
    - any other value: treated as a path.

    ``run_uuid`` resolution:
    - empty (default): a fresh ``uuid4().hex`` per invocation.
    - any other value: pinned (operator-controlled, useful for
      replay).
    """
    sidecar_str = settings.observability_sidecar.strip()
    sidecar_path: Path | None
    if not sidecar_str:
        sidecar_path = settings.data_dir / "stage-events.jsonl"
    elif sidecar_str.lower() in {"none", "/dev/null", "off"}:
        sidecar_path = None
    else:
        sidecar_path = Path(sidecar_str)

    run_uuid = settings.run_uuid.strip() or _uuid.uuid4().hex
    emitter = StageEventEmitter(sidecar_path=sidecar_path, run_uuid=run_uuid)
    set_active_emitter(emitter)
    return emitter


app = typer.Typer(help="BFFI pipeline CLI.", no_args_is_help=True)
provenance_app = typer.Typer(help="Provenance graph maintenance (M7).", no_args_is_help=True)
app.add_typer(provenance_app, name="provenance")


@app.callback()
def _root() -> None:
    """Root callback so subcommands attach cleanly.

    Runs the stale-provenance warning per spec § 8 / BUILD_PLAN M7
    every invocation; suppressed silently when no provenance file
    exists (early-milestone or first-run case). Also bootstraps the
    P-11 Phase A active observability emitter so every subcommand can
    emit events without per-command plumbing — read-only commands
    (``status``, ``workkey-stats``, etc.) simply never call
    ``emit_if_active`` and the registered emitter stays idle.
    """
    warning = prov_writer.stale_provenance_warning()
    if warning is not None:
        typer.echo(warning, err=True)
    _init_observability(get_settings())


@app.command("marc-to-bf")
def marc_to_bf_command(
    input_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Directory of MARCXML files (one record per file, named <bib_id>.xml).",
        ),
    ],
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Output directory; defaults to BFFI_DATA_DIR.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Reconvert records whose output is already newer than the input.",
        ),
    ] = False,
) -> None:
    """Convert MARCXML to BIBFRAME RDF/XML (M2)."""
    target = output_dir or get_settings().data_dir
    summary = marc_to_bf.run(input_dir, output_dir=target, force=force)
    typer.echo(summary.render())
    # Partial-failure exit policy: non-zero only when *nothing* made
    # progress (no successes, no idempotent skips). 800 k-record
    # batches always have a long tail of records missing 336/337/338
    # or 1XX/7XX — those are tracked in ``_errors.jsonl`` and visible
    # in summary.render(), but they must not abort a multi-stage shell
    # driver via ``set -e``.
    if summary.failed and not summary.succeeded and not summary.skipped_idempotent:
        raise typer.Exit(code=1)


@app.command("bf-to-bffi")
def bf_to_bffi_command(
    bibframe_dir: Annotated[
        Path | None,
        typer.Option(
            "--bibframe-dir",
            "-i",
            help="Directory of BIBFRAME RDF/XML files; defaults to <BFFI_DATA_DIR>/bibframe.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Output base; defaults to BFFI_DATA_DIR. BFFI Turtle goes to <output_dir>/bffi.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Reconvert records whose output is already newer than the input.",
        ),
    ] = False,
    llm_title_cascade: Annotated[
        bool,
        typer.Option(
            "--llm-title-cascade/--no-llm-title-cascade",
            help=(
                "Escalate ambiguous parallel-title records (e.g. 'X = Y = Z' with "
                "all-Latin segments Lingua maps to the same language) to the local "
                "Qwen3 cascade for per-segment language assignment. On by default; "
                "pass --no-llm-title-cascade to skip the LLM and stay graph-only."
            ),
        ),
    ] = True,
    llm_contrib_cascade: Annotated[
        bool,
        typer.Option(
            "--llm-contrib-cascade/--no-llm-contrib-cascade",
            help=(
                "Run the MARC 245$c contributor-extraction cascade on records "
                "whose 245$c contains capitalised name-tokens not covered by "
                "100/700. Off by default — fire rate is ~13% on the corpus "
                "and the M12 gold-set validation hasn't run yet."
            ),
        ),
    ] = False,
    primary_model: Annotated[
        str | None,
        typer.Option(
            "--primary-model",
            help="Override LLM_MODEL_PRIMARY for the title-language cascade.",
        ),
    ] = None,
) -> None:
    """Convert BIBFRAME RDF/XML to BFFI Turtle (M3).

    Boundary 3 SHACL failures are flagged in `_validation.jsonl` but do not
    cause a non-zero exit; only hard errors do.
    """
    target = output_dir or get_settings().data_dir
    detector: object | None = None
    if llm_title_cascade:
        from bffi_pipeline.title_lang_llm import LangChainTitleLangDetector

        detector = LangChainTitleLangDetector(model_name=primary_model)
    contrib_extractor: object | None = None
    if llm_contrib_cascade:
        from bffi_pipeline.contrib_extract_llm import LangChainContribExtractor

        contrib_extractor = LangChainContribExtractor(model_name=primary_model)
    summary = bf_to_bffi.run(
        bibframe_dir,
        output_dir=target,
        force=force,
        llm_detector=detector,
        contrib_extractor=contrib_extractor,
    )
    typer.echo(summary.render())
    # Partial-failure exit policy: see ``marc-to-bf`` for the
    # rationale. Errored records are listed in summary.render() and
    # do not abort the pipeline unless *every* record errored.
    if summary.errored and not summary.converted and not summary.skipped_idempotent:
        raise typer.Exit(code=1)


@app.command("status")
def status_command(
    sidecar: Annotated[
        Path | None,
        typer.Option(
            "--sidecar",
            help=(
                "Path to the stage-events.jsonl sidecar. Defaults to "
                "BFFI_OBSERVABILITY_SIDECAR (typically "
                "<BFFI_DATA_DIR>/stage-events.jsonl)."
            ),
            resolve_path=True,
        ),
    ] = None,
    tail: Annotated[
        bool,
        typer.Option(
            "--tail",
            help="Re-render the summary as new events arrive (polling, 200ms).",
        ),
    ] = False,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help=(
                "Filter to events with ts >= this ISO-8601 timestamp. "
                "Pass 'now' to anchor on the latest start event."
            ),
        ),
    ] = None,
    run_uuid: Annotated[
        str | None,
        typer.Option(
            "--run-uuid",
            help="Filter to a specific run_uuid (default: all runs in the file).",
        ),
    ] = None,
) -> None:
    """Render the current pipeline state from the P-11 stage-events sidecar.

    The events come from Phase A's per-stage emitters; this command is
    a pure consumer — it doesn't write any sidecar entries of its own,
    and the observability bootstrap in ``_root`` parks an emitter that
    stays idle here.
    """
    settings = get_settings()
    if sidecar is None:
        sidecar_str = settings.observability_sidecar.strip()
        if not sidecar_str:
            sidecar = settings.data_dir / "stage-events.jsonl"
        elif sidecar_str.lower() in {"none", "/dev/null", "off"}:
            typer.echo(
                "BFFI_OBSERVABILITY_SIDECAR disables the sidecar — nothing to read.",
                err=True,
            )
            raise typer.Exit(code=2)
        else:
            sidecar = Path(sidecar_str)

    since_dt = None
    if since is not None:
        if since.lower() == "now":
            # Anchor: latest `start` ts in the file. The CLI uses
            # ``now`` as shorthand for "the run that's actually running
            # right now", which in practice is the most recent start.
            rows = status_module.parse_sidecar(sidecar)
            start_rows = [r for r in rows if r.event == "start"]
            if start_rows:
                since_dt = start_rows[-1].ts
        else:
            try:
                from datetime import datetime as _dt

                since_dt = _dt.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError as exc:
                typer.echo(f"Invalid --since timestamp: {since!r} ({exc})", err=True)
                raise typer.Exit(code=2) from exc

    if not tail:
        rows = status_module.parse_sidecar(sidecar, since=since_dt, run_uuid=run_uuid)
        typer.echo(status_module.render(status_module.collate(rows)))
        return

    # --tail: re-render on new events. Clean exit on Ctrl-C.
    try:
        for rendered in status_module.tail(sidecar, since=since_dt, run_uuid=run_uuid):
            typer.echo("\033[2J\033[H" + rendered)  # clear screen, home cursor
    except KeyboardInterrupt:
        typer.echo("", err=True)  # clean newline on exit


@app.command("serve-metrics")
def serve_metrics_command(
    port: Annotated[
        int,
        typer.Option(
            "--port",
            help="HTTP port to expose /metrics on. Default 9100.",
        ),
    ] = 9100,
    sidecar: Annotated[
        Path | None,
        typer.Option(
            "--sidecar",
            help=(
                "Path to the stage-events.jsonl sidecar to tail. Defaults "
                "to BFFI_OBSERVABILITY_SIDECAR / <BFFI_DATA_DIR>/stage-events.jsonl."
            ),
            resolve_path=True,
        ),
    ] = None,
    poll_seconds: Annotated[
        float,
        typer.Option(
            "--poll-seconds",
            help="Tail-loop poll interval in seconds. Default 1.0.",
        ),
    ] = 1.0,
) -> None:
    """Run the P-11 Phase D Prometheus exporter.

    Tails the stage-events sidecar and exposes the canonical pipeline
    metric vocabulary at ``/metrics`` on the configured port. Designed
    to run for an arbitrarily long time independent of the pipeline —
    survives stage transitions and pipeline restarts.

    Pair with the local Prometheus + Grafana stack via
    ``make observability-up`` (configured under ``docker-compose.yml``;
    Grafana auto-loads the provisioned bffi-pipeline dashboard).
    """
    settings = get_settings()
    if sidecar is None:
        sidecar_str = settings.observability_sidecar.strip()
        if not sidecar_str:
            sidecar = settings.data_dir / "stage-events.jsonl"
        elif sidecar_str.lower() in {"none", "/dev/null", "off"}:
            typer.echo(
                "BFFI_OBSERVABILITY_SIDECAR disables the sidecar — nothing to export.",
                err=True,
            )
            raise typer.Exit(code=2)
        else:
            sidecar = Path(sidecar_str)

    typer.echo(
        f"Serving Prometheus metrics at http://0.0.0.0:{port}/metrics (tailing {sidecar})",
        err=True,
    )
    from bffi_pipeline.metrics_exporter import serve

    try:
        serve(sidecar, port=port, poll_seconds=poll_seconds)
    except KeyboardInterrupt:
        typer.echo("\nExporter stopped.", err=True)


@app.command("workkey-stats")
def workkey_stats_command(
    path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            resolve_path=True,
            help=(
                "Path to a single BFFI Turtle file or to a data directory "
                "(with bffi/ and bibframe/ subdirs)."
            ),
        ),
    ],
) -> None:
    """Report Stage-1 block-size distribution (M4)."""
    graph = workkey.load_corpus(path)
    stats = workkey.compute_blocks(graph)
    typer.echo(stats.render())


@app.command("embed")
def embed_command(
    corpus_dir: Annotated[
        Path | None,
        typer.Option(
            "--corpus-dir",
            "-i",
            help="Data directory with bffi/ and bibframe/ subdirs; defaults to BFFI_DATA_DIR.",
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help=(
                "Directory for embeddings.faiss and embeddings.idmap.json; "
                "defaults to BFFI_DATA_DIR."
            ),
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    model_name: Annotated[
        str,
        typer.Option(
            "--model",
            help="HuggingFace model name (default BAAI/bge-m3).",
        ),
    ] = embeddings.DEFAULT_MODEL,
    device: Annotated[
        str,
        typer.Option(
            "--device",
            help="PyTorch device: mps (Apple Silicon), cuda, or cpu.",
        ),
    ] = embeddings.DEFAULT_DEVICE,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Embedding batch size (M5 Max saturates at 64-128).",
        ),
    ] = embeddings.DEFAULT_BATCH_SIZE,
    top_k: Annotated[
        int,
        typer.Option(
            "--top-k",
            help="Top-k neighbours per Work for candidate-pair generation.",
        ),
    ] = embeddings.DEFAULT_TOP_K,
    cross_block: Annotated[
        bool,
        typer.Option(
            "--cross-block",
            help="Keep candidate pairs whose Stage-1 blocking keys differ.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Rebuild the index even when persisted files are newer than inputs.",
        ),
    ] = False,
) -> None:
    """Build the FAISS HNSW index and emit candidate pairs (M5)."""
    target = output_dir or get_settings().data_dir
    build_result = embeddings.build_index(
        corpus_dir,
        output_dir=target,
        model_name=model_name,
        device=device,
        batch_size=batch_size,
        force=force,
    )
    typer.echo(build_result.render())
    stats = embeddings.query_candidates(target, top_k=top_k, cross_block=cross_block)
    typer.echo(stats.render())


@app.command("embed-benchmark")
def embed_benchmark_command(
    models: Annotated[
        list[str] | None,
        typer.Option(
            "--model",
            help=(
                "HuggingFace model name; pass multiple times to compare. "
                "Defaults to BGE-M3 / multilingual-e5-large / jina-v3."
            ),
        ),
    ] = None,
    gold_path: Annotated[
        Path | None,
        typer.Option(
            "--gold-path",
            help="Path to gold/gold.jsonl; defaults to repo gold/ directory.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    device: Annotated[
        str,
        typer.Option(
            "--device",
            help="PyTorch device: mps (Apple Silicon), cuda, or cpu.",
        ),
    ] = embeddings.DEFAULT_DEVICE,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Embedding batch size.",
        ),
    ] = embeddings.DEFAULT_BATCH_SIZE,
) -> None:
    """Compare embedding models on the gold set's same_work / different_work gap (M5 / M12)."""
    candidate_models = tuple(models) if models else embed_benchmark.DEFAULT_MODELS
    results = embed_benchmark.benchmark_models(
        models=candidate_models,
        gold_path=gold_path,
        device=device,
        batch_size=batch_size,
    )
    typer.echo(embed_benchmark.render_comparison(results))


@app.command("eval")
def eval_command(
    run_label: Annotated[
        str,
        typer.Option(
            "--run-label",
            help=(
                "Identifier for this run (e.g. 'qwen3-32b-prompt-v3'). Becomes the "
                "filename stem under --output-dir and is recorded in the JSON summary."
            ),
        ),
    ],
    gold_path: Annotated[
        Path | None,
        typer.Option(
            "--gold-path",
            help="Path to gold/gold.jsonl; defaults to repo gold/ directory.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help=("Directory for the JSON summary; defaults to <repo>/eval-runs (gitignored)."),
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Score the gold set against the M6 judge and write a JSON summary (M12).

    Eval is **not in CI** per spec § 9; this subcommand is invoked
    locally on the M5 Max via ``make eval`` before any PR that touches
    prompts / gold / judge code. The text rendering is paste-ready for
    the PR description; the JSON file is the durable record.
    """
    summary, out_path = eval_harness.run_eval(
        run_label=run_label,
        gold_path=gold_path,
        output_dir=output_dir,
    )
    typer.echo(eval_harness.render_text(summary))
    typer.echo("")
    typer.echo(f"Summary written to {out_path}")


@app.command("grow-gold")
def grow_gold_command(
    fuseki_url: Annotated[
        str | None,
        typer.Option(
            "--fuseki-url",
            help="Fuseki dataset base URL; defaults to FUSEKI_URL.",
        ),
    ] = None,
    output_path: Annotated[
        Path | None,
        typer.Option(
            "--output-path",
            help=(
                "Where to write the candidate JSONL; defaults to "
                "gold/grow-candidates.jsonl in the repo."
            ),
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Grow the gold set from human-overridden judge decisions (M12 phase 3).

    Run monthly per spec § 9. Writes a JSONL of candidate cases the
    cataloguer reviews — each row needs ``category`` filled in by hand
    before being merged into ``gold/gold.jsonl``. New cases default to
    ``holdout: false``; cataloguers flip the flag explicitly when a
    case should join the eval set.
    """
    result = eval_grow.grow(
        fuseki_url=fuseki_url,
        output_path=output_path,
    )
    typer.echo(result.render())


@app.command("embed-stats")
def embed_stats_command(
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory holding embeddings.faiss + idmap; defaults to BFFI_DATA_DIR.",
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    top_k: Annotated[
        int,
        typer.Option("--top-k", help="Top-k neighbours per Work."),
    ] = embeddings.DEFAULT_TOP_K,
    cross_block: Annotated[
        bool,
        typer.Option(
            "--cross-block",
            help="Keep candidate pairs whose Stage-1 blocking keys differ.",
        ),
    ] = False,
) -> None:
    """Report band counts and similarity distribution from the persisted index (M5)."""
    target = output_dir or get_settings().data_dir
    stats = embeddings.query_candidates(target, top_k=top_k, cross_block=cross_block)
    typer.echo(stats.render())


@app.command("judge")
def judge_command(
    candidates_path: Annotated[
        Path | None,
        typer.Option(
            "--candidates-path",
            "-i",
            help="M5 embed-candidates.jsonl; defaults to <BFFI_DATA_DIR>/embed-candidates.jsonl.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    output_path: Annotated[
        Path | None,
        typer.Option(
            "--output-path",
            "-o",
            help="Per-pair decisions JSONL; defaults to <BFFI_DATA_DIR>/judge-decisions.jsonl.",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    bffi_corpus_dir: Annotated[
        Path | None,
        typer.Option(
            "--bffi-corpus-dir",
            help=(
                "Directory holding bffi/*.ttl + bibframe/*.rdf so the judge can resolve "
                "Work URIs to WorkRecord; defaults to BFFI_DATA_DIR."
            ),
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    primary_model: Annotated[
        str | None,
        typer.Option(
            "--primary-model",
            help="Override LLM_MODEL_PRIMARY for this run.",
        ),
    ] = None,
    fallback_model: Annotated[
        str | None,
        typer.Option(
            "--fallback-model",
            help="Override LLM_MODEL_FALLBACK for this run.",
        ),
    ] = None,
    restart: Annotated[
        bool,
        typer.Option(
            "--restart",
            help="Wipe the output JSONL and checkpoint and re-run from scratch.",
        ),
    ] = False,
    provenance: Annotated[
        bool,
        typer.Option(
            "--provenance/--no-provenance",
            help=(
                "Emit per-decision bffi-prov:WorkMergeDecision Activities to "
                "<BFFI_DATA_DIR>/provenance.ttl (spec § 8). On by default; the "
                "off-switch is for local development."
            ),
        ),
    ] = True,
    provenance_path: Annotated[
        Path | None,
        typer.Option(
            "--provenance-path",
            help="Path to provenance.ttl; defaults to <BFFI_DATA_DIR>/provenance.ttl.",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            help=(
                "Pairs to judge in parallel. Default 1 (Ollama serial); mlx-lm "
                "production runs sweep {4, 8, 16, 32} per spec § 11. Output JSONL "
                "stays in input order; the checkpoint advances contiguously."
            ),
            min=1,
        ),
    ] = judge.DEFAULT_CONCURRENCY,
    full_rationale: Annotated[
        bool,
        typer.Option(
            "--full-rationale/--no-full-rationale",
            help=(
                "Require a substantive natural-language rationale on every "
                "judge call (default on). Pass --no-full-rationale to switch "
                "to the fast prompt (judge_v1_fast.txt): the LLM may set "
                "rationale=null for 'same_work' / 'different_work' decisions "
                "with confidence ≥ 0.85. Rationale stays required for "
                "'uncertain' or lower-confidence outcomes (where it drives "
                "the human-review queue and the cascade re-run). At corpus "
                "scale fast mode cuts ~50-200 generation tokens per high-"
                "confidence pair; the audit trail keeps the structured "
                "matching_fields / diverging_fields / confidence evidence."
            ),
        ),
    ] = True,
    abort_budget_seconds: Annotated[
        int | None,
        typer.Option(
            "--abort-budget-seconds",
            help=(
                "Per-call LLM wall-time ceiling. Overrides "
                "LLM_CALL_TIMEOUT_SECONDS for this run. When a single "
                "LLM call exceeds this budget, the cascade's existing "
                "retry stack (5/30/120 s) retries the same pair on the "
                "same model first; only after retries exhaust on both "
                "primary and fallback does the pair land as "
                "'uncertain' with bffi-prov:stage='watchdog-aborted'. "
                "Watchdog events stream to stderr (prefix 'WATCHDOG_EVENT') "
                "and to <BFFI_DATA_DIR>/watchdog-events.jsonl for "
                "post-run audit. See docs/plans/completed/p-03-m6-stall-watchdog.md."
            ),
            min=1,
        ),
    ] = None,
    pair_budget_seconds: Annotated[
        int | None,
        typer.Option(
            "--pair-budget-seconds",
            help=(
                "Per-pair LLM wall-time ceiling for the whole cascade "
                "(primary + fallback + retries). Overrides "
                "LLM_PAIR_TIMEOUT_SECONDS for this run. Orthogonal to "
                "--abort-budget-seconds: catches pairs where many "
                "legitimate calls pile up to a long pair time even "
                "when no single call exceeds the per-call ceiling. "
                "Pairs exceeding this budget land as 'uncertain' with "
                "bffi-prov:stage='watchdog-aborted' and emit a "
                "'pair_budget_exceeded' watchdog event. See "
                "docs/plans/completed/p-03-m6-stall-watchdog.md Phase B."
            ),
            min=1,
        ),
    ] = None,
) -> None:
    """Run the cascade judge over M5's escalate-band candidate pairs (M6)."""
    settings = get_settings()
    if abort_budget_seconds is not None:
        # Mutate the cached Settings singleton in place. The judge stage
        # reads ``settings.llm_call_timeout_seconds`` lazily inside
        # ``_build_chain`` so this override propagates without further
        # plumbing.
        settings.llm_call_timeout_seconds = abort_budget_seconds
    if pair_budget_seconds is not None:
        settings.llm_pair_timeout_seconds = pair_budget_seconds
    target_output = output_path or (settings.data_dir / judge.DECISIONS_FILENAME)

    def _on_progress(snapshot: judge.JudgeBatchProgress) -> None:
        typer.echo(snapshot.render())

    if provenance:
        with prov_writer.ProvenanceWriter(provenance_path) as writer:
            result = judge.judge_batch(
                candidates_path,
                target_output,
                bffi_corpus_dir=bffi_corpus_dir,
                resume=not restart,
                primary_model=primary_model,
                fallback_model=fallback_model,
                progress_callback=_on_progress,
                provenance_writer=writer,
                concurrency=concurrency,
                full_rationale=full_rationale,
            )
    else:
        result = judge.judge_batch(
            candidates_path,
            target_output,
            bffi_corpus_dir=bffi_corpus_dir,
            resume=not restart,
            primary_model=primary_model,
            fallback_model=fallback_model,
            progress_callback=_on_progress,
            concurrency=concurrency,
            full_rationale=full_rationale,
        )
    typer.echo(result.render())


@app.command("merge")
def merge_command(
    decisions_path: Annotated[
        Path | None,
        typer.Option(
            "--decisions-path",
            help="M6 judge-decisions.jsonl; defaults to <BFFI_DATA_DIR>/judge-decisions.jsonl.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    bffi_corpus_dir: Annotated[
        Path | None,
        typer.Option(
            "--bffi-corpus-dir",
            help="Directory holding bffi/*.ttl + bibframe/*.rdf; defaults to BFFI_DATA_DIR.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    output_path: Annotated[
        Path | None,
        typer.Option(
            "--output-path",
            "-o",
            help="canonical.ttl path; defaults to <BFFI_DATA_DIR>/canonical.ttl.",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    map_path: Annotated[
        Path | None,
        typer.Option(
            "--map-path",
            help="canonical-map.jsonl path; defaults to <BFFI_DATA_DIR>/canonical-map.jsonl.",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    helmet_map_path: Annotated[
        Path | None,
        typer.Option(
            "--helmet-map-path",
            help="M2 helmet-map.jsonl; defaults to <BFFI_DATA_DIR>/helmet-map.jsonl.",
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Apply judge decisions to mint canonical Works (M8)."""
    result = merge.apply_merge(
        decisions_path,
        bffi_corpus_dir,
        output_path=output_path,
        map_path=map_path,
        helmet_map_path=helmet_map_path,
    )
    typer.echo(result.render())


_RECONCILE_KIND_GROUPS: dict[str, frozenset[reconcile.AuthorityKind]] = {
    "creators": frozenset({"person", "corporate_body"}),
    "subjects": frozenset({"subject"}),
    "genres": frozenset({"genre_form", "music_form"}),
    "all": reconcile.ALL_AUTHORITY_KINDS,
}


def _parse_reconcile_kinds(raw: str | None) -> frozenset[reconcile.AuthorityKind] | None:
    """Parse the ``--kinds`` CLI option into a runtime kind set.

    ``None`` (or ``"all"``, or whitespace) returns ``None`` so the
    orchestrator runs every kind by default. Unknown groups raise
    ``typer.BadParameter`` so cataloguers see the typo immediately.
    """
    if raw is None or not raw.strip():
        return None
    selected: set[reconcile.AuthorityKind] = set()
    for token in raw.split(","):
        name = token.strip().casefold()
        if not name:
            continue
        if name == "all":
            return None
        group = _RECONCILE_KIND_GROUPS.get(name)
        if group is None:
            raise typer.BadParameter(
                f"Unknown --kinds group {name!r}; choose from {sorted(_RECONCILE_KIND_GROUPS)}."
            )
        selected.update(group)
    return frozenset(selected) if selected else None


@app.command("reconcile")
def reconcile_command(
    canonical_path: Annotated[
        Path | None,
        typer.Option(
            "--canonical-path",
            "-i",
            help="Path to canonical.ttl; defaults to <BFFI_DATA_DIR>/canonical.ttl.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    output_path: Annotated[
        Path | None,
        typer.Option(
            "--output-path",
            "-o",
            help="Where to write the reconciled Turtle. Defaults to overwriting --canonical-path.",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    primary_model: Annotated[
        str | None,
        typer.Option(
            "--primary-model",
            help="Override LLM_MODEL_PRIMARY for this run.",
        ),
    ] = None,
    provenance: Annotated[
        bool,
        typer.Option(
            "--provenance/--no-provenance",
            help=(
                "Append per-attempt bffi-prov:Reconciliation Activities to "
                "<BFFI_DATA_DIR>/provenance.ttl. On by default."
            ),
        ),
    ] = True,
    provenance_path: Annotated[
        Path | None,
        typer.Option(
            "--provenance-path",
            help="Path to provenance.ttl; defaults to <BFFI_DATA_DIR>/provenance.ttl.",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    kinds: Annotated[
        str | None,
        typer.Option(
            "--kinds",
            help=(
                "Comma-separated reconciliation groups to run. Choices: "
                "'creators' (KANTO+VIAF persons/corporate bodies), "
                "'subjects' (YSO topical), 'genres' (KAUNO + MUSO music form), "
                "or 'all'. Default is all."
            ),
        ),
    ] = None,
    local_resolver: Annotated[
        bool,
        typer.Option(
            "--local-resolver/--no-local-resolver",
            help=(
                "Tier-0 lookup: try an exact prefLabel match against the local "
                "Fuseki authority graphs (YSO/KAUNO/MUSO/SLM) before calling "
                "Finto. Avoids ~one HTTP round-trip per cataloguer literal at "
                "corpus scale. On by default; pass --no-local-resolver to "
                "force every literal through the Finto API."
            ),
        ),
    ] = True,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            help=(
                "ThreadPoolExecutor max_workers for the tier-2 picker dispatch. "
                "tier-0 / tier-1 / tier-3 stay single-threaded. Defaults to "
                "M9_CONCURRENCY from the env / .env (typically 4). Set 1 to "
                "restore pre-Phase-A sequential behaviour. See "
                "docs/plans/backlog/p-10-m9-reconcile-throughput.md Phase A."
            ),
        ),
    ] = -1,
    field_timeout: Annotated[
        int,
        typer.Option(
            "--field-timeout-seconds",
            help=(
                "Per-field wall budget for the picker (tier-2). On exceed, the "
                "field is marked bffi-prov:stage='watchdog-aborted' and falls "
                "through to tier-3 fallback. Defaults to "
                "LLM_M9_FIELD_TIMEOUT_SECONDS. Set 0 to disable the budget."
            ),
        ),
    ] = -1,
    phase1_concurrency: Annotated[
        int,
        typer.Option(
            "--phase1-concurrency",
            help=(
                "ThreadPoolExecutor max_workers for the Phase 1 pre-pass "
                "(tier-0 SPARQL + Finto/VIAF candidate query). Separate from "
                "--concurrency (tier-2 picker, GPU-bound) because Phase 1's "
                "binding constraint is HTTP/SPARQL throughput. Defaults to "
                "M9_PHASE1_CONCURRENCY (typically 8). Set 1 to restore the "
                "post-Phase-A serial pre-pass. See "
                "docs/plans/in-progress/p-10-m9-reconcile-throughput.md Phase A2."
            ),
        ),
    ] = -1,
    tier0_expansion: Annotated[
        bool | None,
        typer.Option(
            "--tier0-expansion/--no-tier0-expansion",
            help=(
                "P-10 Phase C: enable the folded-lookup tier-0 path "
                "(matches bffi:foldedLabel materialised by load-finto). "
                "Bindings that required the fold are flagged "
                "bffi:descriptionAuthentication=needs-review for cataloguer "
                "audit. Defaults to BFFI_M9_TIER0_EXPANSION from the env "
                "(off until the 200-sample audit confirms zero false "
                "positives on this corpus). See "
                "docs/plans/in-progress/p-10-m9-reconcile-throughput.md Phase C."
            ),
        ),
    ] = None,
) -> None:
    """Reconcile canonical Work creators + subjects against KANTO / VIAF / YSO / KAUNO / MUSO."""
    settings = get_settings()
    target = output_path or canonical_path or (settings.data_dir / "canonical.ttl")
    selected_kinds = _parse_reconcile_kinds(kinds)
    # ``-1`` sentinels mean "fall through to settings"; explicit 0 / 1 / 4 from
    # the CLI override the env. This keeps Phase A's rollback knob ergonomic
    # (`--concurrency 1 --field-timeout-seconds 0`) without forcing the
    # operator to also unset the env vars.
    effective_concurrency = settings.m9_concurrency if concurrency < 0 else concurrency
    effective_field_timeout = (
        settings.llm_m9_field_timeout_seconds if field_timeout < 0 else field_timeout
    )
    effective_phase1_concurrency = (
        settings.m9_phase1_concurrency if phase1_concurrency < 0 else phase1_concurrency
    )
    effective_tier0_expansion = (
        settings.m9_tier0_expansion if tier0_expansion is None else tier0_expansion
    )
    watchdog_sidecar = settings.data_dir / "watchdog-events.jsonl"

    http_client = httpx.Client(timeout=10.0)
    try:
        client = reconcile.FintoSkosmosClient(http_client=http_client)
        fallback = reconcile.ViafClient(http_client=http_client)

        # Picker factory: at c>=2 the orchestrator builds one
        # ``LangChainLLMPicker`` per worker thread (LangChain's
        # underlying OpenAI-compat client has no documented
        # thread-safety guarantee; one client per thread is cheap).
        # At c=1 the factory is called once.
        def picker_factory() -> reconcile.LLMPicker:
            return reconcile.LangChainLLMPicker(model_name=primary_model)

        resolver: local_concept_resolver.LocalConceptResolver | None = (
            local_concept_resolver.FusekiConceptResolver(
                http_client=http_client,
                fuseki_url=settings.fuseki_url,
                tier0_expansion_enabled=effective_tier0_expansion,
            )
            if local_resolver
            else None
        )

        if provenance:
            with prov_writer.ProvenanceWriter(provenance_path) as writer:
                summary, _outcomes = reconcile.apply_reconciliation(
                    canonical_path,
                    output_path=target,
                    client=client,
                    fallback_client=fallback,
                    picker_factory=picker_factory,
                    provenance_graph=writer.graph,
                    kinds=selected_kinds,
                    local_resolver=resolver,
                    concurrency=effective_concurrency,
                    field_timeout_seconds=effective_field_timeout,
                    watchdog_sidecar_path=watchdog_sidecar,
                    phase1_concurrency=effective_phase1_concurrency,
                )
        else:
            summary, _outcomes = reconcile.apply_reconciliation(
                canonical_path,
                output_path=target,
                client=client,
                fallback_client=fallback,
                picker_factory=picker_factory,
                kinds=selected_kinds,
                local_resolver=resolver,
                concurrency=effective_concurrency,
                field_timeout_seconds=effective_field_timeout,
                watchdog_sidecar_path=watchdog_sidecar,
                phase1_concurrency=effective_phase1_concurrency,
            )
    finally:
        http_client.close()
    typer.echo(summary.render())


@app.command("ysa-disambiguation-report")
def ysa_disambiguation_report_command(
    canonical_path: Annotated[
        Path | None,
        typer.Option(
            "--canonical-path",
            "-i",
            help="Path to canonical.ttl; defaults to <BFFI_DATA_DIR>/canonical.ttl.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    output_path: Annotated[
        Path | None,
        typer.Option(
            "--output-path",
            "-o",
            help=("CSV path; defaults to <BFFI_DATA_DIR>/ysa-disambiguation-report.csv."),
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Emit a cataloguer-review CSV for YSA → YSO disambiguation residue.

    Walks canonical.ttl for subject literals that look like
    pre-2018 bare YSA forms (``lapset``, ``2000-luku``, …) and
    queries the locally-loaded YSO graph for the disambiguated forms
    YSO replaced them with. One row per (helmet_bib_id, literal,
    candidate) tuple; see ``docs/runbook.md`` for the operational
    background and the SPARQL needs-review filter this report
    complements.
    """
    summary = ysa_disambiguation_report.run(
        canonical_path=canonical_path,
        output_path=output_path,
    )
    typer.echo(summary.render())


@app.command("skosify")
def skosify_command(
    canonical_path: Annotated[
        Path | None,
        typer.Option(
            "--canonical-path",
            "-i",
            help="Path to canonical.ttl; defaults to <BFFI_DATA_DIR>/canonical.ttl.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    output_path: Annotated[
        Path | None,
        typer.Option(
            "--output-path",
            "-o",
            help=(
                "Path to canonical-skosified.ttl; "
                "defaults to <BFFI_DATA_DIR>/canonical-skosified.ttl."
            ),
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    overlay_path: Annotated[
        Path | None,
        typer.Option(
            "--overlay-path",
            help=(
                "Path to bffi-skos-overlay.ttl; defaults to "
                "config/overlay/bffi-skos-overlay.ttl in the repo."
            ),
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config-path",
            help="Path to bffi.cfg; defaults to config/bffi.cfg in the repo.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-run Skosify even when the existing output is newer than the inputs.",
        ),
    ] = False,
) -> None:
    """Run Skosify with the BFFI overlay to produce dual-typed output (M10 phase 1)."""
    result = skosify_run.run(
        canonical_path,
        output_path=output_path,
        overlay_path=overlay_path,
        config_path=config_path,
        force=force,
    )
    typer.echo(result.render())


@app.command("load")
def load_command(
    skosified_path: Annotated[
        Path | None,
        typer.Option(
            "--skosified-path",
            help=(
                "Path to canonical-skosified.ttl from M10 phase 1; "
                "defaults to <BFFI_DATA_DIR>/canonical-skosified.ttl."
            ),
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    admin_vocab_path: Annotated[
        Path | None,
        typer.Option(
            "--admin-vocab-path",
            help=(
                "Path to bffi-admin-vocabulary.ttl; "
                "defaults to config/bffi-admin-vocabulary.ttl in the repo."
            ),
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    provenance_path: Annotated[
        Path | None,
        typer.Option(
            "--provenance-path",
            help="Path to provenance.ttl; defaults to <BFFI_DATA_DIR>/provenance.ttl.",
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    fuseki_url: Annotated[
        str | None,
        typer.Option(
            "--fuseki-url",
            help="Fuseki dataset endpoint; defaults to FUSEKI_URL from settings.",
        ),
    ] = None,
) -> None:
    """Load skosified data + provenance into Fuseki and run Boundary-5 smokes (M10)."""
    settings = get_settings()
    auth: tuple[str, str] | None = (
        (settings.fuseki_user, settings.fuseki_password)
        if settings.fuseki_user and settings.fuseki_password
        else None
    )
    with httpx.Client(timeout=30.0, auth=auth) as client:
        result = load.run(
            skosified_path=skosified_path,
            admin_vocab_path=admin_vocab_path,
            provenance_path=provenance_path,
            fuseki_url=fuseki_url,
            client=client,
        )
    typer.echo(result.render())
    if not result.success:
        raise typer.Exit(code=1)


@app.command("load-finto")
def load_finto_command(
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help=(
                "Where Finto dump TTLs are cached; defaults to <BFFI_DATA_DIR>. "
                "The actual files land under <output-dir>/finto-dumps/."
            ),
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    fuseki_url: Annotated[
        str | None,
        typer.Option(
            "--fuseki-url",
            help="Fuseki dataset endpoint; defaults to FUSEKI_URL from settings.",
        ),
    ] = None,
    max_age_days: Annotated[
        int,
        typer.Option(
            "--max-age-days",
            help="Re-download a cached dump only if its mtime is older than this many days.",
        ),
    ] = 30,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-download every dump regardless of cache freshness.",
        ),
    ] = False,
    fold_pref_labels: Annotated[
        bool,
        typer.Option(
            "--fold-pref-labels/--no-fold-pref-labels",
            help=(
                "Materialise bffi:foldedLabel triples on every "
                "skos:prefLabel and skos:altLabel of every concept in the "
                "downloaded dump. Default on; the resolver-side feature "
                "flag BFFI_M9_TIER0_EXPANSION still controls whether the "
                "folded predicate is actually queried at reconcile time, "
                "so this is safe to leave on. See "
                "docs/plans/in-progress/p-10-m9-reconcile-throughput.md "
                "Phase C.1."
            ),
        ),
    ] = True,
) -> None:
    """Refresh the KANTO/YSO/KAUNO/MUSO/SLM named graphs in Fuseki.

    Downloads the canonical Turtle dumps from api.finto.fi and PUTs each
    into its named graph via SPARQL Graph Store Protocol. Skosmos's
    per-vocab entries in `config/skosmos-config.ttl` point at the same
    graph URIs, so labels light up on the next concept-page render.
    """
    settings = get_settings()
    auth: tuple[str, str] | None = (
        (settings.fuseki_user, settings.fuseki_password)
        if settings.fuseki_user and settings.fuseki_password
        else None
    )
    with httpx.Client(
        timeout=httpx.Timeout(60.0, read=300.0),
        follow_redirects=True,
        headers={"User-Agent": load_finto.DEFAULT_USER_AGENT},
        auth=auth,
    ) as client:
        summary = load_finto.run(
            output_dir=output_dir,
            fuseki_url=fuseki_url,
            max_age_days=max_age_days,
            force=force,
            http_client=client,
            fold_pref_labels=fold_pref_labels,
        )
    typer.echo(summary.render())


@app.command("lookup-helmet")
def lookup_helmet_command(
    helmet_id: Annotated[
        str,
        typer.Argument(
            help="The Helmet bibliographic record ID to look up.",
        ),
    ],
    fuseki_url: Annotated[
        str | None,
        typer.Option(
            "--fuseki-url",
            help="Fuseki dataset endpoint; defaults to FUSEKI_URL from settings.",
        ),
    ] = None,
) -> None:
    """Resolve a Helmet bib ID to its canonical Work + Expressions (M10)."""
    target_url = fuseki_url or get_settings().fuseki_url
    with httpx.Client(timeout=30.0) as client:
        rows = load.lookup_helmet_id(client, helmet_id, fuseki_url=target_url)
    typer.echo(load.render_helmet_lookup(rows))


# --- bffi-pipeline provenance ... -----------------------------------------


def _parse_age_spec(spec: str) -> int:
    """Parse a duration like ``"90d"`` / ``"30d"`` / a bare number into days."""
    text = spec.strip().lower()
    if not text:
        raise typer.BadParameter("--older-than must be a duration like '90d'.")
    if text.endswith("d"):
        text = text[:-1]
    try:
        days = int(text)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--older-than {spec!r} is not a recognised duration; use '90d' or a bare integer."
        ) from exc
    if days < 0:
        raise typer.BadParameter("--older-than must be non-negative.")
    return days


@provenance_app.command("compact")
def provenance_compact_command(
    older_than: Annotated[
        str,
        typer.Option(
            "--older-than",
            help="Strip rawResponse from Activities older than this; e.g. '90d'.",
        ),
    ] = f"{prov_writer.COMPACTION_AGE_DAYS}d",
    provenance_path: Annotated[
        Path | None,
        typer.Option(
            "--path",
            help="Path to provenance.ttl; defaults to <BFFI_DATA_DIR>/provenance.ttl.",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Strip stale rawResponse literals and refresh lastCompactedAt (M7)."""
    days = _parse_age_spec(older_than)
    removed = prov_writer.compact_provenance(
        older_than_days=days,
        provenance_path=provenance_path,
    )
    typer.echo(
        f"compaction complete: removed {removed} bffi-prov:rawResponse literal(s) "
        f"older than {days}d"
    )


if __name__ == "__main__":
    app()
