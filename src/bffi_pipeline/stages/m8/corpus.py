"""M8 corpus loader — fast-path BFFI-corpus concat parse + per-record fallback.

When M3 has emitted ``<corpus_dir>/bffi-corpus.ttl`` and the concat is
at least as new as every per-record ``bffi/*.ttl``, parse the concat
in one ``Graph().parse()`` call (P-19 Phase A speedup). Otherwise
fall back to walking the per-record dir so partial M3 re-runs still
read correct data.

P-38 Phase D: extracted from m8/runner.py. No logic change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from rdflib import Graph

from bffi_pipeline.stages.m8.graph_extract import extract_work_metadata
from bffi_pipeline.stages.m8.schemas import CanonicalWorkInputs

#: P-19 Phase A — matches ``BFFI_CORPUS_FILENAME`` in
#: ``stages/bf_to_bffi.py``. Stages don't import each other per
#: CLAUDE.md "Stage isolation", so the filename is duplicated as a
#: string constant on each side.
_BFFI_CORPUS_FILENAME: Final[str] = "bffi-corpus.ttl"


def _load_work_records_from_corpus(corpus_dir: Path) -> dict[str, CanonicalWorkInputs]:
    """Read every BFFI Turtle under ``corpus_dir`` into a single graph.

    Fast-path (P-19 Phase A): when ``<corpus_dir>/bffi-corpus.ttl``
    exists AND is at least as new as every per-record ``bffi/*.ttl``,
    parse the concat in one ``Graph().parse()`` call. Otherwise fall
    back to the per-record walk so partial M3 re-runs (where only a
    handful of records were updated since the last concat) read
    correct data.

    P-19 Phase B: ``corpus_dir/bibframe/*.rdf`` is NOT loaded. The
    BIBFRAME side was vestigial — M3's CONSTRUCT preserves every
    predicate ``extract_work_metadata`` reads (``bffi:Work`` typing,
    ``bf:identifiedBy`` / ``bf:source`` / ``bf:role``, plus the
    ``bffi:*`` triples) into the per-record BFFI Turtle, so the
    BIBFRAME walk added no information. Empirically verified on the
    2026-05-13 20 k bench: sidelining ``bibframe/`` produced an
    identical ``canonical-map.jsonl`` (16 652 rows, modulo the
    run-time ``merged_at`` timestamp) and dropped M8 corpus-load
    from 315 s to 19 s — a 25x speedup over the original.
    """
    g = Graph()
    bffi_dir = corpus_dir / "bffi"
    corpus_file = corpus_dir / _BFFI_CORPUS_FILENAME

    used_fast_path = False
    if corpus_file.is_file() and bffi_dir.is_dir():
        corpus_mtime = corpus_file.stat().st_mtime
        if all(p.stat().st_mtime <= corpus_mtime for p in bffi_dir.glob("*.ttl")):
            g.parse(str(corpus_file), format="turtle")
            used_fast_path = True
    if not used_fast_path and bffi_dir.is_dir():
        for path in sorted(bffi_dir.glob("*.ttl")):
            g.parse(str(path), format="turtle")

    return extract_work_metadata(g)
