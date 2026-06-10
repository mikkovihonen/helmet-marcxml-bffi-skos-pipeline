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
from bffi_pipeline.diagnostic.mapping_coverage import (
    DEFAULT_MAX_HOPS,
    analyze_mapping_coverage,
    format_path,
)
from bffi_pipeline.diagnostic.mapping_tables import regenerate_mapping_tables
from bffi_pipeline.diagnostic.marc_mapping import regenerate_marc_mapping
from bffi_pipeline.rdf_utils import local_name
from bffi_pipeline.runs import (
    InvalidRunDirError,
    mint_run_dir,
    validate_under_run_dir,
)
from bffi_pipeline.stages.bffi_to_marc.runner import (
    ConversionOptions as BffiToMarcOptions,
)
from bffi_pipeline.stages.bffi_to_marc.runner import (
    convert_corpus as bffi_to_marc_convert_corpus,
)
from bffi_pipeline.stages.bibframe_to_bffi.runner import (
    ConversionOptions as BibframeToBffiOptions,
)
from bffi_pipeline.stages.bibframe_to_bffi.runner import (
    convert_corpus as bibframe_to_bffi_convert_corpus,
)
from bffi_pipeline.stages.marc_to_bibframe.runner import (
    ConversionOptions,
    convert_corpus,
)
from bffi_pipeline.stages.marc_to_bibframe.xslt import XsltPaths
from bffi_pipeline.stages.roundtrip_eval.runner import EvalOptions, run_eval


def _require_run_dir(path: Path, *, option_label: str) -> None:
    """Enforce the run-dir convention on a stage's output path.

    Operators mint a fresh run via ``bffi-pipeline new-run`` and pass
    a path inside that directory to each stage's ``--output-dir`` (or
    ``--html``) option. Any other path errors out before the stage
    starts work.
    """
    try:
        validate_under_run_dir(path)
    except InvalidRunDirError as exc:
        typer.echo(f"error: {option_label}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


app = typer.Typer(
    name="bffi-pipeline",
    no_args_is_help=True,
    help="MARCXML ↔ BIBFRAME ↔ BFFI conversion pipeline (rewrite branch).",
)


@app.command("diagnose-mappings")
def diagnose_mappings_command(
    max_hops: Annotated[
        int,
        typer.Option(
            "--max-hops",
            help="BFS depth bound when searching for bffi:* equivalents.",
            min=1,
            max=10,
        ),
    ] = DEFAULT_MAX_HOPS,
    show: Annotated[
        str,
        typer.Option(
            "--show",
            help=(
                "Which buckets to print in full: 'summary', 'unreachable', "
                "'indirect', 'routed', 'all'. Default lists summary + "
                "unreachable only."
            ),
        ),
    ] = "default",
) -> None:
    """Cross-map every BIBFRAME-declared bf:* term against `lkd.rdf`.

    Walks the combined edge set across `vocab/bibframe.rdf` and
    `vocab/lkd.rdf` (owl:equivalentClass/Property, rdfs:subClassOf/
    subPropertyOf in both, owl:sameAs, bffi-meta:*Match) via bounded
    BFS, then categorises each bf:* URI as:

      - direct      a 1-hop owl:equivalentClass/Property to a bffi:* URI.
      - indirect    2+ hops via taxonomy / semantic-shift links.
      - unreachable no bffi:* term reached within --max-hops.

    Run periodically; the unreachable count is the "true gap" backlog —
    it should only shrink (new BFFI versions adding terms) or grow
    visibly (new BIBFRAME versions adding terms BFFI hasn't mapped).
    """
    report = analyze_mapping_coverage(max_hops=max_hops)
    typer.echo(report.summary_text(), err=True)
    typer.echo("", err=True)

    if show in {"all", "indirect"}:
        typer.echo("=== indirect ===", err=False)
        for reach in report.indirect:
            best = reach.best
            assert best is not None
            bffi_uri, path = best
            typer.echo(
                f"  bf:{local_name(reach.bf_term):38s} "
                f"{format_path(path)}  bffi:{local_name(bffi_uri)}"
            )
        typer.echo("")

    if show in {"all", "routed"}:
        typer.echo("=== routed (handled by routing code) ===", err=False)
        for uri in report.routed:
            typer.echo(f"  bf:{local_name(uri)}")
        typer.echo("")

    if show in {"default", "unreachable", "all"}:
        typer.echo("=== unreachable (true GAPs) ===", err=False)
        for uri in report.unreachable:
            typer.echo(f"  bf:{local_name(uri)}")


@app.command("regenerate-mapping-tables")
def regenerate_mapping_tables_command(
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help=(
                "Don't write the doc — exit 1 if the on-disk tables differ "
                "from what the generator would emit. Use in CI / pre-commit."
            ),
        ),
    ] = False,
) -> None:
    """Regenerate the Classes + Predicates tables in `docs/bf_to_bffi_mapping.md`.

    The tables are derived from `vocab/bibframe.rdf` + `vocab/lkd.rdf` +
    the routing registry in `src/bffi_pipeline/stages/bibframe_to_bffi/
    routings.py`. Re-run after any of:

      - a BIBFRAME ontology refresh (`vocab/bibframe.rdf` updated)
      - a `lkd.rdf` refresh (new BFFI version)
      - a new routing function in `routings.py`

    With `--check` the command behaves as a CI guard: it computes the
    expected doc text but writes nothing, exiting non-zero on drift.
    """
    _, changed = regenerate_mapping_tables(check=check)
    if check:
        if changed:
            typer.echo(
                "docs/bf_to_bffi_mapping.md is out of date — "
                "run `bffi-pipeline regenerate-mapping-tables` to refresh.",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo("docs/bf_to_bffi_mapping.md is up to date.")
    else:
        verb = "updated" if changed else "already up to date"
        typer.echo(f"docs/bf_to_bffi_mapping.md {verb}.")


@app.command("regenerate-marc-mapping")
def regenerate_marc_mapping_command(
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help=(
                "Don't write the doc — exit 1 if the on-disk tables differ "
                "from what the generator would emit. Use in CI / pre-commit."
            ),
        ),
    ] = False,
) -> None:
    """Regenerate the BFFI → MARC mapping tables in `docs/bffi_to_marc_mapping.md`.

    The tables are derived from `@marc_emit`-decorated extract functions
    in the reverse-converter source. Re-run after adding or modifying a
    MARC field family.

    With `--check` the command behaves as a CI guard: it computes the
    expected doc text but writes nothing, exiting non-zero on drift.
    """
    _, changed = regenerate_marc_mapping(check=check)
    if check:
        if changed:
            typer.echo(
                "docs/bffi_to_marc_mapping.md is out of date — "
                "run `bffi-pipeline regenerate-marc-mapping` to refresh.",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo("docs/bffi_to_marc_mapping.md is up to date.")
    else:
        verb = "updated" if changed else "already up to date"
        typer.echo(f"docs/bffi_to_marc_mapping.md {verb}.")


@app.command("new-run")
def new_run_command() -> None:
    """Mint a fresh canonical run directory under `runs/` and print its path.

    The directory name follows ``yyyymmdd-hhmm-<6hex>`` (UTC timestamp +
    6 random hex chars). Capture the printed path and feed it (or a
    sub-path under it) to each stage's ``--output-dir`` option so the
    convention is enforced consistently across the pipeline.

    Example:

        $ RUN=$(bffi-pipeline new-run)
        $ bffi-pipeline marc-to-bibframe \\
              --input-dir <marc> --output-dir $RUN/bibframe
        $ bffi-pipeline bibframe-to-bffi \\
              --input-dir $RUN/bibframe --output-dir $RUN/bffi
        $ ...
    """
    run_dir = mint_run_dir()
    typer.echo(str(run_dir))


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
    _require_run_dir(output_dir, option_label="--output-dir")
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
def bibframe_to_bffi_command(
    input_dir: Annotated[
        Path,
        typer.Option(
            "--input-dir",
            help="Directory of per-record BIBFRAME RDF/XML files (`*.bibframe.xml`).",
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
            help="Where to write per-record BFFI Turtle (`<stem>.bffi.ttl`).",
        ),
    ],
) -> None:
    """BIBFRAME → BFFI canonical Turtle (BFFI-only emit).

    Step 3 v0 — implements p-56 Phase 1 only (clean rename via every
    `owl:equivalentClass` / `owl:equivalentProperty` row from
    `vocab/lkd.rdf`). No discriminator routings yet — Hub /
    Identifier-scheme / Title-variant / Series-link / Audio / Music
    land in step 6 / 7. Per-record `bf:*` residue (terms with no
    Phase 1 rename) is counted in the summary so step 6 can target the
    surviving terms.
    """
    _require_run_dir(output_dir, option_label="--output-dir")
    options = BibframeToBffiOptions(input_dir=input_dir, output_dir=output_dir)
    summary = bibframe_to_bffi_convert_corpus(options=options)
    routings = " ".join(
        f"{name}={count}" for name, count in sorted(summary.routing_counters.items())
    )
    typer.echo(
        f"bibframe-to-bffi: total={summary.total} "
        f"converted={summary.converted} failed={summary.failed} "
        f"closed_namespace_residue={summary.closed_namespace_residue} | "
        f"routings: {routings}",
        err=True,
    )
    if summary.failed > 0:
        raise typer.Exit(code=1)


@app.command("bffi-to-marc")
def bffi_to_marc_command(
    input_dir: Annotated[
        Path,
        typer.Option(
            "--input-dir",
            help="Directory of per-record BFFI Turtle files (`*.bffi.ttl`).",
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
            help="Where to write per-record reconstructed MARCXML (`<stem>.marcxml`).",
        ),
    ],
) -> None:
    """BFFI graph → reconstructed MARCXML (reverse direction).

    Reads BFFI predicates only (no `bf:*` typing as routing key; no
    `bffi-prov:` pipeline-internal provenance as a content source) and
    reconstructs MARCXML record-by-record. Step 4 v0 emit covers the bare
    minimum (leader placeholder, 001 = Helmet bib ID, 245 $a = main title);
    subsequent commits add field families one at a time so the diff harness
    gives a clean per-family verification signal.
    """
    _require_run_dir(output_dir, option_label="--output-dir")
    options = BffiToMarcOptions(input_dir=input_dir, output_dir=output_dir)
    summary = bffi_to_marc_convert_corpus(options=options)
    typer.echo(
        f"bffi-to-marc: total={summary.total} "
        f"converted={summary.converted} failed={summary.failed} "
        f"no_manifestation={summary.no_manifestation}",
        err=True,
    )
    if summary.failed > 0:
        raise typer.Exit(code=1)


@app.command("roundtrip-eval")
def roundtrip_eval_command(
    source_dir: Annotated[
        Path,
        typer.Option(
            "--source-dir",
            help="Directory of original (source-of-truth) MARCXML files (`*.xml`).",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ],
    reconstructed_dir: Annotated[
        Path,
        typer.Option(
            "--reconstructed-dir",
            help="Directory of reconstructed MARCXML files (`*.marcxml`, from `bffi-to-marc`).",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ],
    html_path: Annotated[
        Path | None,
        typer.Option(
            "--html",
            help="Where to write the cataloguer-review HTML report. Omit to skip the render.",
        ),
    ] = None,
) -> None:
    """Diff source MARCXML vs reconstructed MARCXML; emit cataloguer-review HTML.

    Walks both directories, pairs records by ``controlfield 001`` (Helmet
    bib ID), and produces per-record diff classification (``identical`` /
    ``changed`` / ``lost`` / ``added``), corpus aggregate counts, and an
    optional cataloguer-review HTML with the full residue.

    Tag-changed and marcKey-bypass classifications are deferred to a
    follow-on commit — for v0 they show up as paired ``lost`` + ``added``
    rows the operator reads alongside each other.
    """
    if html_path is not None:
        _require_run_dir(html_path, option_label="--html")
    options = EvalOptions(
        source_dir=source_dir,
        reconstructed_dir=reconstructed_dir,
        html_path=html_path,
    )
    summary = run_eval(options=options)
    dist = " ".join(f"{status}={count}" for status, count in sorted(summary.distribution.items()))
    typer.echo(
        f"roundtrip-eval: pairs={summary.total_pairs} diffed={summary.diffed} "
        f"failed={summary.failed} source_only={summary.source_only} "
        f"reconstructed_only={summary.reconstructed_only} | distribution: {dist}",
        err=True,
    )
    if summary.failed > 0:
        raise typer.Exit(code=1)


@app.command("serve-metrics")
def serve_metrics_command() -> None:
    """Tail `runs/*/stage-events.jsonl` sidecars; serve Prometheus metrics on :9100.

    See `docs/observability.md` for the architecture (sidecar → exporter →
    Prometheus → Grafana → Caddy at `http://localhost:8080`).
    """
    raise NotImplementedError("serve-metrics scaffolded; not yet implemented")


if __name__ == "__main__":
    app()
