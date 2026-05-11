"""Boundary 2: SHACL validation of the post-XSLT BIBFRAME graph.

The shape lives at ``config/shapes/bibframe-conversion.shape.ttl`` and is
loaded once per process. Failures are reported as
``error_type: "bibframe-shape"`` per ``docs/archived/BUILD_PLAN.md`` M2.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pyshacl import validate as pyshacl_validate
from rdflib import Graph

from bffi_pipeline.config import get_settings


class BibframeShapeError(Exception):
    """Boundary-2 SHACL failure. Exposes the human-readable conformance report."""

    def __init__(self, *, message: str, report_text: str, path: Path) -> None:
        super().__init__(message)
        self.message = message
        self.report_text = report_text
        self.path = path

    def __str__(self) -> str:
        return f"[bibframe-shape] {self.path.name}: {self.message}"


@dataclass(frozen=True)
class ShapeReport:
    """Conformance result; ``conforms=False`` means at least one shape failed."""

    conforms: bool
    text: str


def shape_path() -> Path:
    """Return the on-disk path of the bibframe-conversion SHACL shape."""
    return get_settings().config_dir / "shapes" / "bibframe-conversion.shape.ttl"


@lru_cache(maxsize=1)
def _shape_graph() -> Graph:
    g = Graph()
    g.parse(str(shape_path()), format="turtle")
    return g


def validate_graph(data: Graph, *, source_path: Path) -> ShapeReport:
    """Run SHACL on ``data``; return a typed report (no exception on failure)."""
    conforms, _, report_text = pyshacl_validate(
        data,
        shacl_graph=_shape_graph(),
        inference="none",
        meta_shacl=False,
        advanced=True,
        debug=False,
    )
    report = ShapeReport(conforms=bool(conforms), text=str(report_text))
    return report


def assert_conforms(data: Graph, *, source_path: Path) -> None:
    """Run SHACL and raise :class:`BibframeShapeError` if non-conforming."""
    report = validate_graph(data, source_path=source_path)
    if not report.conforms:
        raise BibframeShapeError(
            message="BIBFRAME post-conversion shape failed.",
            report_text=report.text,
            path=source_path,
        )


__all__ = [
    "BibframeShapeError",
    "ShapeReport",
    "assert_conforms",
    "shape_path",
    "validate_graph",
]
