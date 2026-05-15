"""M2 per-record sidecar emitters: ``helmet-map.jsonl``,
``bibframe/_errors.jsonl`` + ``_errors.tsv``, and the unified
cataloguer-handoff source-review TSV (P-31 Phase B).

Four shapes the per-record loop in ``runner.run()`` appends to:

- ``helmet-map.jsonl`` — one row per converted record; ``run()`` calls
  :func:`_append_jsonl` and :func:`_dedupe_helmet_map` at the end so
  re-runs see one row per ``helmet_bib_id``.
- ``bibframe/_errors.jsonl`` — one row per failed record.
- ``bibframe/_errors.tsv`` — cataloguer-facing TSV companion to the
  errors JSONL (three columns: bib_id / error_type / sanitised message).
- ``cataloguer-source-review-<run_uuid>.tsv`` — unified TSV the M2
  errors mirror into via :func:`_append_source_review_m2`.

P-38 Phase D: extracted from m2/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from bffi_pipeline.cataloguer_review import append_source_row
from bffi_pipeline.stages.m2.schemas import ConversionErrorRow


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _append_source_review_m2(row: ConversionErrorRow) -> None:
    """Mirror a ``ConversionErrorRow`` into the unified source-review TSV.

    Pairs with each ``_append_jsonl(errors_path, …)`` call so the
    cataloguer-handoff superset stays in lock-step with the per-stage
    `_errors.jsonl`. P-31 Phase B wire-in for M2.
    """
    append_source_row(
        bib_id=row.helmet_bib_id or row.filename,
        stage="m2",
        severity="blocking",
        details=row.message,
    )


#: Truncate over-long error messages in the TSV so a spreadsheet
#: stays readable. XSD validation errors carry the offending value
#: and a regex pattern — easily 400+ chars. The full message lives
#: in the JSONL for forensic lookup; the TSV is for triage.
_ERRORS_TSV_MESSAGE_MAX: Final[int] = 240


def _emit_errors_tsv(path: Path, errors: list[ConversionErrorRow]) -> None:
    """Cataloguer-facing TSV companion to ``bibframe/_errors.jsonl``.

    Three columns the cataloguer can open in Excel / Sheets / Numbers
    and act on without parsing JSON:

    - ``helmet_bib_id`` — derived from the source filename's stem so
      it's populated even when the XSD parse failed before we could
      extract the 001 control field (the JSONL leaves
      ``helmet_bib_id=null`` in that case).
    - ``error_type`` — one of ``marcxml-xsd-validation``,
      ``marcxml-content-minimum``, ``bibframe-shape``,
      ``bibframe-conversion``. Filterable.
    - ``message`` — single-line, tab + newline + control char
      sanitised; truncated to keep the spreadsheet readable.

    Always emitted — even on a clean run a header-only TSV is
    written. Workflows wired to the artifact path don't need a
    missing-file guard.

    Sorted by (``helmet_bib_id``, ``error_type``) for stable diffs
    across re-runs. Atomic write via ``.tmp`` + ``replace``.
    """
    header = "helmet_bib_id\terror_type\tmessage\n"
    rows: list[tuple[str, str, str]] = []
    for row in errors:
        bib_id = row.helmet_bib_id or Path(row.filename).stem
        message_clean = " ".join(row.message.replace("\t", " ").split())
        if len(message_clean) > _ERRORS_TSV_MESSAGE_MAX:
            message_clean = message_clean[: _ERRORS_TSV_MESSAGE_MAX - 1] + "…"
        rows.append((bib_id, row.error_type, message_clean))
    rows.sort()
    body = "".join(f"{bib_id}\t{etype}\t{msg}\n" for bib_id, etype, msg in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _dedupe_helmet_map(path: Path) -> None:
    """Last-write-wins dedup on ``helmet_bib_id``. Rewrites atomically."""
    if not path.exists():
        return
    seen: dict[str, dict[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        seen[row["helmet_bib_id"]] = row
    rewritten = "\n".join(json.dumps(row, ensure_ascii=False) for row in seen.values()) + "\n"
    _atomic_write_bytes(path, rewritten.encode("utf-8"))
