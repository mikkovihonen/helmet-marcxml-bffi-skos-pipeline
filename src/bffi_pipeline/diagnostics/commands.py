"""Operator-facing diagnostic CLI commands.

These commands don't belong to the canonical pipeline chain
(``CANONICAL_STAGES`` in ``runner.py``); they're cataloguer- and
operator-targeted reports. Registered as flat top-level commands on
the root typer app from ``cli.py``:

- ``bffi-pipeline workkey-stats`` — Stage-1 block-size distribution
  (the M4 diagnostic).
- ``bffi-pipeline ysa-disambiguation-report`` — CSV of YSA → YSO
  candidates for cataloguer review.

P-38 Phase C-3: extracted from ``cli.py`` so the per-package CLI
surface lives next to the backing module. Commands stay flat on the
root app (``app.command("workkey-stats")(workkey_stats_command)``
in cli.py) so the operator-facing CLI surface is bit-identical to
the pre-Phase-C state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bffi_pipeline.diagnostics import blocking_stats
from bffi_pipeline.stages.m9 import ysa_disambiguation_report


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
    graph = blocking_stats.load_corpus(path)
    stats = blocking_stats.compute_blocks(graph)
    typer.echo(stats.render())


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
