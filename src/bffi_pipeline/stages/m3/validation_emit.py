"""M3 SHACL-validation TSV emitter + report extraction.

Boundary-3 SHACL validation runs against every converted record;
failures are recorded in ``bffi/_validation.jsonl`` and surfaced to
cataloguers via the matching ``_validation.tsv`` companion. The TSV
extracts the human-actionable ``sh:message`` text from rdflib's
report serialisation; the full multi-line report stays in the JSONL
for forensic lookup.

P-38 Phase D: extracted from m3/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from bffi_pipeline.stages.m3.schemas import ValidationRow

#: Truncate over-long rendered messages in the validation TSV so a
#: spreadsheet stays readable. The full multi-line report lives in
#: the JSONL for forensic lookup; the TSV is for cataloguer triage.
_VALIDATION_TSV_MESSAGE_MAX: Final[int] = 240

#: Regex to extract every ``sh:message Literal("…")`` clause from
#: rdflib's SHACL report serialization. The message text is the
#: cataloguer-actionable bit; the rest of the report is rdflib
#: boilerplate (severity, source shape, focus node etc.) that's only
#: useful for pipeline-team debugging.
_SH_MESSAGE_RE: Final[re.Pattern[str]] = re.compile(
    r'sh:message\s+Literal\(\s*"([^"\\]*(?:\\.[^"\\]*)*)"', re.DOTALL
)


def _extract_shape_messages(report_text: str) -> str:
    """Pull every ``sh:message Literal("…")`` out of a SHACL report.

    Joined with ``" | "`` when a single record has multiple
    violations. Falls back to the full report (with control chars
    collapsed) when no messages can be extracted — better to surface
    the rdflib boilerplate than an empty cell.
    """
    matches = _SH_MESSAGE_RE.findall(report_text)
    if not matches:
        return " ".join(report_text.replace("\t", " ").split())
    return " | ".join(m.strip() for m in matches)


def _emit_validation_tsv(path: Path, rows: list[ValidationRow]) -> None:
    """Cataloguer-facing TSV companion to ``bffi/_validation.jsonl``.

    Three columns the cataloguer can open in Excel / Sheets / Numbers
    and act on without parsing JSON:

    - ``helmet_bib_id`` — lookup key in Helmet / Sierra.
    - ``shape_message`` — the human-readable ``sh:message`` text
      extracted from the SHACL report (e.g. ``"bffi:Work must have
      skos:prefLabel in fi/sv/en."``). Multiple violations on one
      record are joined with ``" | "``. This is the
      cataloguer-actionable column.
    - ``output_file`` — the BFFI Turtle file the failed shape was
      validated against (e.g. ``b1234.ttl``); pipeline-team
      cross-reference. The full multi-line rdflib SHACL report
      stays in the JSONL companion.

    Messages over 240 chars are truncated with an ellipsis to keep
    spreadsheet rendering readable. Full report stays in JSONL.

    Always emitted — even when every record passed Boundary-3, a
    header-only TSV is written so cataloguer workflows wired to the
    artifact path don't need a missing-file guard.

    Sorted by ``helmet_bib_id`` for stable diffs across re-runs.
    Atomic write via ``.tmp`` + ``replace``.

    Mirrors the M2 ``bibframe/_errors.tsv`` + the M8
    ``canonical-mint-failures.tsv`` conventions so cataloguers see a
    consistent artifact shape across stages.
    """
    header = "helmet_bib_id\tshape_message\toutput_file\n"
    out_rows: list[tuple[str, str, str]] = []
    for row in rows:
        message = _extract_shape_messages(row.report_text)
        message_clean = " ".join(message.replace("\t", " ").split())
        if len(message_clean) > _VALIDATION_TSV_MESSAGE_MAX:
            message_clean = message_clean[: _VALIDATION_TSV_MESSAGE_MAX - 1] + "…"
        out_rows.append((row.helmet_bib_id, message_clean, row.output_file))
    out_rows.sort()
    body = "".join(f"{bib_id}\t{msg}\t{output_file}\n" for bib_id, msg, output_file in out_rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)
