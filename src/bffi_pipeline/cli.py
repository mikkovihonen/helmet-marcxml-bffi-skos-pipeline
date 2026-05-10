"""Typer entry point for the bffi-pipeline CLI. Stages are wired up here."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import httpx
import typer

from bffi_pipeline.config import get_settings
from bffi_pipeline.eval import embed_benchmark
from bffi_pipeline.eval import grow as eval_grow
from bffi_pipeline.eval import harness as eval_harness
from bffi_pipeline.provenance import writer as prov_writer
from bffi_pipeline.stages import (
    bf_to_bffi,
    embeddings,
    judge,
    load,
    marc_to_bf,
    merge,
    reconcile,
    skosify_run,
    workkey,
)

app = typer.Typer(help="BFFI pipeline CLI.", no_args_is_help=True)
provenance_app = typer.Typer(help="Provenance graph maintenance (M7).", no_args_is_help=True)
app.add_typer(provenance_app, name="provenance")


@app.callback()
def _root() -> None:
    """Root callback so subcommands attach cleanly.

    Runs the stale-provenance warning per spec § 8 / BUILD_PLAN M7
    every invocation; suppressed silently when no provenance file
    exists (early-milestone or first-run case).
    """
    warning = prov_writer.stale_provenance_warning()
    if warning is not None:
        typer.echo(warning, err=True)


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
    if summary.failed:
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
    summary = bf_to_bffi.run(
        bibframe_dir,
        output_dir=target,
        force=force,
        llm_detector=detector,
    )
    typer.echo(summary.render())
    if summary.errored:
        raise typer.Exit(code=1)


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
                "Pairs to judge in parallel. Default 1 (Ollama serial); vllm-mlx "
                "production runs sweep {4, 8, 16, 32} per spec § 11. Output JSONL "
                "stays in input order; the checkpoint advances contiguously."
            ),
            min=1,
        ),
    ] = judge.DEFAULT_CONCURRENCY,
) -> None:
    """Run the cascade judge over M5's escalate-band candidate pairs (M6)."""
    settings = get_settings()
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
) -> None:
    """Reconcile canonical Work creators + subjects against KANTO / VIAF / YSO / KAUNO / MUSO."""
    settings = get_settings()
    target = output_path or canonical_path or (settings.data_dir / "canonical.ttl")
    selected_kinds = _parse_reconcile_kinds(kinds)

    http_client = httpx.Client(timeout=10.0)
    try:
        client = reconcile.FintoSkosmosClient(http_client=http_client)
        fallback = reconcile.ViafClient(http_client=http_client)
        picker = reconcile.LangChainLLMPicker(model_name=primary_model)

        if provenance:
            with prov_writer.ProvenanceWriter(provenance_path) as writer:
                summary, _outcomes = reconcile.apply_reconciliation(
                    canonical_path,
                    output_path=target,
                    client=client,
                    fallback_client=fallback,
                    picker=picker,
                    provenance_graph=writer.graph,
                    kinds=selected_kinds,
                )
        else:
            summary, _outcomes = reconcile.apply_reconciliation(
                canonical_path,
                output_path=target,
                client=client,
                fallback_client=fallback,
                picker=picker,
                kinds=selected_kinds,
            )
    finally:
        http_client.close()
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
