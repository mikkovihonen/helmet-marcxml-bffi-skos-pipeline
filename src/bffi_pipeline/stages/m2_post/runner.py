"""Per-record M2-post driver.

Walks every ``<bibframe_dir>/<bib_id>.rdf`` file, loads the matching
source MARCXML, runs the correlator, and writes back the enriched
BIBFRAME RDF with the new ``bffi-prov:fromMarcField`` triples
attached to raw entities.

Outputs:

- BIBFRAME ``.rdf`` files mutated in place (atomic .tmp + rename).
- ``<output_dir>/m2-post-audit.jsonl`` — one row per marcKey-bearing
  entity with the resulting correlation. Consumed by the
  cataloguer-bundle stage so the audit shows alongside the salvage /
  contrib / picker tabs.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph

from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.stages.m2_post.correlator import CorrelationResult, correlate
from bffi_pipeline.stages.m2_post.token import MarcFieldIndex

#: Audit log filename. Lives at ``<output_dir>/m2-post-audit.jsonl``
#: alongside the other per-stage cascade audit logs.
AUDIT_FILENAME: str = "m2-post-audit.jsonl"


@dataclass
class M2PostSummary:
    """Aggregate stats for the M2-post pass. Returned by :func:`run`."""

    total_records: int = 0
    correlated_records: int = 0
    marckey_entities_seen: int = 0
    marckey_entities_matched: int = 0
    marckey_entities_unmatched: int = 0
    #: Bib IDs whose source MARCXML couldn't be located (or failed to
    #: parse). Logged as warnings; downstream stages still run.
    missing_marcxml_bib_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "total_records": self.total_records,
            "correlated_records": self.correlated_records,
            "marckey_entities_seen": self.marckey_entities_seen,
            "marckey_entities_matched": self.marckey_entities_matched,
            "marckey_entities_unmatched": self.marckey_entities_unmatched,
            "missing_marcxml_bib_ids": list(self.missing_marcxml_bib_ids),
        }


def run(
    input_dir: Path,
    *,
    bibframe_dir: Path | None = None,
    output_dir: Path | None = None,
    force: bool = False,
) -> M2PostSummary:
    """Enrich every BIBFRAME RDF file under ``bibframe_dir`` with
    ``bffi-prov:fromMarcField`` triples computed from the matching
    source MARCXML in ``input_dir``.

    ``force`` reserved for parity with other stages; M2-post is
    idempotent (rdflib's ``add`` is set-semantics), so re-running
    against an already-enriched file is a no-op. The flag does still
    truncate the audit log so a re-run produces a coherent file.
    """
    base = output_dir or get_settings().data_dir
    bibframe_dir = bibframe_dir or (base / "bibframe")
    audit_path = base / AUDIT_FILENAME

    if force or audit_path.exists():
        # Reset so each run produces a complete, coherent log. M2-post
        # is fast enough that a fresh write is cheaper than append +
        # later-stage de-duplication.
        audit_path.unlink(missing_ok=True)

    rdf_files = sorted(p for p in bibframe_dir.glob("*.rdf") if p.stem != "_errors")
    summary = M2PostSummary(total_records=len(rdf_files))

    emit_if_active(
        stage="m2-post",
        event="start",
        counters={"total": len(rdf_files)},
    )

    with audit_path.open("w", encoding="utf-8") as audit_fp:
        for processed, rdf_path in enumerate(rdf_files, start=1):
            bib_id = rdf_path.stem
            marcxml_path = input_dir / f"{bib_id}.xml"
            if not marcxml_path.is_file():
                summary.missing_marcxml_bib_ids.append(bib_id)
                continue
            try:
                index = MarcFieldIndex.from_marcxml_path(marcxml_path)
            except Exception as exc:
                summary.missing_marcxml_bib_ids.append(bib_id)
                # Surface as a stage-level health row rather than aborting
                # the entire pass — one malformed source XML shouldn't
                # block the other 499 records' provenance enrichment.
                emit_if_active(
                    stage="m2-post",
                    event="health",
                    extra={
                        "bib_id": bib_id,
                        "reason": "marcxml-parse-failed",
                        "error": str(exc),
                    },
                )
                continue

            graph = Graph()
            graph.parse(str(rdf_path), format="xml")
            results = correlate(graph, index)
            _write_audit(audit_fp, results)
            _update_summary(summary, results)

            _write_atomic(graph, rdf_path)
            summary.correlated_records += 1

            if processed % 100 == 0 or processed == len(rdf_files):
                emit_if_active(
                    stage="m2-post",
                    event="progress",
                    counters={"processed": processed, "total": len(rdf_files)},
                )

    emit_if_active(
        stage="m2-post",
        event="end",
        counters={
            "correlated": summary.correlated_records,
            "matched": summary.marckey_entities_matched,
            "unmatched": summary.marckey_entities_unmatched,
        },
    )
    return summary


def _write_audit(fp: object, results: Iterable[CorrelationResult]) -> None:
    """Append one JSONL row per correlation to the audit log."""
    for r in results:
        row = {
            "bib_id": r.bib_id,
            "entity": r.entity,
            "marc_key": r.marc_key,
            "matched_token": r.matched_token,
            "matched_via": r.matched_via,
        }
        if r.reason is not None:
            row["reason"] = r.reason
        fp.write(json.dumps(row, ensure_ascii=False))  # type: ignore[attr-defined]
        fp.write("\n")  # type: ignore[attr-defined]


def _update_summary(summary: M2PostSummary, results: Iterable[CorrelationResult]) -> None:
    for r in results:
        summary.marckey_entities_seen += 1
        if r.matched_token is not None:
            summary.marckey_entities_matched += 1
        else:
            summary.marckey_entities_unmatched += 1


def _write_atomic(graph: Graph, path: Path) -> None:
    """Serialise ``graph`` to ``path`` via .tmp + rename so partial
    writes never replace a valid file. RDF/XML output keeps M2's
    canonical format intact."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    graph.serialize(destination=str(tmp), format="xml")
    tmp.replace(path)
