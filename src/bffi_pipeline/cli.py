"""Typer entry point for the bffi-pipeline CLI. Stages are wired up here."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages import bf_to_bffi, embeddings, marc_to_bf, workkey

app = typer.Typer(help="BFFI pipeline CLI.", no_args_is_help=True)


@app.callback()
def _root() -> None:
    """Root callback so subcommands attach cleanly."""


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


if __name__ == "__main__":
    app()
