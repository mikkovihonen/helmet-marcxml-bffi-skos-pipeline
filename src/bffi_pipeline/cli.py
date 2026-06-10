"""Typer CLI for the BFFI conversion pipeline.

Five subcommands map one-to-one to the four conversion pillars
+ the eval harness on the `rewrite` branch:

  - `export`             — Sierra (Postgres replica) → MARCXML
  - `marc-to-bibframe`   — MARCXML → BIBFRAME via the LoC marc2bibframe2 XSLT
  - `bibframe-to-bffi`   — BIBFRAME → BFFI canonical Turtle (BFFI-only emit)
  - `bffi-to-marc`       — BFFI graph → reconstructed MARCXML (reverse direction)
  - `roundtrip-eval`     — diff source MARC vs reconstructed MARC; cataloguer-review HTML

Plus `serve-metrics` for the observability stack (Prometheus exporter that
tails `runs/*/stage-events.jsonl` sidecars).

Stage subcommands are currently scaffolded stubs — they will be filled in
by subsequent plans under `docs/plans/`. Run `bffi-pipeline --help` to list
them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.marc_to_bibframe.runner import (
    ConversionOptions,
    convert_corpus,
)
from bffi_pipeline.stages.marc_to_bibframe.xslt import XsltPaths

app = typer.Typer(
    name="bffi-pipeline",
    no_args_is_help=True,
    help="MARCXML ↔ BIBFRAME ↔ BFFI conversion pipeline (rewrite branch).",
)


@app.command("export")
def export_command() -> None:
    """Sierra → MARCXML.

    Stream bibliographic records from the Sierra Postgres replica and emit
    one MARCXML file per record into the configured runs directory.

    Implementation defers to :mod:`marcxml_export_pipeline.sierra.marcxml`
    once wired up.
    """
    raise NotImplementedError("export stage scaffolded; not yet implemented")


@app.command("marc-to-bibframe")
def marc_to_bibframe_command(
    input_dir: Annotated[
        Path,
        typer.Option(
            "--input-dir",
            help="Directory of per-record MARCXML files (`*.xml`).",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Where to write per-record BIBFRAME RDF/XML (`<stem>.bibframe.xml`).",
        ),
    ],
    baseuri: Annotated[
        str,
        typer.Option(
            "--baseuri",
            help="URI stem the marc2bibframe2 XSLT uses for minted entities.",
        ),
    ] = "http://urn.fi/URN:NBN:fi:bib:",
    idsource: Annotated[
        str | None,
        typer.Option(
            "--idsource",
            help="Optional source URI for the Local identifier minted from the ID field.",
        ),
    ] = None,
    no_preprocess: Annotated[
        bool,
        typer.Option(
            "--no-preprocess",
            help="Skip the LoC preprocessing splitter step (defaults to on).",
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Per-record xsltproc timeout in seconds."),
    ] = 60.0,
) -> None:
    """MARCXML → BIBFRAME via the LoC marc2bibframe2 XSLT.

    Reads ``input_dir/*.xml`` MARCXML records, runs each through the
    vendored marc2bibframe2 XSLT (optional preprocess + main convert),
    and writes ``output_dir/<stem>.bibframe.xml`` per record. Failures
    are logged via the observability sidecar and counted in the summary;
    the run continues past per-record failures.
    """
    settings = get_settings()
    options = ConversionOptions(
        input_dir=input_dir,
        output_dir=output_dir,
        xslt_paths=XsltPaths.from_repo_root(settings.repo_root),
        baseuri=baseuri,
        idsource=idsource,
        preprocess=not no_preprocess,
        timeout_per_record=timeout,
    )
    summary = convert_corpus(options=options)
    typer.echo(
        f"marc-to-bibframe: total={summary.total} "
        f"converted={summary.converted} failed={summary.failed}",
        err=True,
    )
    if summary.failed > 0:
        raise typer.Exit(code=1)


@app.command("bibframe-to-bffi")
def bibframe_to_bffi_command() -> None:
    """BIBFRAME → BFFI canonical Turtle (BFFI-only emit).

    Applies the BIBFRAME-to-BFFI SPARQL CONSTRUCTs in `sparql/`,
    materialising every routing decision per `docs/bf_to_bffi_mapping.md`.
    Zero `bf:*` URIs appear in the output (p-56 hard-cut transition).
    """
    raise NotImplementedError("bibframe-to-bffi stage scaffolded; not yet implemented")


@app.command("bffi-to-marc")
def bffi_to_marc_command() -> None:
    """BFFI graph → reconstructed MARCXML (reverse direction).

    Reads BFFI predicates only (no `bf:*` typing as routing key; no
    `bffi-prov:` pipeline-internal provenance as a content source — see the
    cardinal rule in `docs/bffi_limitations.md`) and reconstructs MARCXML
    record-by-record. Used for round-trip verification and downstream MARC
    consumers.
    """
    raise NotImplementedError("bffi-to-marc stage scaffolded; not yet implemented")


@app.command("roundtrip-eval")
def roundtrip_eval_command() -> None:
    """Diff source MARCXML vs reconstructed MARCXML; emit cataloguer-review HTML.

    Walks both directories, pairs records by Helmet bib ID, and produces:
      - per-record diff classification (`identical` / `changed` / `lost` /
        `tag-changed` / `marckey-bypass`),
      - aggregate counts for the observability dashboard,
      - a cataloguer-review HTML with the full residue.
    """
    raise NotImplementedError("roundtrip-eval stage scaffolded; not yet implemented")


@app.command("serve-metrics")
def serve_metrics_command() -> None:
    """Tail `runs/*/stage-events.jsonl` sidecars; serve Prometheus metrics on :9100.

    See `docs/observability.md` for the architecture (sidecar → exporter →
    Prometheus → Grafana → Caddy at `http://localhost:8080`).
    """
    raise NotImplementedError("serve-metrics scaffolded; not yet implemented")


if __name__ == "__main__":
    app()
