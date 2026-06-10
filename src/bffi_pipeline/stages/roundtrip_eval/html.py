"""Cataloguer-review HTML output for the round-trip diff.

Renders a single self-contained HTML file with:

  - corpus-wide diff distribution (one row per status),
  - per-record summary table (clickable into per-record detail panels),
  - per-record diff detail (every field with status badge + content).

No external assets, no JS. Style is inline so the report can be served
by Caddy's ``/files/`` mount and viewed offline.
"""

from __future__ import annotations

import html
from collections import Counter
from collections.abc import Iterable

from bffi_pipeline.stages.roundtrip_eval.diff import DiffStatus, FieldDiff, RecordDiff

_STATUS_ORDER: tuple[DiffStatus, ...] = ("identical", "changed", "lost", "added")

_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 1em; }
h1 { margin-bottom: 0.2em; }
table { border-collapse: collapse; margin: 0.5em 0 1em 0; }
th, td { border: 1px solid #ccc; padding: 4px 8px; text-align: left; }
th { background: #f4f4f4; }
.identical { background: #d4edda; }
.changed   { background: #fff3cd; }
.lost      { background: #f8d7da; }
.added     { background: #cce5ff; }
.tag { font-family: ui-monospace, monospace; }
.field-detail { font-family: ui-monospace, monospace; font-size: 0.9em; white-space: pre-wrap; }
details { margin: 0.5em 0; }
summary { cursor: pointer; }
"""


def _escape(text: str | None) -> str:
    return html.escape(text or "")


def _render_summary_table(corpus_counts: Counter[DiffStatus]) -> str:
    rows: list[str] = []
    for status in _STATUS_ORDER:
        count = corpus_counts.get(status, 0)
        rows.append(f'<tr><td class="{status}">{status}</td><td>{count}</td></tr>')
    return "<table><tr><th>status</th><th>count</th></tr>" + "".join(rows) + "</table>"


def _render_per_record_overview(records: list[RecordDiff]) -> str:
    headers = (
        "<tr><th>bib ID</th>"
        + "".join(f'<th class="{s}">{s}</th>' for s in _STATUS_ORDER)
        + "</tr>"
    )
    rows: list[str] = []
    for rec in records:
        counts = rec.status_counts
        cells = "".join(f"<td>{counts.get(status, 0)}</td>" for status in _STATUS_ORDER)
        rows.append(
            f'<tr><td class="tag"><a href="#bib-{_escape(rec.bib_id)}">'
            f"{_escape(rec.bib_id)}</a></td>{cells}</tr>"
        )
    return "<table>" + headers + "".join(rows) + "</table>"


def _render_field_row(diff: FieldDiff) -> str:
    source = _escape(diff.source.display()) if diff.source is not None else ""
    recon = _escape(diff.reconstructed.display()) if diff.reconstructed is not None else ""
    return (
        f'<tr class="{diff.status}">'
        f'<td class="tag">{_escape(diff.tag)}</td>'
        f"<td>{diff.status}</td>"
        f'<td class="field-detail">{source}</td>'
        f'<td class="field-detail">{recon}</td>'
        "</tr>"
    )


def _render_per_record_detail(record: RecordDiff) -> str:
    counts = record.status_counts
    summary_text = " · ".join(f"{status}: {counts.get(status, 0)}" for status in _STATUS_ORDER)
    header = "<tr><th>tag</th><th>status</th><th>source</th><th>reconstructed</th></tr>"
    body = "".join(_render_field_row(d) for d in record.fields)
    return (
        f'<details id="bib-{_escape(record.bib_id)}">'
        f"<summary><strong>{_escape(record.bib_id)}</strong> — {summary_text}</summary>"
        f"<table>{header}{body}</table>"
        "</details>"
    )


def render(
    records: Iterable[RecordDiff],
    *,
    title: str = "BFFI Round-Trip Diff",
) -> str:
    """Render the full cataloguer-review HTML document.

    Caller passes the full ordered list of :class:`RecordDiff` instances
    (one per source/reconstructed pair). The corpus-wide distribution is
    derived from the records themselves so the report stays internally
    consistent.
    """
    records_list = list(records)
    corpus_counts: Counter[DiffStatus] = Counter()
    for rec in records_list:
        for status, count in rec.status_counts.items():
            corpus_counts[status] += count

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        f"<head><meta charset='utf-8'><title>{_escape(title)}</title>",
        f"<style>{_STYLE}</style></head>",
        "<body>",
        f"<h1>{_escape(title)}</h1>",
        f"<p>{len(records_list)} record(s) compared.</p>",
        "<h2>Corpus distribution</h2>",
        _render_summary_table(corpus_counts),
        "<h2>Per-record overview</h2>",
        _render_per_record_overview(records_list),
        "<h2>Per-record detail</h2>",
        *(_render_per_record_detail(r) for r in records_list),
        "</body></html>",
    ]
    return "\n".join(parts)
