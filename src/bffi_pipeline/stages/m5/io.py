"""M5 file-system I/O — corpus loading + idempotency check + emit-target name.

The build phase reads every BFFI Turtle + BIBFRAME RDF/XML under the
configured ``corpus_dir``; the query phase writes candidate pairs as
JSONL to :data:`CANDIDATES_FILENAME`. Re-runs of the build phase skip
when both index files exist, are newer than every input, and match
the chosen model name.

P-38 Phase D: extracted from m5/runner.py. No logic change.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from rdflib import Graph

#: Default top-k per Work for the query pass. Spec § 6 default.
DEFAULT_TOP_K: Final[int] = 20

CANDIDATES_FILENAME: Final[str] = "embed-candidates.jsonl"


def _index_inputs(corpus_dir: Path) -> tuple[list[Path], list[Path]]:
    """Return (bffi-turtle, bibframe-rdf) source files under ``corpus_dir``."""
    bffi = sorted((corpus_dir / "bffi").glob("*.ttl")) if (corpus_dir / "bffi").exists() else []
    bibframe = sorted(
        p
        for p in (corpus_dir / "bibframe").glob("*.rdf")
        if (corpus_dir / "bibframe").exists() and not p.name.startswith("_")
    )
    return bffi, bibframe


def _is_index_fresh(
    index_path: Path,
    idmap_path: Path,
    inputs: Iterable[Path],
    model_name: str,
) -> bool:
    """True when both files exist, are newer than every input, and match ``model_name``."""
    if not (index_path.is_file() and idmap_path.is_file()):
        return False
    try:
        idmap = json.loads(idmap_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if idmap.get("model_name") != model_name:
        return False
    out_mtime = min(index_path.stat().st_mtime, idmap_path.stat().st_mtime)
    return all(p.stat().st_mtime <= out_mtime for p in inputs)


def _load_corpus_graph(corpus_dir: Path) -> Graph:
    """Parse every BFFI Turtle and BIBFRAME RDF/XML under ``corpus_dir`` into one graph.

    Mirrors the M4 ``workkey.load_corpus`` helper rather than importing
    it (per the stage-isolation rule).
    """
    g = Graph()
    bffi, bibframe = _index_inputs(corpus_dir)
    for path in bffi:
        g.parse(str(path), format="turtle")
    for path in bibframe:
        g.parse(str(path), format="xml")
    return g
