"""Boundary 3: SHACL validation of the post-CONSTRUCT BFFI graph.

The shape lives at ``config/shapes/bffi.shape.ttl``. Per
``docs/archived/BUILD_PLAN.md`` M3, failures are non-blocking: the stage continues,
records are flagged in ``_validation.jsonl`` and a summary count is
surfaced on the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pyshacl import validate as pyshacl_validate
from rdflib import Graph

from bffi_pipeline.config import get_settings


def shape_path() -> Path:
    """Return the on-disk path of the bffi SHACL shape."""
    return get_settings().config_dir / "shapes" / "bffi.shape.ttl"


@lru_cache(maxsize=1)
def _shape_graph() -> Graph:
    g = Graph()
    g.parse(str(shape_path()), format="turtle")
    return g


@dataclass(frozen=True)
class ShapeReport:
    """Conformance result and the human-readable conformance text."""

    conforms: bool
    text: str


def validate_graph(data: Graph) -> ShapeReport:
    """Run SHACL on ``data`` and return a conformance report (no raise)."""
    conforms, _, report_text = pyshacl_validate(
        data,
        shacl_graph=_shape_graph(),
        inference="none",
        meta_shacl=False,
        advanced=True,
        debug=False,
    )
    return ShapeReport(conforms=bool(conforms), text=str(report_text))


__all__ = ["ShapeReport", "shape_path", "validate_graph"]
