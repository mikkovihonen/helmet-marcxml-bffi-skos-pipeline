"""Pin the centralised Turtle-prefix binding contract.

Every namespace the pipeline emits must round-trip through Turtle
serialisation with a stable, human-readable ``@prefix`` declaration.
Without an explicit ``Graph.bind``, rdflib's Turtle serialiser
auto-generates ``ns1`` / ``ns2`` / ... placeholders in
graph-iteration order — non-deterministic across processes, and the
root cause of the 2026-06-07 corpus-concat collision where one
record's ``@prefix ns1: <bflc>`` clobbered another's
``@prefix ns1: <bffi-prov>`` after string-level concatenation.

These tests assert:

1. The canonical helper binds every namespace listed as a project
   namespace in ``vocab.py``, and zero ``ns<N>`` declarations land
   in serialised output containing triples from those namespaces.
2. Every stage that produces canonical Turtle (``m2`` / ``m3`` /
   ``m8`` / ``provenance`` / ``m9`` / ``m10``) routes its bindings
   through the canonical helper rather than maintaining a private
   list — a static-source grep guards against drift.
"""

from __future__ import annotations

import re
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL

from bffi_pipeline.provenance import vocab as V

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PROJECT_ROOT / "src" / "bffi_pipeline"


def test_canonical_helper_binds_every_project_namespace() -> None:
    """Every namespace in ``CANONICAL_TURTLE_PREFIXES`` lands on the
    graph after ``bind_canonical_prefixes``. Sanity check: the helper
    can't silently drop entries from its source dict."""
    g = Graph()
    V.bind_canonical_prefixes(g)
    bound = {short: str(ns) for short, ns in g.namespaces()}
    for short, ns in V.CANONICAL_TURTLE_PREFIXES.items():
        assert short in bound, f"{short!r} not bound after bind_canonical_prefixes"
        assert bound[short] == str(ns), f"{short!r} bound to {bound[short]!r}, expected {str(ns)!r}"


def test_serialised_turtle_emits_zero_auto_prefixes_for_project_namespaces() -> None:
    """Construct a graph that touches every project namespace, serialise
    to Turtle, and assert no ``@prefix ns<N>: …`` line appears.

    Triggers one triple per namespace so the serialiser is forced to
    emit a ``@prefix`` line for each. If a namespace lacked a binding,
    rdflib's auto-prefixer kicks in and the resulting Turtle would
    contain ``@prefix ns1: <…>`` / ``@prefix ns2: <…>`` / etc.
    """
    g = Graph()
    V.bind_canonical_prefixes(g)
    subj = URIRef("urn:test:subject")
    # Emit one triple per project namespace so each gets exercised.
    g.add((subj, V.RDF.type, V.BFFI.Work))
    g.add((subj, V.RDFS.label, Literal("test")))
    g.add((subj, V.SKOS.prefLabel, Literal("test", lang="fi")))
    g.add((subj, OWL.sameAs, URIRef("urn:other")))
    g.add((subj, V.XSD.string, Literal("test")))  # type: ignore[attr-defined]
    g.add((subj, V.DCTERMS.modified, Literal("2026-06-07")))
    g.add((subj, V.PROV.wasGeneratedBy, URIRef("urn:activity")))
    g.add((subj, V.BF.title, Literal("test title")))
    g.add((subj, V.BFLC.marcKey, Literal("245 $a test")))
    g.add((subj, V.MADSRDF.isIdentifiedByAuthority, URIRef("urn:auth")))
    g.add((subj, V.BFFI.contribution, URIRef("urn:contrib")))
    g.add((subj, V.fromMarcField, Literal("test:245:1")))
    g.add((subj, V.BIB.helmetBibId, Literal("b00000001")))

    turtle = g.serialize(format="turtle")
    auto_prefix = re.compile(r"^@prefix\s+ns\d+:", re.MULTILINE)
    matches = auto_prefix.findall(turtle)
    assert not matches, (
        f"rdflib emitted auto-prefixes {matches!r} — a project namespace is "
        "missing from CANONICAL_TURTLE_PREFIXES. Serialised output:\n"
        f"{turtle}"
    )


def test_canonical_helper_is_idempotent() -> None:
    """Calling the helper twice is safe — second call is a no-op.
    Stages that get re-run on an already-bound graph (M9 binds the
    canonical TTL it parses, then re-serialises) must not blow up or
    double-bind."""
    g = Graph()
    V.bind_canonical_prefixes(g)
    bindings_before = sorted(g.namespaces())
    V.bind_canonical_prefixes(g)
    bindings_after = sorted(g.namespaces())
    assert bindings_before == bindings_after


def test_no_private_prefix_bind_lists_in_pipeline_stages() -> None:
    """Static-source check: every ``graph.bind("...", V....)`` call in
    pipeline stages should go through :func:`bind_canonical_prefixes`,
    not maintain a private list. Catches drift between stages where
    one file binds 7 of 13 namespaces and another binds 4 different
    ones.

    Allowlist: ``vocab.py`` itself (defines the helper) and ``m10/
    load_finto.py`` (loads external Finto vocabs into Fuseki — those
    are different namespaces with their own conventions, not the
    pipeline's emit set).
    """
    allowlist = {
        _SRC / "provenance" / "vocab.py",
        _SRC / "stages" / "m10" / "load_finto.py",
    }
    pattern = re.compile(r"\.bind\(\s*['\"]")
    offenders: list[tuple[Path, int, str]] = []
    for py in _SRC.rglob("*.py"):
        if py in allowlist:
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append((py.relative_to(_PROJECT_ROOT), lineno, line.strip()))
    assert not offenders, (
        "Found private prefix-bind calls outside the canonical helper. "
        "Replace with bffi_pipeline.provenance.vocab.bind_canonical_prefixes(graph):\n"
        + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
    )
