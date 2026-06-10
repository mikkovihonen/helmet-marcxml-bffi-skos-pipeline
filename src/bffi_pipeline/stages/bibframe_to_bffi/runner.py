"""Pillar 3 orchestrator: BIBFRAME RDF/XML -> BFFI canonical Turtle.

Step 3 of P-057 — v0 emit. Implements P-56 Phase 1 (clean rename) only:
every ``owl:equivalentClass`` / ``owl:equivalentProperty`` row from the
mapping doc is materialised as a URI substitution; any remaining
``bf:*`` URI in the input falls through to the output and triggers the
closed-namespace test failure. Discriminator routings (Hub /
Identifier-scheme / Title-variant / Series-link / Audio / Music) land in
step 6.

Stage label for observability sidecar events: ``bibframe2bffi``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from rdflib import BNode, Graph, Literal, URIRef

from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.provenance.vocab import bind_canonical_prefixes
from bffi_pipeline.stages.bibframe_to_bffi.mappings import (
    BF_NAMESPACE,
    CleanRenameRules,
    load_rules,
)
from bffi_pipeline.stages.bibframe_to_bffi.routings import apply_all_routings

STAGE: Final[str] = "bibframe2bffi"

#: How often to emit a ``progress`` event during corpus conversion.
PROGRESS_CADENCE: Final[int] = 100


@dataclass(frozen=True)
class ConversionOptions:
    """Configuration for one corpus-conversion run."""

    input_dir: Path
    output_dir: Path
    #: Path to ``vocab/lkd.rdf``. None means look up via ``get_settings``.
    lkd_rdf_path: Path | None = None


@dataclass
class ConversionSummary:
    """Aggregate counts after corpus conversion ends."""

    total: int = 0
    converted: int = 0
    failed: int = 0
    #: Records that contained at least one ``bf:*`` URI after rename +
    #: Phase 4 routings. A non-zero value indicates a closed-namespace
    #: discipline gap on a term family not yet covered.
    closed_namespace_residue: int = 0
    #: Corpus-wide totals of how many times each Phase 4 routing fired.
    #: Surfaces in the observability ``end`` event so the operator can
    #: see, e.g. "this run rewrote 9 525 bf:Isbn instances".
    routing_counters: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[Path, str]] = field(default_factory=list)


class BibframeToBffiError(RuntimeError):
    """A single-record conversion failed."""


def _rename_node(node: object, rules: CleanRenameRules) -> object:
    """Return the BFFI counterpart of ``node`` if a rename applies.

    Literals and blank nodes pass through unchanged; only URIs are
    rewritten via the rules table.
    """
    if isinstance(node, URIRef):
        return rules.rename(node)
    return node


def _residual_bf_uris(graph: Graph) -> set[URIRef]:
    """Return every ``bf:*`` URI still present in ``graph`` after rename.

    Phase 1 expects this set to shrink to zero only for records that
    don't trigger any discriminator-routed term. The summary surfaces
    the per-record count so the operator can target step 6 work.
    """
    out: set[URIRef] = set()
    for s, p, o in graph:
        for node in (s, p, o):
            if isinstance(node, URIRef) and str(node).startswith(BF_NAMESPACE):
                out.add(node)
    return out


def rename_graph(input_graph: Graph, rules: CleanRenameRules) -> Graph:
    """Apply every clean rename rule to ``input_graph``; return a fresh graph.

    The transformation is term-by-term: every URI in subject / predicate /
    object position is looked up in the rules table; matches get
    substituted, non-matches pass through. Blank nodes and literals
    pass through unchanged.

    The output graph has the canonical Turtle prefix bindings applied
    so serialisation is deterministic across records.
    """
    output = Graph()
    bind_canonical_prefixes(output)
    for s, p, o in input_graph:
        renamed_s = _rename_node(s, rules)
        renamed_p = _rename_node(p, rules)
        renamed_o = _rename_node(o, rules)
        # Type guards keep mypy --strict happy; rdflib's add() accepts the
        # general triple shape but we narrow to the types we actually emit.
        assert isinstance(renamed_s, URIRef | BNode)
        assert isinstance(renamed_p, URIRef)
        assert isinstance(renamed_o, URIRef | BNode | Literal)
        output.add((renamed_s, renamed_p, renamed_o))
    return output


def convert_one(
    bibframe_path: Path,
    *,
    options: ConversionOptions,
    rules: CleanRenameRules,
) -> tuple[Path, int, dict[str, int]]:
    """Convert one BIBFRAME RDF/XML file to BFFI Turtle.

    Returns ``(output_path, residual_bf_count, routing_counters)``.
    Pipeline order:

      1. ``rename_graph`` applies p-56 Phase 1 clean renames (including
         BFLC aliases — ``bflc:marcKey`` / ``bflc:simplePlace`` / etc.
         all rename to their ``bffi:*`` counterparts here).
      2. ``apply_all_routings`` applies p-56 Phase 4 discriminator
         routings (Identifier-scheme, Title-variant, Audio, Series-link,
         Hub).
      3. Residual ``bf:*`` URIs are counted — non-zero means a term
         family beyond what Phase 1 + Phase 4 cover.

    The routing counters are per-record; the caller aggregates them
    into the corpus summary.
    """
    output_path = options.output_dir / f"{bibframe_path.stem.removesuffix('.bibframe')}.bffi.ttl"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    input_graph = Graph()
    try:
        input_graph.parse(bibframe_path, format="xml")
    except Exception as exc:
        raise BibframeToBffiError(f"rdflib parse failed for {bibframe_path}: {exc}") from exc

    output_graph = rename_graph(input_graph, rules)
    routing_counters = apply_all_routings(output_graph)
    residual = len(_residual_bf_uris(output_graph))

    # rdflib's RDF/XML parser is permissive and accepts URIs containing
    # spaces / control characters that the stricter Turtle serializer
    # then refuses ("does not look like a valid URI"). These appear in
    # the Helmet corpus when a cataloguer typed free text into a field
    # marc2bibframe2 then concatenates onto a LoC URI base. Catch the
    # serialize-side failure per-record so one bad URI in record N
    # doesn't abort the corpus run; the closed-namespace test still
    # catches any bffi:* drift.
    try:
        turtle = output_graph.serialize(format="turtle")
    except Exception as exc:
        raise BibframeToBffiError(
            f"rdflib turtle serialize failed for {bibframe_path}: {exc}"
        ) from exc

    output_path.write_text(turtle, encoding="utf-8")
    return output_path, residual, routing_counters


def convert_corpus(*, options: ConversionOptions) -> ConversionSummary:
    """Walk ``options.input_dir`` and convert every ``*.bibframe.xml`` to BFFI Turtle.

    Emits observability events through the active emitter (if any):

      - ``start``    once at entry, with ``entities_total``
      - ``progress`` every ``PROGRESS_CADENCE`` records
      - ``failed``   per record that raised :exc:`BibframeToBffiError`
      - ``end``      once at exit, with success / failed bucket counts and
                     the corpus-wide closed-namespace residue total

    Returns the aggregate :class:`ConversionSummary`.
    """
    bibframe_files = sorted(options.input_dir.glob("*.bibframe.xml"))
    total = len(bibframe_files)
    rules = load_rules(options.lkd_rdf_path)

    emit_if_active(
        stage=STAGE,
        event="start",
        counters={"entities_total": total},
    )

    summary = ConversionSummary(total=total)

    for idx, path in enumerate(bibframe_files, start=1):
        try:
            _, residual, routings = convert_one(path, options=options, rules=rules)
            summary.converted += 1
            if residual > 0:
                summary.closed_namespace_residue += 1
            for name, count in routings.items():
                summary.routing_counters[name] = summary.routing_counters.get(name, 0) + count
        except BibframeToBffiError as exc:
            summary.failed += 1
            message = str(exc)
            summary.failures.append((path, message))
            emit_if_active(
                stage=STAGE,
                event="failed",
                extra={"path": str(path), "error": message[:240]},
            )

        if idx % PROGRESS_CADENCE == 0 or idx == total:
            emit_if_active(
                stage=STAGE,
                event="progress",
                counters={"entities_processed": idx},
            )

    emit_if_active(
        stage=STAGE,
        event="end",
        counters={
            "success": summary.converted,
            "failed": summary.failed,
            "closed_namespace_residue": summary.closed_namespace_residue,
            **{f"routing_{name}": count for name, count in summary.routing_counters.items()},
        },
    )

    return summary
