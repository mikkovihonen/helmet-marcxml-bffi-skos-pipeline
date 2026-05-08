"""Typer entry point for the bffi-pipeline CLI. Stages are wired up here."""

from __future__ import annotations

import typer

app = typer.Typer(help="BFFI pipeline CLI (skeleton).", no_args_is_help=True)


@app.callback()
def _root() -> None:
    """Root callback so subcommands attach cleanly once stages exist."""


if __name__ == "__main__":
    app()
