"""P-41 Phase C.3 — retrospective regen of the per-run
export-synthesis TSV from ``data/provenance.ttl``.

The per-run TSV (:mod:`bffi_pipeline.export_synthesis`) is the live
artefact a pipeline run writes. This module reads the per-record
provenance graphs the M2 conversion emits and rebuilds the same TSV
deterministically — useful when an operator deletes the TSV but keeps
the BIBFRAME RDF, or wants to re-derive the TSV from a partial run
the dashboard can't fully recover from. The regen output matches the
live TSV byte-identically (modulo row ordering, since SPARQL ORDER BY
re-orders by bib_id+tier whereas the live append happens in
M2's per-record loop order).

The SPARQL query lives at
``sparql/queries/export_synthesis_report.rq`` per ``CLAUDE.md`` §
"Conventions: SPARQL". Every column the live TSV writes is read from
the Synthesis Activity's ``bffi-prov:synthetic*`` predicates —
``syntheticValue`` and ``syntheticMarcSource`` carry the two fields
that would otherwise need graph traversal or method-tag heuristics.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Final

from rdflib import Graph

#: SPARQL query path relative to the repo root.
_QUERY_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "sparql" / "queries" / "export_synthesis_report.rq"
)

#: TSV header — must match :mod:`bffi_pipeline.export_synthesis` exactly.
_HEADER: Final[tuple[str, ...]] = (
    "bib_id",
    "field",
    "marc_source",
    "synthesised_value",
    "tier",
    "method",
    "confidence",
    "activity_uri",
)


def _load_provenance(data_dir: Path) -> Graph:
    """Load every per-record BIBFRAME RDF/XML and the aggregated
    ``provenance.ttl`` (if present) into a single graph. M2 writes
    per-record graphs at ``<data_dir>/bibframe/<bib_id>.rdf``; a
    future plan that consolidates provenance into one Turtle file can
    simplify this to one parse call.
    """
    g = Graph()
    bibframe_dir = data_dir / "bibframe"
    if bibframe_dir.is_dir():
        for path in sorted(bibframe_dir.glob("*.rdf")):
            g.parse(str(path), format="xml")
    aggregated = data_dir / "provenance.ttl"
    if aggregated.is_file():
        g.parse(str(aggregated), format="turtle")
    return g


def regen_export_synthesis_report(
    *,
    data_dir: Path,
    output_path: Path,
) -> int:
    """Rebuild the per-run export-synthesis TSV from the provenance
    graphs under ``data_dir``. Writes ``output_path`` atomically (via
    a ``.tmp`` then rename); returns the row count (excluding header).

    The output is **idempotent** — running this twice on the same
    provenance state overwrites the target file with the same content.
    Row order is bib_id asc + tier asc (per the SPARQL ORDER BY).
    """
    query_text = _QUERY_PATH.read_text(encoding="utf-8")
    graph = _load_provenance(data_dir)
    results = graph.query(query_text)

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    row_count = 0
    with tmp_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(_HEADER)
        for row in results:
            bib_id = str(row.bib_id)  # type: ignore[union-attr]
            field = str(row.field)  # type: ignore[union-attr]
            marc_source = str(row.marc_source)  # type: ignore[union-attr]
            synthesised_value = str(row.value)  # type: ignore[union-attr]
            tier = str(row.tier)  # type: ignore[union-attr]
            method = str(row.method)  # type: ignore[union-attr]
            confidence = float(str(row.confidence))  # type: ignore[union-attr]
            activity = str(row.activity)  # type: ignore[union-attr]
            writer.writerow(
                [
                    bib_id,
                    field,
                    marc_source,
                    synthesised_value,
                    tier,
                    method,
                    f"{confidence:.4f}",
                    activity,
                ]
            )
            row_count += 1

    tmp_path.replace(output_path)
    return row_count


__all__ = ["regen_export_synthesis_report"]
