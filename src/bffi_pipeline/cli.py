"""Typer entry point for the bffi-pipeline CLI. Stages are wired up here."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages import bf_to_bffi, marc_to_bf, workkey

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


if __name__ == "__main__":
    app()
