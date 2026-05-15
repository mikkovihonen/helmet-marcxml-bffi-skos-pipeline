"""M8 sidecar emitters — canonical-map + conflicts + mint-failures.

All emitters write atomically (``.tmp`` then rename) so a half-built
sidecar never lands on disk, and all rows are sorted by their natural
key so re-runs against the same inputs produce byte-stable diffs.

Two of the four files are cataloguer-facing TSVs; the canonical-map
+ conflicts + mint-failures JSONLs are the canonical machine-readable
artefacts.

P-38 Phase D: extracted from m8/runner.py. No logic change.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from bffi_pipeline.stages.m8.schemas import (
    CanonicalEntry,
    GroupConflict,
    MintFailure,
)


def _emit_canonical_map(path: Path, entries: Iterable[CanonicalEntry]) -> None:
    rows = sorted(entries, key=lambda e: e.canonical_work_uri)
    payload = "\n".join(
        json.dumps(
            {
                "canonical_work_uri": e.canonical_work_uri,
                "raw_work_uris": list(e.raw_work_uris),
                "helmet_bib_ids": list(e.helmet_bib_ids),
                "merged_at": e.merged_at,
            },
            ensure_ascii=False,
        )
        for e in rows
    )
    if payload:
        payload += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _emit_conflicts(path: Path, conflicts: Iterable[GroupConflict]) -> None:
    rows = sorted(conflicts, key=lambda c: c.members[0] if c.members else "")
    payload = "\n".join(
        json.dumps(
            {
                "members": list(c.members),
                "conflicting_pair": list(c.conflicting_pair),
                "same_work_path": [list(edge) for edge in c.same_work_path],
            },
            ensure_ascii=False,
        )
        for c in rows
    )
    if payload:
        payload += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _emit_mint_failures(path: Path, mint_failures: Iterable[MintFailure]) -> None:
    """Atomic write of the per-record mint-failure JSONL.

    Sorted by ``anchor_work_uri`` for byte-stable diffs across re-runs
    of the same input. Each row includes the title (``pref_label``)
    and Helmet ``bib_id`` list so the JSONL is self-contained for
    debugging — the TSV companion at ``_emit_mint_failures_tsv``
    surfaces the same fields in a cataloguer-friendly shape.
    """
    rows = sorted(mint_failures, key=lambda f: f.anchor_work_uri)
    payload = "\n".join(
        json.dumps(
            {
                "members": list(f.members),
                "anchor_work_uri": f.anchor_work_uri,
                "missing_inputs": list(f.missing_inputs),
                "pref_label": f.pref_label,
                "helmet_bib_ids": list(f.helmet_bib_ids),
            },
            ensure_ascii=False,
        )
        for f in rows
    )
    if payload:
        payload += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _emit_mint_failures_tsv(path: Path, mint_failures: Iterable[MintFailure]) -> None:
    """Cataloguer-facing TSV companion to :func:`_emit_mint_failures`.

    Same row set as the JSONL, but with the columns a cataloguer can
    open in a spreadsheet without parsing JSON:

    - ``helmet_bib_id`` — one bib_id per row even when union-find
      clustered multiple raw Works into a single MintFailure (the
      TSV expands the cluster across N rows). Easier to filter +
      sort than a packed list column.
    - ``title`` — the anchor Work's ``skos:prefLabel`` (MARC 245$a +
      $b). Empty when ``missing_inputs`` contains ``"pref_label"``.
    - ``missing_inputs`` — comma-separated; values:
      ``creator_uri`` and/or ``pref_label``.
    - ``cluster_size`` — total bib_ids in the same MintFailure
      cluster (1 for the common case; >1 when union-find folded
      records before the mint check refused them).
    - ``anchor_work_uri`` — raw Work URI of the cluster anchor (the
      one M8 inspected). For debugging cross-reference with the
      JSONL + ``canonical-map.jsonl``.

    File is always written, even when there are zero failures —
    a header-only TSV is a useful "we ran, everything minted"
    signal that won't surprise the cataloguer if they wired the
    artifact into a workflow.

    Sorted by (``helmet_bib_id``, ``anchor_work_uri``) for stable
    diffs across re-runs.
    """
    header = "helmet_bib_id\ttitle\tmissing_inputs\tcluster_size\tanchor_work_uri\n"
    rows: list[tuple[str, str, str, int, str]] = []
    for failure in mint_failures:
        title = failure.pref_label or ""
        # Sanitise tab + newline out of title — they'd corrupt TSV column
        # alignment. Replace with a single space; collapse runs.
        title_clean = " ".join(title.replace("\t", " ").split())
        missing = ",".join(failure.missing_inputs)
        cluster_size = max(len(failure.helmet_bib_ids), 1)
        if not failure.helmet_bib_ids:
            # Cluster with no extracted bib_ids (rare — would imply the
            # raw Work carried no bf:identifiedBy → Helmet bib_id link).
            # Render one row with an empty bib_id rather than dropping
            # the cluster entirely.
            rows.append(("", title_clean, missing, cluster_size, failure.anchor_work_uri))
            continue
        for bib_id in failure.helmet_bib_ids:
            rows.append((bib_id, title_clean, missing, cluster_size, failure.anchor_work_uri))
    rows.sort(key=lambda r: (r[0], r[4]))
    body = "".join(
        f"{bib_id}\t{title}\t{missing}\t{cluster}\t{anchor}\n"
        for bib_id, title, missing, cluster, anchor in rows
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)
