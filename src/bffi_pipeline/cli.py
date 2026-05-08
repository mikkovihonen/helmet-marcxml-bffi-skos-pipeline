"""Typer entry point for the bffi-pipeline CLI. Stages are wired up here."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages import marc_to_bf

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


if __name__ == "__main__":
    app()
