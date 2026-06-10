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

import typer

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
def marc_to_bibframe_command() -> None:
    """MARCXML → BIBFRAME via the LoC marc2bibframe2 XSLT.

    Reads MARCXML records from the configured input directory and runs them
    through the marc2bibframe2 XSLT (vendored in `third_party/`), emitting
    BIBFRAME RDF/XML per record.
    """
    raise NotImplementedError("marc-to-bibframe stage scaffolded; not yet implemented")


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
