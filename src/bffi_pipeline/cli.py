"""Typer entry point for the bffi-pipeline CLI. Stages are wired up here."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bffi_pipeline.config import get_settings
from bffi_pipeline.eval import embed_benchmark
from bffi_pipeline.provenance import writer as prov_writer
from bffi_pipeline.stages import bf_to_bffi, embeddings, judge, marc_to_bf, workkey

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
) -> None:
    """Convert BIBFRAME RDF/XML to BFFI Turtle (M3).

    Boundary 3 SHACL failures are flagged in `_validation.jsonl` but do not
    cause a non-zero exit; only hard errors do.
    """
    target = output_dir or get_settings().data_dir
    summary = bf_to_bffi.run(bibframe_dir, output_dir=target, force=force)
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
) -> None:
    """Run the cascade judge over M5's escalate-band candidate pairs (M6)."""
    settings = get_settings()
    target_output = output_path or (settings.data_dir / judge.DECISIONS_FILENAME)

    def _on_progress(snapshot: judge.JudgeBatchProgress) -> None:
        typer.echo(snapshot.render())

    result = judge.judge_batch(
        candidates_path,
        target_output,
        bffi_corpus_dir=bffi_corpus_dir,
        resume=not restart,
        primary_model=primary_model,
        fallback_model=fallback_model,
        progress_callback=_on_progress,
    )
    typer.echo(result.render())


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
