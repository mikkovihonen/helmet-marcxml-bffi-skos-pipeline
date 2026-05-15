"""Unit tests for stages/skosify_run (M10 phase 1).

Runs the real ``skosify.skosify`` (cheap; no network, no LLM) over a
tiny synthetic canonical Turtle to verify the dual-typing behaviour
the spec § 5 overlay-plus-inference approach commits to. The
config + overlay paths are committed in the repo and used as-is by
the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m10.skosify_run import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OVERLAY_PATH,
    SKOSIFIED_FILENAME,
    run,
)

# --- Fixtures -------------------------------------------------------------


WORK = "http://urn.fi/URN:NBN:fi:bib:work:abc"
EXPR = "http://urn.fi/URN:NBN:fi:bib:expression:abc"
ADMIN = "http://urn.fi/URN:NBN:fi:bib:adminmeta/1"


def _build_canonical_graph(*, with_admin: bool = True) -> Graph:
    g = Graph()
    work = URIRef(WORK)
    expr = URIRef(EXPR)
    admin = URIRef(ADMIN)

    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Sota ja rauha", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))

    if with_admin:
        g.add((work, V.adminMetadata, admin))
        g.add((admin, RDF.type, V.AdminMetadata))
        g.add((admin, V.adminMetadataFor, work))
        g.add((admin, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    return g


@pytest.fixture
def canonical_path(tmp_path: Path) -> Path:
    path = tmp_path / "canonical.ttl"
    _build_canonical_graph().serialize(destination=str(path), format="turtle")
    return path


# --- Skosify dual-typing --------------------------------------------------


def test_skosify_run_dual_types_works_as_skos_concept(canonical_path: Path, tmp_path: Path) -> None:
    output = tmp_path / "skosified.ttl"
    result = run(canonical_path, output_path=output)

    assert result.skipped_idempotent is False
    assert output.is_file()
    assert result.dual_typed_works == 1

    g = Graph()
    g.parse(str(output), format="turtle")
    work = URIRef(WORK)
    types = set(g.objects(work, RDF.type))
    assert V.BFFI.Work in types  # BFFI typing preserved
    assert V.SKOS.Concept in types  # SKOS typing inferred via overlay


def test_skosify_run_dual_types_expressions_as_skos_concept(
    canonical_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "skosified.ttl"
    result = run(canonical_path, output_path=output)
    assert result.dual_typed_expressions == 1

    g = Graph()
    g.parse(str(output), format="turtle")
    expr = URIRef(EXPR)
    types = set(g.objects(expr, RDF.type))
    assert V.BFFI.Expression in types
    assert V.SKOS.Concept in types


def test_skosify_run_lifts_has_expression_to_skos_narrower(
    canonical_path: Path, tmp_path: Path
) -> None:
    """spec § 5: ``bffi:hasExpression rdfs:subPropertyOf skos:narrower``."""
    output = tmp_path / "skosified.ttl"
    result = run(canonical_path, output_path=output)
    assert result.inferred_narrower >= 1
    assert result.inferred_broader >= 1

    g = Graph()
    g.parse(str(output), format="turtle")
    work = URIRef(WORK)
    expr = URIRef(EXPR)
    # Both the original BFFI predicate AND the inferred SKOS predicate
    # must be present — the overlay does not destroy the BFFI side.
    assert (work, V.BFFI.hasExpression, expr) in g
    assert (work, V.SKOS.narrower, expr) in g
    assert (expr, V.BFFI.expressionOf, work) in g
    assert (expr, V.SKOS.broader, work) in g


def test_skosify_run_preserves_admin_metadata_block(canonical_path: Path, tmp_path: Path) -> None:
    """The cleanup_* options in bffi.cfg are off; AdminMetadata must survive."""
    output = tmp_path / "skosified.ttl"
    run(canonical_path, output_path=output)

    g = Graph()
    g.parse(str(output), format="turtle")
    work = URIRef(WORK)
    admin_blocks = list(g.objects(work, V.adminMetadata))
    assert len(admin_blocks) == 1
    block = admin_blocks[0]
    assert any(g.triples((block, V.descriptionAuthentication, V.AUTH_AUTO_MERGED)))


def test_skosify_run_uses_committed_overlay_and_config_paths(
    canonical_path: Path, tmp_path: Path
) -> None:
    """When --overlay-path / --config-path aren't passed, defaults apply."""
    output = tmp_path / "skosified.ttl"
    result = run(canonical_path, output_path=output)
    assert result.dual_typed_works == 1
    # Sanity: the constants point at real files.
    assert DEFAULT_OVERLAY_PATH.is_file()
    assert DEFAULT_CONFIG_PATH.is_file()


# --- Idempotency ---------------------------------------------------------


def test_skosify_run_skips_when_output_is_newer_than_inputs(
    canonical_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "skosified.ttl"
    first = run(canonical_path, output_path=output)
    assert first.skipped_idempotent is False

    second = run(canonical_path, output_path=output)
    assert second.skipped_idempotent is True
    assert second.output_triples > 0  # summary still computed


def test_skosify_run_force_re_runs_even_when_output_is_fresh(
    canonical_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "skosified.ttl"
    run(canonical_path, output_path=output)
    forced = run(canonical_path, output_path=output, force=True)
    assert forced.skipped_idempotent is False


# --- Failure modes -------------------------------------------------------


def test_skosify_run_raises_when_canonical_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run(tmp_path / "missing.ttl", output_path=tmp_path / "out.ttl")


# --- Constants -----------------------------------------------------------


def test_skosified_filename_constant() -> None:
    assert SKOSIFIED_FILENAME == "canonical-skosified.ttl"
