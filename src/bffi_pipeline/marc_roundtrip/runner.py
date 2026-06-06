"""Per-record orchestration for the MARC round-trip review.

Walks every ``bffi:Manifestation`` in the canonical (Skosified) graph,
reconstructs a MARCXML record from each, locates the original MARCXML
from Helmet's source dir, diffs the two, and writes:

    <output_dir>/reconstructed/<bib_id>.xml
    <output_dir>/diffs/<bib_id>.json
    <output_dir>/summary.json

The HTML reviewer in P-47 commit 4 reads from this layout. The
:func:`run` function is the operator entry point and is dispatched by
the pipeline runner as a stage between ``load`` and
``cataloguer-bundle``.

Originals are looked up by ``<marcxml_dir>/<bib_id>.xml``. A missing
original is treated as a soft warning and the bib's diff carries an
``error`` field so the HTML can render an inline "no source MARC found"
note instead of failing the run.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph, URIRef

from bffi_pipeline.marc_roundtrip.converter import (
    MARC_NAMESPACE,
    ReconstructedRecord,
    reconstruct_marc,
    serialize_marc,
)
from bffi_pipeline.marc_roundtrip.diff import RecordDiff, diff_records
from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.provenance import vocab as V


@dataclass
class RoundtripSummary:
    """Aggregate stats for the run. Serialised to ``summary.json``."""

    total_manifestations: int = 0
    reconstructed: int = 0
    diffed: int = 0
    missing_original: int = 0
    #: Per-status totals summed across all bibs ({"identical": N, ...}).
    per_status: dict[str, int] = field(default_factory=dict)
    #: Bib IDs that had no source MARCXML to compare against.
    missing_bib_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "total_manifestations": self.total_manifestations,
            "reconstructed": self.reconstructed,
            "diffed": self.diffed,
            "missing_original": self.missing_original,
            "per_status": dict(self.per_status),
            "missing_bib_ids": list(self.missing_bib_ids),
        }


def run(
    *,
    canonical_path: Path,
    marcxml_dir: Path,
    output_dir: Path,
    finto_dump_dir: Path | None = None,
) -> RoundtripSummary:
    """Reconstruct + diff every Manifestation. Returns the summary
    that ``summary.json`` is built from.

    ``finto_dump_dir`` (default = the project's ``finto-dumps/``) is
    merged into the same graph as the canonical so the converter's
    cross-vocab label lookups work — relator URIs resolve to ``$e``
    relator terms on 700, media/carrier URIs resolve to ``$a`` labels
    on 337/338, etc. Pass an empty/non-existent path to skip the merge."""
    canonical_path = Path(canonical_path)
    marcxml_dir = Path(marcxml_dir)
    output_dir = Path(output_dir)

    if not canonical_path.is_file():
        raise FileNotFoundError(
            f"Canonical Turtle not found at {canonical_path!s}. Run skosify first."
        )

    reconstructed_dir = output_dir / "reconstructed"
    diffs_dir = output_dir / "diffs"
    reconstructed_dir.mkdir(parents=True, exist_ok=True)
    diffs_dir.mkdir(parents=True, exist_ok=True)

    emit_if_active(
        stage="marc-roundtrip",
        event="start",
        extra={"input": str(canonical_path)},
    )

    graph = Graph()
    graph.parse(str(canonical_path), format="turtle")
    _merge_vocab_dumps(graph, finto_dump_dir)

    manifestation_uris = [
        s for s in graph.subjects(V.RDF.type, V.BFFI.Manifestation) if isinstance(s, URIRef)
    ]
    summary = RoundtripSummary(total_manifestations=len(manifestation_uris))
    emit_if_active(
        stage="marc-roundtrip",
        event="phase_boundary",
        phase="diff",
        counters={"total": len(manifestation_uris)},
    )

    for processed, manif in enumerate(sorted(manifestation_uris, key=str), start=1):
        record = reconstruct_marc(graph, manif)
        if not record.bib_id:
            continue
        summary.reconstructed += 1
        recon_path = reconstructed_dir / f"{record.bib_id}.xml"
        recon_path.write_bytes(serialize_marc(record))

        original_path = marcxml_dir / f"{record.bib_id}.xml"
        diff = _diff_one(record, original_path)
        if diff is None:
            summary.missing_original += 1
            summary.missing_bib_ids.append(record.bib_id)
            # Write a stub diff JSON so the HTML can render the
            # "no source MARC" note in a uniform shape.
            stub = {
                "bib_id": record.bib_id,
                "error": "original_marcxml_not_found",
                "marcxml_path": str(original_path),
            }
            (diffs_dir / f"{record.bib_id}.json").write_text(
                json.dumps(stub, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            continue

        summary.diffed += 1
        for status, count in diff.summary.items():
            summary.per_status[status] = summary.per_status.get(status, 0) + count

        (diffs_dir / f"{record.bib_id}.json").write_text(
            json.dumps(diff.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if processed % 100 == 0 or processed == len(manifestation_uris):
            emit_if_active(
                stage="marc-roundtrip",
                event="progress",
                counters={"processed": processed, "total": len(manifestation_uris)},
            )

    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    emit_if_active(
        stage="marc-roundtrip",
        event="end",
        counters={
            "reconstructed": summary.reconstructed,
            "diffed": summary.diffed,
            "missing_original": summary.missing_original,
        },
    )
    return summary


#: Vocab dumps the converter benefits from cross-resolving (label
#: lookups for relator URIs, media/carrier URIs, etc.). Loading more
#: than this is wasteful — we don't query the YSO graph for anything
#: at round-trip time. Keep the list narrow so the merge stays fast.
_ROUNDTRIP_VOCAB_FILES: tuple[str, ...] = (
    "relators-skos.ttl",
    "rda-media-skos.ttl",
    "rda-carrier-skos.ttl",
    "mencformat-skos.ttl",
    "mrecmedium-skos.ttl",
    "mplayspeed-skos.ttl",
    "mplayback-skos.ttl",
    "mcapturestorage-skos.ttl",
    "mcolor-skos.ttl",
)


def _merge_vocab_dumps(graph: Graph, finto_dump_dir: Path | None) -> None:
    """Parse the small Finto / LoC vocab dumps used for cross-vocab
    label lookups into ``graph``. No-op when the dir is absent or the
    dumps haven't been fetched yet — the converter degrades gracefully
    (URIs render in $4 codes / no $a / no $e)."""
    if finto_dump_dir is None:
        # Default to the project's finto-dumps/ — the load-finto cache.
        finto_dump_dir = Path(__file__).resolve().parents[3] / "finto-dumps"
    finto_dump_dir = Path(finto_dump_dir)
    if not finto_dump_dir.is_dir():
        return
    for name in _ROUNDTRIP_VOCAB_FILES:
        path = finto_dump_dir / name
        if path.is_file():
            try:
                graph.parse(str(path), format="turtle")
            except Exception as exc:
                print(f"[marc-roundtrip] WARN: could not parse {path}: {exc}")


def _diff_one(record: ReconstructedRecord, original_path: Path) -> RecordDiff | None:
    """Parse the original MARCXML + diff against the reconstructed
    record. Returns ``None`` when the original is missing or unreadable."""
    if not original_path.is_file():
        return None
    try:
        original_root = ET.parse(str(original_path)).getroot()
    except ET.ParseError:
        return None
    # The Helmet MARCXML files have a single ``<record>`` directly, or
    # are wrapped in a ``<collection>`` — handle both.
    if original_root.tag == f"{{{MARC_NAMESPACE}}}record":
        original_record = original_root
    else:
        found = original_root.find(f"{{{MARC_NAMESPACE}}}record")
        if found is None:
            return None
        original_record = found
    return diff_records(
        bib_id=record.bib_id,
        original=original_record,
        reconstructed=record.element,
        skipped_tags=record.skipped_tags,
    )


__all__ = [
    "RoundtripSummary",
    "run",
]
